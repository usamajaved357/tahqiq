"""Regressions for parse_atraf.parse_entries_sequential() on real Tuhfat
al-Ashraf pages (docs/HADITH_CROSS_REFERENCING.md section 4f). The fixture is
a few consecutive pages around each case, copied verbatim from the Shamela
export.
"""
import json
from pathlib import Path

import pytest

from scripts.ingestion.parse_atraf import is_real_companion_title, parse_entries_sequential

CASES = {c["name"]: c for c in json.loads((Path(__file__).parent / "fixtures" / "tuhfat_pages.json").read_text(encoding="utf-8"))}


def parse(name: str) -> dict[str, dict]:
    c = CASES[name]
    entries, missing = parse_entries_sequential(c["pages"], c["first_serial"])
    assert missing == []
    return {e["serial"]: e for e in entries}


def test_next_entry_is_not_glued_on():
    # the old parser stored all of "٧٤ - مكحول …" inside entry ٧٣
    e = parse("glued_next_entry_73_74")
    assert list(e) == ["٧١", "٧٢", "٧٣", "٧٤", "٧٥"]
    assert "مكحول" not in e["٧٣"]["text"]
    assert e["٧٣"]["companion"].startswith("مسروق بن الأجدع")
    assert e["٧٤"]["text"].startswith("مكحول")


def test_entry_wrapped_in_a_title_span_is_an_entry():
    # "١٠١٧ -<span data-type="title"> حديث: أنّ النبيَّ ﷺ أعتق صفيَّة…</span>"
    e = parse("entry_in_title_span_1017")
    assert e["١٠١٧"]["text"].startswith("أنّ النبيَّ ﷺ أعتق صفيَّة")
    assert e["١٠١٨"]["codes"] == "دق"


def test_header_glued_to_the_heading_before_it():
    # "…عن سهل بن سعد٤٧٠٥ - د حديث: إن رجلاً أتى النبيَّ ﷺ…"
    e = parse("header_glued_to_heading_4705")
    assert e["٤٧٠٥"]["codes"] == "د"
    assert e["٤٧٠٥"]["text"].startswith("إن رجلاً أتى النبيَّ")
    # Known limit (2026-09-26): that heading is plain text, not a title span,
    # so it stays at the end of ٤٧٠٤'s text and ٤٧٠٥ keeps the previous
    # heading. The only such entry in the book (as is ٧٤'s inline heading
    # "مكحول … عن أبيّ"), and both cite one book only — no link depends on it.
    assert e["٤٧٠٤"]["text"].endswith("عن سهل بن سعد")


def test_heading_after_a_serial_is_still_a_heading():
    # "١٤٣٢١ -<span …> محمد بن عبد الرحمن بن أبي ذئب، عن سعيد المقبري…</span>"
    e = parse("heading_after_serial_14321")
    heading = "محمد بن عبد الرحمن بن أبي ذئب، عن سعيد المقبري، عن أبيه، عن أبي هريرة"
    assert e["١٤٣٢١"]["text"] == ""
    assert e["١٤٣٢٢"]["companion"] == heading
    assert e["١٤٣٢٢"]["codes"] == "خدتسق"


def test_chain_continuation_entries():
    # "١٤٧٥٠ - وبه فيه (الصلاة ٩٠: ٢) يتعاقبون فيكم ملائكة…"
    e = parse("chain_continuation_14750")
    assert list(e) == ["١٤٧٤٨", "١٤٧٤٩", "١٤٧٥٠", "١٤٧٥١"]
    assert e["١٤٧٥٠"]["continues_chain"]
    assert "يتعاقبون فيكم ملائكة" in e["١٤٧٥٠"]["text"]


@pytest.mark.parametrize(
    "title, real",
    [
        ("محمد بن عبد الرحمن بن أبي ذئب، عن سعيد المقبري، عن أبيه، عن أبي هريرة", True),
        ("حديث: نهى عن ثمن الكلب والسنور.د في البيوع (٦٤: ١) عن إبراهيم بن موسى", False),
        ("وبه: من صام رمضان", False),
    ],
)
def test_companion_title_detection(title, real):
    assert is_real_companion_title(title) is real

"""Regressions for parse_takhrij_book.parse_book() (Arna'ut/Risala editions)."""
import json
from pathlib import Path

from scripts.ingestion.parse_takhrij_book import parse_book

MUSNAD_PAGES = json.loads((Path(__file__).parent / "fixtures" / "musnad_page_break.json").read_text(encoding="utf-8"))


def test_page_break_is_a_word_break():
    # Musnad #4663 runs over pages 3644-3645: "بِشَطْرِ مَا" + "يَخْرُجُ" was
    # stored as "مَايَخْرُجُ" (5,879 of the Musnad's page joins fused)
    entries, _ = parse_book(MUSNAD_PAGES)
    by_serial = {e.serial: e for e in entries}
    text = by_serial["٤٦٦٣"].text
    assert "مَايَخْرُجُ" not in text
    assert "مَا\nيَخْرُجُ" in text
    assert by_serial["٤٦٦٤"].text == MUSNAD_PAGES[1]["body"].split("٤٦٦٤ - ")[1]


def test_no_separator_before_punctuation():
    pages = [
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان عن فلان قال: لا يغسل ميت»", "foot": ""},
        {"id": "1-2", "body": "، ثم قال كذا.", "foot": ""},
    ]
    entries, _ = parse_book(pages)
    assert entries[0].text.endswith("ميت»، ثم قال كذا.")


def test_title_mid_page_does_not_cut_the_previous_hadith():
    # the text before a heading still belongs to the hadith in progress,
    # and each hadith takes the heading in effect where it starts
    pages = [
        {"id": "1-1", "body": "<span data-type='title'>مسند أ</span>١ - حَدَّثَنَا فلان قال كذا", "foot": ""},
        {"id": "1-2", "body": "وكذا.<span data-type='title'>مسند ب</span>٢ - حَدَّثَنَا فلان قال", "foot": ""},
    ]
    entries, _ = parse_book(pages)
    assert [(e.serial, e.heading) for e in entries] == [("١", "مسند أ"), ("٢", "مسند ب")]
    assert entries[0].text.endswith("وكذا.")

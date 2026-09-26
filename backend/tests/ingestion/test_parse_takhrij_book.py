"""Regressions for parse_takhrij_book.parse_book() (Arna'ut/Risala editions)."""
import json
from pathlib import Path

from scripts.ingestion.parse_takhrij_book import parse_book, prepare_pages

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


def test_separator_dots_are_not_hadith_text():
    # 1,863 Musnad pages hold only ". . . . ." (end of a volume/section);
    # 1,496 hadith on Neon end in that row, some have it mid-sentence
    pages = prepare_pages([
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان عن زُرْعَةَ", "foot": ""},
        {"id": "1-2", "body": ". . . . . . . . . . . . . . . .", "foot": ""},
        {"id": "1-3", "body": "عَنْ أَبِي هُرَيْرَةَ قال كذا.", "foot": ""},
    ])
    entries, _ = parse_book(pages)
    assert entries[0].text == "حَدَّثَنَا فلان عن زُرْعَةَ\nعَنْ أَبِي هُرَيْرَةَ قال كذا."


def test_footnote_text_after_separator_dots_is_a_footnote():
    # Musnad page 25794-5592: dots, then "= وأخرجه النسائي…" (the previous
    # page's footnote continued) — it was stored as the end of hadith #7120
    pages = prepare_pages([
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان قال كذا (١).", "foot": "(١) إسناده صحيح، وأخرجه="},
        {"id": "1-2", "body": ". . . . . . . .= البخاري (٢٣٥٥).", "foot": ""},
    ])
    entries, _ = parse_book(pages)
    assert entries[0].text == "حَدَّثَنَا فلان قال كذا (١)."
    assert "البخاري (٢٣٥٥)" in entries[0].footnote_text


def test_footnotes_are_matched_by_number_on_each_page():
    # numbering restarts on every page and runs across the hadith on it;
    # a note may start right after a letter ("…تحريف(٢)") or ")" ("(٢٦٤٥٢)(١)")
    pages = [
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان قال كذا (١).٢ - حَدَّثَنَا آخر قال (٢) كذا (٣).",
         "foot": "(١) إسناده صحيح. وأخرجه مسلم (٢٣٨٠) (١٧٢).(٢) في (م): قال تحريف(٣) إسناده ضعيف"},
        {"id": "1-2", "body": "٣ - حَدَّثَنَا ثالث قال كذا (١).", "foot": "(١) حديث حسن"},
    ]
    entries, _ = parse_book(pages)
    notes = {e.serial: e.footnote_text for e in entries}
    assert notes["١"] == "(١) إسناده صحيح. وأخرجه مسلم (٢٣٨٠) (١٧٢)."
    assert notes["٢"] == "(٢) في (م): قال تحريف(٣) إسناده ضعيف"
    assert notes["٣"] == "(١) حديث حسن"


def test_a_missed_footnote_does_not_shift_the_rest():
    # the old FIFO gave every later hadith its neighbour's note after one miss;
    # a misprinted number ("(١٢" for "(٢)") leaves only that marker without a note
    pages = [
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان قال (١) كذا.٢ - حَدَّثَنَا آخر (٢) قال (٣).",
         "foot": "(١) حديث صحيح.(١٢ لفظ قلت ليس في (م).(٣) إسناده ضعيف"},
    ]
    entries, _ = parse_book(pages)
    notes = {e.serial: e.footnote_text for e in entries}
    assert notes["١"].startswith("(١) حديث صحيح.")
    assert notes["٢"] == "(٣) إسناده ضعيف"


def test_footnote_continued_on_the_next_page():
    pages = [
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان قال كذا (١).", "foot": "(١) إسناده صحيح. وأخرجه ="},
        {"id": "1-2", "body": "٢ - حَدَّثَنَا آخر قال كذا (١).", "foot": "= البخاري (١٢).(١) حديث حسن"},
    ]
    entries, _ = parse_book(pages)
    assert "البخاري (١٢)" in entries[0].footnote_text
    assert entries[1].footnote_text == "(١) حديث حسن"


def test_footnotes_printed_in_the_body_after_equals():
    # Musnad page 25794-4318: empty footnote field, notes after "=" in the body
    pages = prepare_pages([
        {"id": "1-1", "body": "١ - حَدَّثَنَا فلان عن سالم= قوله: كذا.(١) إسناده صحيح", "foot": ""},
    ])
    entries, _ = parse_book(pages)
    assert entries[0].text == "حَدَّثَنَا فلان عن سالم"

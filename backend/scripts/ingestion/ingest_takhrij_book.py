"""Ingests one Al-Arna'ut/Risala-style critical edition (already parsed by
scripts/ingestion/parse_takhrij_book.py) as a new HadithCollection: hadith
text, chapter/companion structure, and a grading extracted from the
footnote's own explicit grading statement (e.g. "إسناده صحيح").

Deliberately does NOT touch cross-references here — extracting and
classifying the footnote's "وأخرجه..." citations (same-hadith vs. a
different companion's corroborating "شاهد") is separate, higher-stakes work
that needs its own validation pass before writing anything to
related_hadith_ids. This script only ingests the collection's own text.

Footnote-reference markers left inline in the parsed text ("(¬N)" or plain
"(N)") are stripped before storing — they're this edition's own apparatus,
not part of the hadith itself, and every other collection in this database
stores plain text without them.

A "•" character is also stripped: confirmed (by checking real occurrences
against their surrounding text) to be this edition's own typographic marker
for "a new numbered item begins here," not hadith content — it shows up at
the tail of an entry's buffered text right before the next entry's serial
number, which the boundary regex consumes starting at the digit, leaving
the bullet stranded on the previous entry.

Usage: python -m scripts.ingestion.ingest_takhrij_book <parsed.json> <collection_name> <source_tag>
"""
import json
import re
import sys

from app.models.hadith import Hadith, HadithBook, HadithCollection, HadithGrading
from scripts.ingestion.common import hadith_db_session, upsert_many, upsert_many_returning, upsert_one

FOOTNOTE_MARKER_RE = re.compile(r"\(\s*¬?[٠-٩]+\s*\)")
BULLET_MARKER_RE = re.compile(r"•")

# Al-Arna'ut opens every note that judges a hadith with the verdict: "إسناده
# صحيح على شرط الشيخين…", "حديث صحيح", "صحيح لغيره، وهذا إسناد ضعيف…",
# "إسناده ضعيف لضعف…" (openings counted over all Musnad Ahmad notes,
# 2026-09-26). Only a note's OPENING is read: further in, the notes discuss
# other routes ("…من طريق صالح المري… وإسناده ضعيف") and other hadith, which
# searching the whole footnote picked up (#19804 "صحيح لغيره" read as da'if).
# Ordered so "صحيح لغيره" wins over "صحيح". Not mapped (no grade rather than
# a guess): "إسناده قوي" / "إسناده جيد" (between hasan and sahih in his usage),
# "رجاله ثقات…" (no verdict), "إسناده محتمل للتحسين".
GRADE_OPENINGS: list[tuple[str, str]] = [
    (r"(?:حديث\s+|مرفوعه\s+)?صحيح\s+لغيره", "sahih li-ghayrihi"),
    (r"(?:حديث\s+|مرفوعه\s+)?حسن\s+لغيره", "hasan li-ghayrihi"),
    (r"(?:إسناده|إسناداه)\s+صحيح", "sahih"),
    (r"(?:حديث\s+)?صحيح(?![ء-ي])", "sahih"),
    (r"إسناده\s+حسن", "hasan"),
    (r"(?:حديث\s+)?حسن(?![ء-ي])", "hasan"),
    (r"إسناده\s+ضعيف", "da'if"),
    (r"(?:حديث\s+)?ضعيف(?![ء-ي])", "da'if"),
]
GRADE_OPENING_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, (p, _) in enumerate(GRADE_OPENINGS)))
GRADE_BY_GROUP = {f"g{i}": grade for i, (_, grade) in enumerate(GRADE_OPENINGS)}
_NOTE_SPLIT_RE = re.compile(r"(?:^|(?<=\S))\(\s*¬?\s*[٠-٩]+\s*\)\s*")
_DIACRITICS_RE = re.compile(r"[ً-ْٰ]")


def clean_text(text: str) -> str:
    text = FOOTNOTE_MARKER_RE.sub("", text)
    text = BULLET_MARKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_grade(footnote_text: str) -> str | None:
    """The verdict at the opening of the first of this hadith's own notes
    that opens with one (notes on a word or a manuscript reading come first
    as often as not: "(١) في (م): …(٢) إسناده صحيح…")."""
    for note in _NOTE_SPLIT_RE.split(footnote_text or ""):
        m = GRADE_OPENING_RE.match(_DIACRITICS_RE.sub("", note).strip())
        if m:
            return GRADE_BY_GROUP[m.lastgroup]
    return None


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    parsed_path, collection_name, source_tag = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(parsed_path, encoding="utf-8") as f:
        parsed = json.load(f)
    entries = parsed["entries"]
    print(f"loaded {len(entries)} parsed entries")

    with hadith_db_session() as session:
        collection_id = upsert_one(
            session,
            HadithCollection,
            {"name": collection_name, "total_hadith": len(entries)},
            index_elements=["name"],
        )
        session.commit()

        # One section per RUN of consecutive entries under the same heading,
        # numbered in page order (2026-09-24). The previous scheme — one book
        # per DISTINCT heading text, numbered by sorting the set of headings —
        # merged every repeated heading ("حديث رجل", "نوع آخر", "باب" …) across
        # the whole book into a single section. Its stated reason (book_number
        # stable across re-runs even if the parse shifts) still holds here:
        # the run sequence depends only on the parse, and a re-ingest of an
        # existing collection should go through resection_takhrij_book.py,
        # which corrects rows in place instead of upserting by book_number.
        run_index: list[int] = []
        run_headings: list[str] = []
        for e in entries:
            heading = e["heading"] or "(no heading)"
            if not run_headings or run_headings[-1] != heading:
                run_headings.append(heading)
            run_index.append(len(run_headings) - 1)

        book_id_by_run: dict[int, int] = {}
        for i, heading in enumerate(run_headings):
            book_id_by_run[i] = upsert_one(
                session,
                HadithBook,
                {"collection_id": collection_id, "book_number": i + 1, "name_ar": heading},
                index_elements=["collection_id", "book_number"],
            )
        session.commit()
        print(f"  {len(book_id_by_run)} book/chapter sections")

        # (book_id, hadith_number) is our uniqueness key, but a handful of
        # real cases exist where the SAME printed number covers two
        # genuinely different hadith texts within the same section — a rare
        # edition-side quirk, confirmed by reading actual examples (e.g.
        # "حديث عبادة بن الصامت" #22669 covers two distinct isnad/matn pairs).
        # PostgreSQL's ON CONFLICT DO UPDATE refuses outright if a single
        # INSERT tries to affect the same key twice, so this must be
        # resolved before upserting — but dropping either one would silently
        # lose real hadith text, which "no mistake is bearable" rules out.
        # Disambiguating with a "-2", "-3" suffix keeps both, still
        # traceable back to the edition's own printed number.
        seen_key_counts: dict[tuple[int, str], int] = {}
        disambiguated = 0

        hadith_rows = []
        skipped_empty = 0
        for e, run in zip(entries, run_index):
            text = clean_text(e["text"])
            if not text:
                skipped_empty += 1
                continue
            serial = e["serial"]
            key = (run, serial)
            seen_key_counts[key] = seen_key_counts.get(key, 0) + 1
            if seen_key_counts[key] > 1:
                serial = f"{serial}-{seen_key_counts[key]}"
                disambiguated += 1
            hadith_rows.append(
                {
                    "book_id": book_id_by_run[run],
                    "hadith_number": serial,
                    "text_ar": text,
                    "source_dataset": source_tag,
                    "verification_status": "unverified",
                    "_footnote_text": e["footnote_text"],
                }
            )
        print(f"  entries skipped (empty text after cleaning): {skipped_empty}")
        print(f"  hadith_number disambiguated (real duplicate printed numbers): {disambiguated}")

        footnote_by_key = {
            (r["book_id"], r["hadith_number"]): r.pop("_footnote_text") for r in hadith_rows
        }
        returned = upsert_many_returning(
            session, Hadith, hadith_rows, index_elements=["book_id", "hadith_number"]
        )
        hadith_id_by_key: dict[tuple[int, str], int] = {
            (book_id, hadith_number): hadith_id for hadith_id, book_id, hadith_number in returned
        }
        session.commit()
        print(f"  {len(hadith_id_by_key)} hadith rows upserted")

        grading_rows = []
        graded_count = 0
        for key, hadith_id in hadith_id_by_key.items():
            footnote_text = footnote_by_key.get(key, "")
            grade = extract_grade(footnote_text)
            if grade:
                graded_count += 1
                grading_rows.append(
                    {"hadith_id": hadith_id, "grader_name": "Shu'ayb al-Arna'ut", "grade": grade}
                )
        upsert_many(session, HadithGrading, grading_rows, index_elements=["hadith_id", "grader_name"])
        session.commit()
        print(f"  {graded_count} hadith with an extracted grading (of {len(hadith_id_by_key)})")


if __name__ == "__main__":
    main()

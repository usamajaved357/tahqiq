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

# Ordered so a more specific phrase ("صحيح لغيره") is checked before the
# plainer one it contains ("صحيح") would otherwise also match.
GRADE_PATTERNS: list[tuple[str, str]] = [
    (r"إسناده\s+صحيح\s+على\s+شرط\s+الشيخين", "sahih"),
    (r"إسناده\s+صحيح\s+على\s+شرط\s+مسلم", "sahih"),
    (r"إسناده\s+صحيح\s+على\s+شرط\s+البخاري", "sahih"),
    (r"حديث\s+صحيح\s+لغيره", "sahih li-ghayrihi"),
    (r"إسناده\s+صحيح\s+لغيره", "sahih li-ghayrihi"),
    (r"حديث\s+حسن\s+لغيره", "hasan li-ghayrihi"),
    (r"إسناده\s+حسن\s+لغيره", "hasan li-ghayrihi"),
    (r"إسناده\s+صحيح", "sahih"),
    (r"حديث\s+صحيح", "sahih"),
    (r"إسناده\s+حسن", "hasan"),
    (r"حديث\s+حسن", "hasan"),
    (r"إسناده\s+ضعيف", "da'if"),
    (r"حديث\s+ضعيف", "da'if"),
]
GRADE_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, (p, _) in enumerate(GRADE_PATTERNS)))
GRADE_BY_GROUP = {f"g{i}": grade for i, (_, grade) in enumerate(GRADE_PATTERNS)}


def clean_text(text: str) -> str:
    text = FOOTNOTE_MARKER_RE.sub("", text)
    text = BULLET_MARKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_grade(footnote_text: str) -> str | None:
    """Takes the FIRST explicit grading statement in the footnote — Al-Arna'ut's
    convention is to open a hadith's footnote with its grading before any
    cross-reference citations, so the first match is reliably the grade for
    THIS hadith, not a later hadith's grade bleeding in from a shared page."""
    m = GRADE_RE.search(footnote_text)
    if not m:
        return None
    return GRADE_BY_GROUP[m.lastgroup]


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

        # Assign book_number as a running counter over distinct headings, in
        # a STABLE sort of the distinct heading strings, not first-seen
        # encounter order. HadithBook's unique key is (collection_id,
        # book_number), not the heading text — so book_number is what
        # upsert_one actually matches an existing row by. Encounter order
        # depends on how many entries happen to fall under each heading,
        # which is NOT guaranteed to stay identical between two runs of the
        # parser (confirmed on real data: a re-run after fixing an unrelated
        # boundary bug changed the entries-per-heading distribution enough
        # to shift which heading landed on which book_number). That silently
        # relabeled existing book rows to a DIFFERENT heading on the second
        # run, which orphaned the first run's hadith rows under a book_id
        # that no longer meant what it did when they were written — a
        # confirmed 36,182-row corruption this sort-based numbering fixes by
        # making book_number depend only on the SET of headings, never on
        # the order or count of entries under them.
        book_id_by_heading: dict[str, int] = {}
        distinct_headings = sorted({e["heading"] or "(no heading)" for e in entries})
        book_rows_needed: dict[str, int] = {h: i + 1 for i, h in enumerate(distinct_headings)}

        for heading, number in book_rows_needed.items():
            book_id = upsert_one(
                session,
                HadithBook,
                {"collection_id": collection_id, "book_number": number, "name_ar": heading},
                index_elements=["collection_id", "book_number"],
            )
            book_id_by_heading[heading] = book_id
        session.commit()
        print(f"  {len(book_id_by_heading)} book/chapter sections")

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
        seen_key_counts: dict[tuple[str, str], int] = {}
        disambiguated = 0

        hadith_rows = []
        skipped_empty = 0
        for e in entries:
            text = clean_text(e["text"])
            if not text:
                skipped_empty += 1
                continue
            heading = e["heading"] or "(no heading)"
            serial = e["serial"]
            key = (heading, serial)
            seen_key_counts[key] = seen_key_counts.get(key, 0) + 1
            if seen_key_counts[key] > 1:
                serial = f"{serial}-{seen_key_counts[key]}"
                disambiguated += 1
            hadith_rows.append(
                {
                    "book_id": book_id_by_heading[heading],
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

"""Replace one Arna'ut/Risala edition's Al-Arna'ut gradings with the ones read
from each hadith's OWN footnote (2026-09-26).

The live Musnad Ahmad gradings (16,287, ingested with commit f2eb645's
parser) often belong to another hadith: every footnote of every page a
hadith touched was given to it, and the first grade phrase anywhere in that
text was taken — usually a neighbour's note, or a verdict Arna'ut quotes
(al-Hakim, al-Tirmidhi). Of those that the corrected reading also grades,
3,662 differ; 773 have no note of their own behind them. See
parse_takhrij_book._FootnoteAssembler and ingest_takhrij_book.extract_grade.

Rows are matched to parsed entries exactly as resection_takhrij_book.py
does (printed number; text where one number covers several hadith) and
EVERY row must match, or nothing is written. Only rows of grader
"Shu'ayb al-Arna'ut" on this collection's hadith are touched (the six books'
"Shuaib Al Arnaut" grades come from another source and are left alone).

Without --apply: prints the plan (added / changed / removed, with the
change counts by old -> new grade) and writes <collection>.regrade_plan.json.
With --apply <snapshot.json>: saves every current row it will touch to the
snapshot (refuses to overwrite one), then deletes / updates / inserts in
batches of 1,000, each its own transaction, and re-reads to verify.
--rollback <snapshot.json> <collection> restores the saved rows.

Usage: python -m scripts.ingestion.regrade_takhrij_book <raw_pages.jsonl> <collection_name> [--apply <snapshot.json>]
       python -m scripts.ingestion.regrade_takhrij_book --rollback <snapshot.json> <collection_name>
"""
import json
import os
import sys
from collections import Counter

from sqlalchemy import delete, select, update

from app.models.hadith import Hadith, HadithBook, HadithCollection, HadithGrading
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.ingest_takhrij_book import clean_text, extract_grade
from scripts.ingestion.parse_takhrij_book import load_pages, parse_book, prepare_pages
from scripts.ingestion.resection_takhrij_book import match_entries_to_rows

GRADER = "Shu'ayb al-Arna'ut"
BATCH = 1000


def current_grades(session, coll_id: int) -> dict[int, str]:
    return dict(
        session.execute(
            select(HadithGrading.hadith_id, HadithGrading.grade)
            .join(Hadith, Hadith.id == HadithGrading.hadith_id)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .where(HadithBook.collection_id == coll_id, HadithGrading.grader_name == GRADER)
        ).all()
    )


def write(coll_id: int, remove: list[int], change: dict[int, str], add: dict[int, str]) -> None:
    for i in range(0, len(remove), BATCH):
        with hadith_db_session() as s:
            s.execute(delete(HadithGrading).where(HadithGrading.grader_name == GRADER, HadithGrading.hadith_id.in_(remove[i : i + BATCH])))
            s.commit()
    items = sorted(change.items())
    for i in range(0, len(items), BATCH):
        with hadith_db_session() as s:
            for hid, grade in items[i : i + BATCH]:
                s.execute(update(HadithGrading).where(HadithGrading.grader_name == GRADER, HadithGrading.hadith_id == hid).values(grade=grade))
            s.commit()
    items = sorted(add.items())
    for i in range(0, len(items), BATCH):
        with hadith_db_session() as s:
            present = set(s.scalars(select(HadithGrading.hadith_id).where(
                HadithGrading.grader_name == GRADER, HadithGrading.hadith_id.in_([h for h, _ in items[i : i + BATCH]]))))
            s.add_all(HadithGrading(hadith_id=h, grader_name=GRADER, grade=g) for h, g in items[i : i + BATCH] if h not in present)
            s.commit()


def collection_id(name: str) -> int:
    with hadith_db_session() as s:
        cid = s.scalar(select(HadithCollection.id).filter_by(name=name))
    if cid is None:
        sys.exit(f"no collection {name!r}")
    return cid


def rollback(snapshot_path: str, name: str) -> None:
    snap = json.load(open(snapshot_path, encoding="utf-8"))
    cid = collection_id(name)
    with hadith_db_session() as s:
        now = current_grades(s, cid)
    before = {int(k): v for k, v in snap["before"].items()}
    touched = set(before) | {int(h) for h in snap["touched"]}
    remove = sorted(h for h in touched if h in now and h not in before)
    change = {h: g for h, g in before.items() if h in now and now[h] != g}
    add = {h: g for h, g in before.items() if h not in now}
    write(cid, remove, change, add)
    with hadith_db_session() as s:
        now = current_grades(s, cid)
    bad = [h for h in touched if now.get(h) != before.get(h)]
    print(f"rolled back {len(touched)} hadith; mismatches {len(bad)}")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--rollback":
        rollback(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) not in (3, 5) or (len(sys.argv) == 5 and sys.argv[3] != "--apply"):
        print(__doc__)
        sys.exit(1)
    pages_path, name = sys.argv[1], sys.argv[2]
    snapshot_path = sys.argv[4] if len(sys.argv) == 5 else None

    entries = [e for e in parse_book(prepare_pages(load_pages(pages_path)))[0] if clean_text(e.text)]
    cid = collection_id(name)
    with hadith_db_session() as s:
        rows = s.execute(
            select(Hadith.id, Hadith.hadith_number, Hadith.text_ar)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .where(HadithBook.collection_id == cid)
        ).all()
        now = current_grades(s, cid)
    assigned, _ = match_entries_to_rows(entries, rows)
    unmatched = [(hid, n) for hid, n, _ in rows if hid not in assigned]
    print(f"{len(rows)} rows, {len(entries)} parsed entries; rows unmatched: {len(unmatched)}")
    if unmatched:
        for hid, n in unmatched[:20]:
            print(f"   unmatched row id={hid} number={n}")
        sys.exit("refusing: every row must match its parsed entry")

    new = {hid: g for hid, e in assigned.items() if (g := extract_grade(e.footnote_text))}
    remove = sorted(h for h in now if h not in new)
    change = {h: g for h, g in new.items() if h in now and now[h] != g}
    add = {h: g for h, g in new.items() if h not in now}
    same = sum(1 for h, g in new.items() if now.get(h) == g)
    print(f"grades now: {len(now)}; read from the hadith's own note: {len(new)} {dict(Counter(new.values()))}")
    print(f"plan: keep {same}, change {len(change)}, add {len(add)}, remove {len(remove)} (no verdict in the hadith's own note)")
    print("changes old -> new:", Counter((now[h], g) for h, g in change.items()).most_common(12))
    with open(f"{name.replace(' ', '_')}.regrade_plan.json", "w", encoding="utf-8") as f:
        json.dump({"change": {h: [now[h], g] for h, g in change.items()}, "add": add,
                   "remove": {h: now[h] for h in remove}}, f, ensure_ascii=False, indent=0)
    if not snapshot_path:
        print("DRY RUN — nothing written")
        return

    if os.path.exists(snapshot_path):
        sys.exit(f"snapshot {snapshot_path} already exists — refusing to overwrite a rollback point")
    touched = set(remove) | set(change) | set(add)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump({"collection": name, "before": {h: now[h] for h in touched if h in now}, "touched": sorted(touched)}, f)
    write(cid, remove, change, add)
    with hadith_db_session() as s:
        after = current_grades(s, cid)
    bad = [h for h in set(after) | set(new) if after.get(h) != new.get(h)]
    print(f"written; snapshot {snapshot_path}; verification problems: {len(bad)}")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()

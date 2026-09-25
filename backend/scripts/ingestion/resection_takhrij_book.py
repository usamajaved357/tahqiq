"""Correct an already-ingested Arna'ut/Risala edition in place (2026-09-24):
re-parse with parse_takhrij_book.prepare_pages() and the pre-title fix, then

  1. SECTIONS: one HadithBook per run of consecutive hadith under the same
     heading, numbered in page order. ingest_takhrij_book.py used to make one
     book per DISTINCT heading text, merging every "حديث رجل" / "نوع آخر" /
     "باب" in the whole book into a single section, and — with no plain-text
     headings recognized and pages read out of order — left Musnad Ahmad's
     largest musnads (Abu Hurayra, Ibn 'Umar, Anas, Jabir, Ibn 'Abbas, 'Ali…)
     inside whatever small section preceded them (7,943 hadith under "Abu
     Rimtha").
  2. TEXT: rows whose stored text differs from the corrected parse — front
     matter or a heading glued on, a page tail dropped before a title,
     editorial end-of-section notes.

Hadith ids never change (rows are updated in place), so related_hadith_ids,
gradings and translations are untouched. Each existing row is matched to a
parsed entry by printed number, and by text where one printed number covers
two hadith; the run refuses to write anything unless EVERY row is matched
exactly once.

Usage: python -m scripts.ingestion.resection_takhrij_book <raw_pages.jsonl> <collection_name> [--apply]
Without --apply it only prints the plan (and writes <collection>.resection_plan.json).
"""
import json
import re
import sys
from collections import Counter

from sqlalchemy import bindparam, delete, select, update

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.ingest_takhrij_book import clean_text
from scripts.ingestion.parse_takhrij_book import load_pages, parse_book, prepare_pages

BATCH = 1000
TEMP_BOOK_NUMBER_OFFSET = 100000
_DISAMBIG_RE = re.compile(r"^(.*?)-(\d+)$")


def words(t: str) -> set[str]:
    return set(re.sub(r"[ً-ْٰ]", "", t).split())


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        sys.exit(1)
    pages_path, collection_name = sys.argv[1], sys.argv[2]
    apply = len(sys.argv) == 4 and sys.argv[3] == "--apply"

    entries, _ = parse_book(prepare_pages(load_pages(pages_path)))
    entries = [e for e in entries if clean_text(e.text)]  # ingest skipped empty ones too
    print(f"parsed {len(entries)} entries")

    with hadith_db_session() as session:
        # a plain id, not the ORM object — it is used again after this session closes
        coll_id = session.scalar(select(HadithCollection.id).filter_by(name=collection_name))
        rows = session.execute(
            select(Hadith.id, Hadith.hadith_number, Hadith.text_ar, Hadith.book_id)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .where(HadithBook.collection_id == coll_id)
        ).all()
    print(f"{len(rows)} rows in the database")

    # printed number -> rows ("N", and "N-2"/"N-3" for disambiguated duplicates)
    by_number: dict[str, list[tuple[int, str]]] = {}
    for hid, number, text, _ in rows:
        m = _DISAMBIG_RE.match(number)
        base = m.group(1) if m and m.group(1) and not m.group(1).endswith("-") else number
        by_number.setdefault(base, []).append((hid, text or ""))

    # match entries to rows; for a shared number, assign greedily by text overlap
    assigned: dict[int, object] = {}
    unmatched_entries = []
    by_serial: dict[str, list] = {}
    for e in entries:
        by_serial.setdefault(e.serial, []).append(e)
    for serial, es in by_serial.items():
        cands = list(by_number.get(serial, []))
        if not cands:
            unmatched_entries += es
            continue
        pairs = sorted(
            ((len(words(clean_text(e.text)) & words(t)) / max(1, len(words(t))), i, hid) for i, e in enumerate(es) for hid, t in cands),
            reverse=True,
        )
        used_e, used_r = set(), set()
        for score, i, hid in pairs:
            if i in used_e or hid in used_r:
                continue
            assigned[hid] = es[i]
            used_e.add(i)
            used_r.add(hid)
        unmatched_entries += [e for i, e in enumerate(es) if i not in used_e]
    unmatched_rows = [(hid, n) for hid, n, _, _ in rows if hid not in assigned]
    print(f"rows matched: {len(assigned)}   rows unmatched: {len(unmatched_rows)}   entries unmatched: {len(unmatched_entries)}")
    for hid, n in unmatched_rows[:20]:
        print(f"   unmatched row id={hid} number={n}")
    for e in unmatched_entries[:20]:
        print(f"   unmatched entry {e.serial}: {clean_text(e.text)[:90]}")

    # sections = runs of consecutive entries (page order) with the same heading
    order = {id(e): i for i, e in enumerate(entries)}
    runs: list[tuple[str, int]] = []  # (heading, first entry index)
    run_of_entry: dict[int, int] = {}
    for i, e in enumerate(entries):
        heading = (e.heading or "(no heading)").strip()
        if not runs or runs[-1][0] != heading:
            runs.append((heading, i))
        run_of_entry[id(e)] = len(runs) - 1
    print(f"sections: {len(runs)} (currently {len({b for _, _, _, b in rows})})")

    text_changes = []
    for hid, number, text, _ in rows:
        e = assigned.get(hid)
        if e is not None and clean_text(e.text) != (text or ""):
            text_changes.append({"id": hid, "number": number, "old": text, "new": clean_text(e.text)})
    kinds = Counter()
    for c in text_changes:
        if c["new"] in (c["old"] or "") and len(c["new"]) < len(c["old"] or ""):
            kinds["shortened (glued text removed)"] += 1
        elif (c["old"] or "") in c["new"] and len(c["new"]) > len(c["old"] or ""):
            kinds["lengthened (dropped tail restored)"] += 1
        else:
            kinds["other"] += 1
    print(f"text changes: {len(text_changes)} {dict(kinds)}")

    # a (section, printed number) must stay unique
    key_counts = Counter((run_of_entry[id(assigned[hid])], n) for hid, n, _, _ in rows if hid in assigned)
    clashes = [k for k, v in key_counts.items() if v > 1]
    print(f"(section, number) clashes: {len(clashes)}")

    plan = {
        "sections": [{"book_number": i + 1, "name_ar": h} for i, (h, _) in enumerate(runs)],
        "text_changes": text_changes,
        "unmatched_rows": unmatched_rows,
        "unmatched_entries": [{"serial": e.serial, "text": clean_text(e.text)} for e in unmatched_entries],
    }
    with open(f"{collection_name.replace(' ', '_')}.resection_plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    if not apply:
        print("DRY RUN — nothing written")
        return
    if unmatched_rows or clashes:
        sys.exit("refusing to apply: every row must match exactly one entry and (section, number) must be unique")

    # only sections that actually hold rows (e.g. the Musnad's introduction
    # parses as a "section" whose one junk row was deleted beforehand)
    used_runs = sorted({run_of_entry[id(e)] for e in assigned.values()})
    runs = [runs[i] for i in used_runs]
    remap = {old: new for new, old in enumerate(used_runs)}
    run_of_entry = {k: remap[v] for k, v in run_of_entry.items() if v in remap}
    # Resumable: a run interrupted after creating the new (temp-numbered)
    # sections — Neon cut the connection mid-move on 2026-09-24 — is finished
    # by re-running; those sections are reused, not duplicated. Every step
    # below is idempotent (moves and text edits set absolute values).
    with hadith_db_session() as session:
        books = session.execute(
            select(HadithBook.id, HadithBook.book_number, HadithBook.name_ar).where(HadithBook.collection_id == coll_id)
        ).all()
        old_book_ids = {bid for bid, num, _ in books if num <= TEMP_BOOK_NUMBER_OFFSET}
        temp = {num - TEMP_BOOK_NUMBER_OFFSET - 1: (bid, name) for bid, num, name in books if num > TEMP_BOOK_NUMBER_OFFSET}
        if temp:
            if sorted(temp) != list(range(len(runs))) or any(temp[i][1] != h for i, (h, _) in enumerate(runs)):
                sys.exit("temp-numbered sections exist but do not match this plan — inspect before re-running")
            new_book_id = {i: bid for i, (bid, _) in temp.items()}
            print(f"reusing {len(new_book_id)} sections from an interrupted run")
        else:
            new_book_id = {}
            for i, (heading, _) in enumerate(runs):
                b = HadithBook(collection_id=coll_id, book_number=TEMP_BOOK_NUMBER_OFFSET + i + 1, name_ar=heading)
                session.add(b)
                session.flush()
                new_book_id[i] = b.id
            session.commit()
            print(f"created {len(new_book_id)} sections")

    moves = [{"b_id": hid, "b_book": new_book_id[run_of_entry[id(e)]]} for hid, e in assigned.items()]
    for start in range(0, len(moves), BATCH):
        with hadith_db_session() as session:
            session.execute(
                update(Hadith.__table__).where(Hadith.__table__.c.id == bindparam("b_id")).values(book_id=bindparam("b_book")),
                moves[start : start + BATCH],
            )
            session.commit()
    print(f"moved {len(moves)} rows")

    edits = [{"b_id": c["id"], "b_text": c["new"]} for c in text_changes]
    for start in range(0, len(edits), BATCH):
        with hadith_db_session() as session:
            session.execute(
                update(Hadith.__table__).where(Hadith.__table__.c.id == bindparam("b_id")).values(text_ar=bindparam("b_text")),
                edits[start : start + BATCH],
            )
            session.commit()
    print(f"corrected {len(edits)} texts")

    with hadith_db_session() as session:
        still_used = set(session.scalars(select(Hadith.book_id).where(Hadith.book_id.in_(old_book_ids))))
        if still_used:
            sys.exit(f"old sections still referenced: {sorted(still_used)[:10]} — not deleting")
        session.execute(delete(HadithBook).where(HadithBook.id.in_(old_book_ids)))
        for i in range(len(runs)):
            session.execute(
                update(HadithBook).where(HadithBook.id == new_book_id[i]).values(book_number=i + 1)
            )
        session.commit()
    print(f"removed {len(old_book_ids)} old sections; renumbered 1..{len(runs)}")


if __name__ == "__main__":
    main()

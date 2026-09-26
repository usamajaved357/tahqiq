"""Push reviewed changes from a local working copy of the hadith database to
Neon, sending only the rows that changed (2026-09-26).

Neon's free tier locked the project on 2026-09-24 after full-corpus reads
used up its monthly data transfer. So the heavy work (resection, link plans)
runs on a local copy, and only the difference is sent back:

  baseline — an untouched restore of the Neon dump the working copy came from
  working  — the same dump after the reviewed changes were applied locally
  target   — Neon

It compares baseline and working locally (hadith_books rows, and every
column of hadiths), then reads from the target ONLY a hash of each touched
row, and classifies each row as already done (= working), to do (=
baseline) or a CONFLICT (anything else: the target changed since the dump).
Any conflict, or any change it does not handle (hadith rows added or
removed, other hadith tables changed), stops it before writing.

With --apply, in this order, each step its own short transaction(s):
  1. insert the sections the working copy added (same ids; book numbers
     parked above 200000 so they cannot clash with sections still in use);
  2. update the changed hadiths, 500 per transaction, each guarded by its
     baseline hash (a row that changed meanwhile is not overwritten);
  3. delete the sections the working copy removed (only if no hadith still
     points at them);
  4. give every added/changed section its working values (renumbering goes
     through negative numbers, so the unique (collection, number) holds);
  5. gradings (hadith_gradings, keyed by (hadith_id, grader_name) — their ids
     differ between copies): delete / update / insert, each guarded by the
     baseline value (added 2026-09-26 for the Musnad regrade);
  6. re-read every touched row, section and grading and compare them with
     the working copy.
A re-run after an interruption skips what is done and completes the rest.

Usage: python -m scripts.ingestion.push_hadith_diff <baseline_url> <working_url> <target_url> [--apply]
"""
import sys
from collections import Counter

from sqlalchemy import create_engine, text

BATCH = 500
PARK = 200000
HADITH_COLS = "book_id, hadith_number, text_ar, narrator_chain, topic_tags, related_hadith_ids, source_dataset, verification_status"
BOOK_COLS = "collection_id, book_number, name_en, name_ar"
ROW_HASH = f"md5(row({HADITH_COLS})::text)"
BOOK_HASH = f"md5(row({BOOK_COLS})::text)"
OTHER_TABLES = ("hadith_collections", "hadith_translations")
GRADE_SQL = "select hadith_id, grader_name, grade, source_work, note from hadith_gradings"


def table_fingerprint(conn, table: str) -> tuple[int, str]:
    return tuple(conn.execute(text(f"select count(*), coalesce(md5(string_agg(md5(t::text), '' order by id)), '') from {table} t")).one())


def hashes(conn, sql: str, ids=None) -> dict[int, str]:
    if ids is None:
        return dict(conn.execute(text(sql)).all())
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 5000):
        out.update(conn.execute(text(sql + " where id = any(:ids)"), {"ids": ids[i : i + 5000]}).all())
    return out


def plan(baseline, working):
    with baseline.connect() as b, working.connect() as w:
        for t in OTHER_TABLES:
            if table_fingerprint(b, t) != table_fingerprint(w, t):
                sys.exit(f"{t} differs between baseline and working — not handled by this script")
        hb = hashes(b, f"select id, {ROW_HASH} from hadiths")
        hw = hashes(w, f"select id, {ROW_HASH} from hadiths")
        if hb.keys() != hw.keys():
            sys.exit(f"hadith rows added/removed ({len(hw.keys() - hb.keys())}/{len(hb.keys() - hw.keys())}) — not handled")
        bb = hashes(b, f"select id, {BOOK_HASH} from hadith_books")
        bw = hashes(w, f"select id, {BOOK_HASH} from hadith_books")
        changed = sorted(i for i in hw if hw[i] != hb[i])
        rows = {
            r.id: r._asdict()
            for i in range(0, len(changed), 5000)
            for r in w.execute(text(f"select id, {HADITH_COLS} from hadiths where id = any(:ids)"), {"ids": changed[i : i + 5000]})
        }
        added = sorted(bw.keys() - bb.keys())
        removed = sorted(bb.keys() - bw.keys())
        books_changed = sorted(i for i in bw.keys() & bb.keys() if bw[i] != bb[i])
        books = {r.id: r._asdict() for r in w.execute(text(f"select id, {BOOK_COLS} from hadith_books where id = any(:ids)"), {"ids": added + books_changed})}
    return {"hb": hb, "hw": hw, "rows": rows, "bb": bb, "bw": bw, "added": added, "removed": removed,
            "books_changed": books_changed, "books": books}


def gradings(conn, hadith_ids=None) -> dict[tuple[int, str], tuple]:
    """(hadith_id, grader_name) -> (grade, source_work, note); all rows, or those of hadith_ids."""
    out = {}
    if hadith_ids is None:
        chunks = [conn.execute(text(GRADE_SQL))]
    else:
        ids = sorted(hadith_ids)
        chunks = (conn.execute(text(GRADE_SQL + " where hadith_id = any(:ids)"), {"ids": ids[i : i + 5000]})
                  for i in range(0, len(ids), 5000))
    for rows in chunks:
        for h, g, grade, source_work, note in rows:
            out[(h, g)] = (grade, source_work, note)
    return out


def plan_grades(baseline, working) -> dict:
    with baseline.connect() as b, working.connect() as w:
        gb, gw = gradings(b), gradings(w)
    keys = sorted(k for k in gb.keys() | gw.keys() if gb.get(k) != gw.get(k))
    return {"keys": keys, "base": {k: gb.get(k) for k in keys}, "work": {k: gw.get(k) for k in keys}}


def classify_grades(target, gp):
    with target.connect() as t:
        gt = gradings(t, {h for h, _ in gp["keys"]})
    todo, done, conflict = [], [], []
    for k in gp["keys"]:
        (done if gt.get(k) == gp["work"][k] else todo if gt.get(k) == gp["base"][k] else conflict).append(k)
    return todo, done, conflict


def apply_grades(target, gp, todo) -> None:
    guard = "hadith_id = :h and grader_name = :g and grade = :b0 and source_work is not distinct from :b1 and note is not distinct from :b2"
    n = Counter()
    for i in range(0, len(todo), BATCH):
        with target.begin() as t:
            for h, g in todo[i : i + BATCH]:
                b, w = gp["base"][(h, g)], gp["work"][(h, g)]
                args = {"h": h, "g": g}
                if b is not None:
                    args.update(b0=b[0], b1=b[1], b2=b[2])
                if w is None:
                    done = t.execute(text(f"delete from hadith_gradings where {guard}"), args).rowcount
                elif b is None:
                    done = t.execute(text("insert into hadith_gradings (hadith_id, grader_name, grade, source_work, note) "
                                          "values (:h, :g, :w0, :w1, :w2) on conflict do nothing"),
                                     {**args, "w0": w[0], "w1": w[1], "w2": w[2]}).rowcount
                else:
                    done = t.execute(text(f"update hadith_gradings set grade = :w0, source_work = :w1, note = :w2 where {guard}"),
                                     {**args, "w0": w[0], "w1": w[1], "w2": w[2]}).rowcount
                if done != 1:
                    raise RuntimeError(f"grading {(h, g)} changed on the target meanwhile — rolled back this batch")
                n["delete" if w is None else "insert" if b is None else "update"] += 1
    print(f"gradings: {dict(n)}")


def classify(target, p):
    with target.connect() as t:
        ht = hashes(t, f"select id, {ROW_HASH} from hadiths", p["rows"].keys())
        bt = hashes(t, f"select id, {BOOK_HASH} from hadith_books", p["removed"] + p["books_changed"])
        added_there = {
            r.id: r._asdict()
            for r in t.execute(text(f"select id, {BOOK_COLS} from hadith_books where id = any(:ids)"), {"ids": p["added"]})
        }
    todo, done, conflict = [], [], []
    for i in p["rows"]:
        (done if ht.get(i) == p["hw"][i] else todo if ht.get(i) == p["hb"][i] else conflict).append(i)
    book_conflicts = [i for i in p["books_changed"] if bt.get(i) not in (p["bb"][i], p["bw"][i])]
    book_conflicts += [i for i in p["removed"] if i in bt and bt[i] != p["bb"][i]]
    for i, row in added_there.items():  # inserted by an interrupted run: parked or final
        want = p["books"][i]
        same = all(row[k] == want[k] for k in ("collection_id", "name_en", "name_ar"))
        if not same or row["book_number"] not in (want["book_number"], PARK + want["book_number"]):
            book_conflicts.append(i)
    return todo, done, conflict, book_conflicts


def apply(target, p, todo):
    with target.begin() as t:
        present = set(t.execute(text("select id from hadith_books where id = any(:ids)"), {"ids": p["added"]}).scalars())
        new = [p["books"][i] for i in p["added"] if i not in present]
        if new:
            t.execute(text("insert into hadith_books (id, collection_id, book_number, name_en, name_ar) "
                           "values (:id, :collection_id, :book_number, :name_en, :name_ar)"),
                      [{**b, "book_number": PARK + b["book_number"]} for b in new])
        t.execute(text("select setval(pg_get_serial_sequence('hadith_books', 'id'), (select max(id) from hadith_books))"))
    print(f"sections inserted: {len(new)} (already there: {len(present)})")

    cols = [c.strip() for c in HADITH_COLS.split(",")]
    sets = ", ".join(f"{c} = :{c}" for c in cols)
    moved = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        with target.begin() as t:
            n = 0
            for hid in batch:
                n += t.execute(text(f"update hadiths set {sets} where id = :id and {ROW_HASH} = :guard"),
                               {**p["rows"][hid], "guard": p["hb"][hid]}).rowcount
            if n != len(batch):
                raise RuntimeError(f"batch at {i}: {len(batch) - n} rows changed on the target meanwhile — rolled back this batch")
        moved += n
    print(f"hadiths updated: {moved}")

    with target.begin() as t:
        still = t.execute(text("select distinct book_id from hadiths where book_id = any(:ids)"), {"ids": p["removed"]}).scalars().all()
        if still:
            raise RuntimeError(f"removed sections still referenced: {still[:10]}")
        gone = t.execute(text("delete from hadith_books where id = any(:ids)"), {"ids": p["removed"]}).rowcount
        ids = p["added"] + p["books_changed"]
        if ids:
            t.execute(text("update hadith_books set book_number = -id where id = any(:ids)"), {"ids": ids})
            t.execute(text("update hadith_books set collection_id = :collection_id, book_number = :book_number, "
                           "name_en = :name_en, name_ar = :name_ar where id = :id"), [p["books"][i] for i in ids])
    print(f"sections deleted: {gone}; sections set to working values: {len(ids)}")


def verify(target, p) -> list[str]:
    with target.connect() as t:
        ht = hashes(t, f"select id, {ROW_HASH} from hadiths", p["rows"].keys())
        bt = hashes(t, f"select id, {BOOK_HASH} from hadith_books", p["added"] + p["removed"] + p["books_changed"])
    problems = [f"hadith {i} differs from working" for i in p["rows"] if ht.get(i) != p["hw"][i]]
    problems += [f"section {i} differs from working" for i in p["added"] + p["books_changed"] if bt.get(i) != p["bw"][i]]
    problems += [f"section {i} not deleted" for i in p["removed"] if i in bt]
    return problems


def main() -> None:
    if len(sys.argv) not in (4, 5) or (len(sys.argv) == 5 and sys.argv[4] != "--apply"):
        print(__doc__)
        sys.exit(1)
    baseline, working, target = (create_engine(u, pool_pre_ping=True) for u in sys.argv[1:4])
    p = plan(baseline, working)
    gp = plan_grades(baseline, working)
    todo, done, conflict, book_conflicts = classify(target, p)
    g_todo, g_done, g_conflict = classify_grades(target, gp)
    print(f"hadiths changed: {len(p['rows'])} (to push {len(todo)}, already on target {len(done)}, CONFLICT {len(conflict)})")
    print(f"sections: added {len(p['added'])}, removed {len(p['removed'])}, changed {len(p['books_changed'])}, CONFLICT {len(book_conflicts)}")
    print(f"gradings changed: {len(gp['keys'])} (to push {len(g_todo)}, already on target {len(g_done)}, CONFLICT {len(g_conflict)})")
    if conflict or book_conflicts or g_conflict:
        print(f"   conflicting hadith ids: {conflict[:20]}  sections: {book_conflicts[:20]}  gradings: {g_conflict[:10]}")
        sys.exit("refusing: the target differs from the baseline for these rows")
    if len(sys.argv) == 4:
        print("DRY RUN — nothing written")
        return
    apply(target, p, todo)
    apply_grades(target, gp, g_todo)
    problems = verify(target, p)
    with target.connect() as t:
        gt = gradings(t, {h for h, _ in gp["keys"]})
    problems += [f"grading {k} differs from working" for k in gp["keys"] if gt.get(k) != gp["work"][k]]
    print(f"verification problems: {len(problems)}")
    for x in problems[:20]:
        print("  ", x)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()

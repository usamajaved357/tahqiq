"""Remove and add hadith links (hadiths.related_hadith_ids) from a reviewed
plan, with a snapshot to roll back from (2026-09-25).

apply_atraf.py only ADDS links. Correcting a link (e.g. the Sahih Muslim
route refinement, refine_muslim_routes.py + plan_route_changes.py) also
means REMOVING one, and removal is the dangerous direction: a link can be
justified by several independent sources, and the database does not record
which. So this script does not decide anything — it executes a plan that
plan_route_changes.py built and a person reviewed:

  {"remove": [[a, b], ...], "add": [[a, b], ...], "protect": [[a, b], ...]}

and it is deliberately defensive:
- a DRY RUN unless --apply: prints what would change and checks every id
  exists;
- a pair in both "remove" and "add"/"protect" is never removed;
- before writing, the related_hadith_ids of every row it will touch are saved
  to a snapshot file (refuses to overwrite an existing snapshot);
- links are removed/added in BOTH directions, in batches of 1,000 rows, each
  its own transaction (Neon drops very long transactions — see
  apply_atraf.py), re-reading current values inside each transaction, so a
  re-run after an interruption simply completes;
- afterwards it re-reads every touched row and checks: every removal gone,
  every addition present, every touched link symmetric;
- --rollback <snapshot> restores exactly the saved arrays.

Usage:
  python -m scripts.ingestion.replace_links <plan.json> <snapshot.json>            (dry run)
  python -m scripts.ingestion.replace_links <plan.json> <snapshot.json> --apply
  python -m scripts.ingestion.replace_links --rollback <snapshot.json>
"""
import json
import os
import sys

from sqlalchemy import bindparam, select, update

from app.models.hadith import Hadith
from scripts.ingestion.common import hadith_db_session

BATCH_SIZE = 1000


def load_plan(path: str) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """(removals, additions) as unordered pairs; protected/added pairs are
    taken out of the removals."""
    plan = json.load(open(path, encoding="utf-8"))
    norm = lambda pairs: {tuple(sorted((int(a), int(b)))) for a, b in pairs if int(a) != int(b)}
    add = norm(plan.get("add", []))
    remove = norm(plan.get("remove", [])) - add - norm(plan.get("protect", []))
    return remove, add


def wanted_changes(remove, add) -> dict[int, tuple[set[int], set[int]]]:
    """row id -> (ids to drop from its array, ids to add to it)."""
    per_row: dict[int, tuple[set[int], set[int]]] = {}
    for a, b in remove:
        per_row.setdefault(a, (set(), set()))[0].add(b)
        per_row.setdefault(b, (set(), set()))[0].add(a)
    for a, b in add:
        per_row.setdefault(a, (set(), set()))[1].add(b)
        per_row.setdefault(b, (set(), set()))[1].add(a)
    return per_row


def read_rows(session, ids) -> dict[int, set[int]]:
    out = {}
    ids = sorted(ids)
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        for hid, rel in session.execute(select(Hadith.id, Hadith.related_hadith_ids).where(Hadith.id.in_(chunk))):
            out[hid] = set(rel or [])
    return out


def write_rows(new_values: dict[int, list[int]]) -> None:
    ids = sorted(new_values)
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        with hadith_db_session() as session:
            session.execute(
                update(Hadith.__table__)
                .where(Hadith.__table__.c.id == bindparam("b_id"))
                .values(related_hadith_ids=bindparam("b_related")),
                [{"b_id": i, "b_related": new_values[i]} for i in chunk],
            )
            session.commit()


def verify(remove, add) -> list[str]:
    ids = {i for p in remove | add for i in p}
    with hadith_db_session() as session:
        rel = read_rows(session, ids)
    problems = []
    for a, b in remove:
        if b in rel.get(a, set()) or a in rel.get(b, set()):
            problems.append(f"still linked: {a} <-> {b}")
    for a, b in add:
        if b not in rel.get(a, set()) or a not in rel.get(b, set()):
            problems.append(f"missing link: {a} <-> {b}")
    return problems


def rollback(snapshot_path: str) -> None:
    snapshot = {int(k): v for k, v in json.load(open(snapshot_path, encoding="utf-8")).items()}
    write_rows({k: sorted(v) for k, v in snapshot.items()})
    with hadith_db_session() as session:
        now = read_rows(session, snapshot)
    bad = [k for k, v in snapshot.items() if now.get(k) != set(v)]
    print(f"restored {len(snapshot)} rows; rows not matching the snapshot afterwards: {len(bad)}")
    if bad:
        sys.exit(1)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--rollback":
        rollback(sys.argv[2])
        return
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--apply"):
        print(__doc__)
        sys.exit(1)
    plan_path, snapshot_path = sys.argv[1], sys.argv[2]
    apply = len(sys.argv) == 4

    remove, add = load_plan(plan_path)
    per_row = wanted_changes(remove, add)
    with hadith_db_session() as session:
        current = read_rows(session, per_row)
    missing = sorted(set(per_row) - set(current))
    if missing:
        sys.exit(f"hadith ids not in the database: {missing[:10]} ({len(missing)})")

    present_removals = {(a, b) for a, b in remove if b in current[a] or a in current[b]}
    new_additions = {(a, b) for a, b in add if b not in current[a] or a not in current[b]}
    new_values = {}
    for hid, (drop, gain) in per_row.items():
        value = (current[hid] - drop) | gain
        if value != current[hid]:
            new_values[hid] = sorted(value)
    print(f"plan: {len(remove)} removals ({len(present_removals)} currently present), "
          f"{len(add)} additions ({len(new_additions)} not yet present)")
    print(f"rows that would change: {len(new_values)}")
    for hid in sorted(new_values)[:5]:
        print(f"  {hid}: {sorted(current[hid])} -> {new_values[hid]}")
    if not apply:
        print("DRY RUN — nothing written")
        return

    if os.path.exists(snapshot_path):
        sys.exit(f"snapshot {snapshot_path} already exists — refusing to overwrite a rollback point")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump({str(k): sorted(current[k]) for k in new_values}, f)
    print(f"snapshot of {len(new_values)} rows written to {snapshot_path}")

    # re-read inside the write so a concurrent/partial earlier run is merged, not clobbered
    ids = sorted(new_values)
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        with hadith_db_session() as session:
            now = read_rows(session, chunk)
            values = [{"b_id": i, "b_related": sorted((now[i] - per_row[i][0]) | per_row[i][1])} for i in chunk]
            session.execute(
                update(Hadith.__table__)
                .where(Hadith.__table__.c.id == bindparam("b_id"))
                .values(related_hadith_ids=bindparam("b_related")),
                values,
            )
            session.commit()
    problems = verify(remove, add)
    print(f"written; verification problems: {len(problems)}")
    for p in problems[:20]:
        print("  " + p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()

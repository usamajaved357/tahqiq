"""Phase 2 cross-referencing: apply reviewed atraf clusters (from
match_atraf.py's *_clusters.json output) to hadiths.related_hadith_ids.

Only run this after a human has reviewed the corresponding review .txt file
(per docs/project-documentation.md Section 13 Phase 2's "manually review a
sample of matches" requirement). Idempotent — safe to re-run; merges with
whatever's already in related_hadith_ids rather than overwriting it.

Writes in batches, each its own transaction: a single transaction over
~20,000 row updates was dropped by Neon mid-commit (2026-09-24, "server
closed the connection unexpectedly") and rolled back in full. Because the
merge is additive and idempotent, a re-run after any interruption simply
completes the remaining rows.

Usage: python -m scripts.ingestion.apply_atraf <clusters.json>
"""
import json
import sys

from sqlalchemy import bindparam, select, update

from app.models.hadith import Hadith
from scripts.ingestion.common import hadith_db_session

BATCH_SIZE = 1000


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        clusters = json.load(f)

    # every id each hadith must link to, from all clusters in the file
    wanted: dict[int, set[int]] = {}
    for c in clusters:
        ids = c["hadith_ids"]
        for hid in ids:
            wanted.setdefault(hid, set()).update(i for i in ids if i != hid)

    ids = sorted(wanted)
    updated = 0
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        with hadith_db_session() as session:
            current = dict(
                session.execute(select(Hadith.id, Hadith.related_hadith_ids).where(Hadith.id.in_(chunk))).all()
            )
            missing = set(chunk) - set(current)
            if missing:
                raise SystemExit(f"hadith ids not in the database: {sorted(missing)[:10]}")
            changes = []
            for hid in chunk:
                existing = set(current[hid] or [])
                merged = existing | wanted[hid]
                if merged != existing:
                    changes.append({"b_id": hid, "b_related": sorted(merged)})
            if changes:
                session.execute(
                    update(Hadith.__table__)
                    .where(Hadith.__table__.c.id == bindparam("b_id"))
                    .values(related_hadith_ids=bindparam("b_related")),
                    changes,
                )
            session.commit()
            updated += len(changes)
        print(f"  {min(start + BATCH_SIZE, len(ids))}/{len(ids)} hadith checked, {updated} updated so far", flush=True)

    print(f"clusters applied: {len(clusters)}")
    print(f"hadith rows updated: {updated}")


if __name__ == "__main__":
    main()

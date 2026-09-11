"""Phase 2 cross-referencing: apply reviewed atraf clusters (from
match_atraf.py's *_clusters.json output) to hadiths.related_hadith_ids.

Only run this after a human has reviewed the corresponding review .txt file
(per docs/project-documentation.md Section 13 Phase 2's "manually review a
sample of matches" requirement). Idempotent — safe to re-run; merges with
whatever's already in related_hadith_ids rather than overwriting it.

Usage: python -m scripts.ingestion.apply_atraf <clusters.json>
"""
import json
import sys

from sqlalchemy import select

from app.models.hadith import Hadith
from scripts.ingestion.common import hadith_db_session


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        clusters = json.load(f)

    with hadith_db_session() as session:
        hadith_ids = {hid for c in clusters for hid in c["hadith_ids"]}
        existing = {
            h.id: h
            for h in session.scalars(select(Hadith).where(Hadith.id.in_(hadith_ids)))
        }

        updated = 0
        for c in clusters:
            ids = c["hadith_ids"]
            for hid in ids:
                hadith = existing[hid]
                others = [i for i in ids if i != hid]
                current = set(hadith.related_hadith_ids or [])
                merged = sorted(current | set(others))
                if merged != sorted(current):
                    hadith.related_hadith_ids = merged
                    updated += 1

        session.commit()

    print(f"clusters applied: {len(clusters)}")
    print(f"hadith rows updated: {updated}")


if __name__ == "__main__":
    main()

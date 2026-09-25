"""Turn a list of hadith links that were READ and found WRONG into a
replace_links.py plan for the current database (2026-09-26).

The list names each link by (collection, hadith_number) — not by id — so it
was reviewed once and applies to any copy of the database (the stand-in
rehearsal database and Neon number their rows differently):

  [{"a": ["Sahih Muslim", "1779"], "b": ["Sahih al-Bukhari", "38"],
    "reason": "qiyam Ramadan vs sawm Ramadan (composite hub IM 1326)", ...}, ...]

First use (D3, 2026-09-26): 34 live sibling links read and found wrong —
two different statements joined in one citing hadith ("من صام رمضان وقامه"),
or the same subject from DIFFERENT companions (Ibn 'Umar / Abu Sa'id on a
woman travelling). Every pair must resolve to exactly one row per side, and
pairs in the PROTECT review files (the first Tuhfat al-Ashraf pass, whose
applied file no longer exists — see plan_route_changes.py) are passed as
"protect", so replace_links.py never removes them.

Usage: python -m scripts.ingestion.plan_reviewed_removals <reviewed.json> <plan_out.json> [protect_review.txt ...]
"""
import json
import sys

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.plan_route_changes import pair, review_pairs


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    reviewed = json.load(open(sys.argv[1], encoding="utf-8"))
    out_path, protect_files = sys.argv[2], sys.argv[3:]
    with hadith_db_session() as session:
        key_to_id: dict[tuple[str, str], list[int]] = {}
        for hid, coll, number in session.execute(
            select(Hadith.id, HadithCollection.name, Hadith.hadith_number)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .join(HadithCollection, HadithBook.collection_id == HadithCollection.id)
        ):
            key_to_id.setdefault((coll, number), []).append(hid)
    remove = []
    for x in reviewed:
        ia, ib = key_to_id.get(tuple(x["a"]), []), key_to_id.get(tuple(x["b"]), [])
        if len(ia) != 1 or len(ib) != 1:
            sys.exit(f"not resolvable to one row each: {x['a']} {x['b']} -> {ia} {ib}")
        remove.append(list(pair(ia[0], ib[0])))
    protect = []
    for a, b in review_pairs(protect_files):
        ia, ib = key_to_id.get(a, []), key_to_id.get(b, [])
        if len(ia) == 1 and len(ib) == 1:
            protect.append(list(pair(ia[0], ib[0])))
    hit = {tuple(p) for p in remove} & {tuple(p) for p in protect}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"remove": remove, "add": [], "protect": protect}, f, indent=1)
    print(f"{len(remove)} removals planned; protected pairs {len(protect)}, of which in the removal list: {len(hit)}")


if __name__ == "__main__":
    main()

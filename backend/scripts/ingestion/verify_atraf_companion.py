"""Post-hoc audit pass over match_atraf.py's clusters: for every matched
hadith in every cluster (exact, fuzzy, and overlap alike), check whether the
companion Mizzi named for that citation actually appears in the matched
hadith's isnad. Catches the failure mode text-similarity scoring cannot see:
a short, formulaic legal ruling ("don't sell gold for gold...") narrated
independently by several different companions, where our matcher picked up
someone else's version of the same ruling instead of the one Mizzi cited.

Writes nothing to the database and does not change any cluster file — this
is a read-only audit whose output is a human-reviewable report, per the same
"review before trusting" discipline as match_atraf.py itself.

Usage: python -m scripts.ingestion.verify_atraf_companion <parsed.json> <review_output_prefix> <report_output.txt>
"""
import json
import sys

from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.match_atraf import Hadith, check_companion_in_text, extract_companion_name


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    parsed_path, review_prefix, report_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(parsed_path, encoding="utf-8") as f:
        parsed = json.load(f)
    entries_by_serial = {e["serial"]: e for e in parsed["entries"]}

    clusters = []
    for suffix in ("_clusters.json", "_fuzzy_clusters.json", "_overlap_clusters.json"):
        path = review_prefix + suffix
        try:
            with open(path, encoding="utf-8") as f:
                kind = suffix.replace("_clusters.json", "").lstrip("_") or "exact"
                for c in json.load(f):
                    clusters.append({"kind": kind, **c})
        except FileNotFoundError:
            continue

    all_ids = sorted({hid for c in clusters for hid in c["hadith_ids"]})
    with hadith_db_session() as session:
        rows = session.query(Hadith).filter(Hadith.id.in_(all_ids)).all()
        text_by_id = {r.id: r.text_ar or "" for r in rows}

    passed, failed, unverifiable = [], [], []
    for c in clusters:
        entry = entries_by_serial.get(c["serial"])
        if entry is None:
            continue
        companion_name = extract_companion_name(entry["companion"] or "")
        if companion_name is None:
            unverifiable.append(c)
            continue
        mismatches = [
            hid for hid in c["hadith_ids"]
            if not check_companion_in_text(companion_name, text_by_id.get(hid, ""))
        ]
        if mismatches:
            failed.append({**c, "companion_name": companion_name, "mismatched_ids": mismatches})
        else:
            passed.append(c)

    print(f"total clusters audited: {len(clusters)}")
    print(f"  passed (companion confirmed in every matched hadith): {len(passed)}")
    print(f"  FAILED (companion not found in at least one matched hadith): {len(failed)}")
    print(f"  unverifiable (Mizzi's companion field itself is ambiguous): {len(unverifiable)}")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"=== {len(failed)} FAILED clusters (companion mismatch — likely wrong match) ===\n\n")
        for c in failed:
            entry = entries_by_serial[c["serial"]]
            f.write(f"[{c['kind']}] serial {c['serial']} | companion field: {entry['companion']}\n")
            f.write(f"  extracted companion name: {c['companion_name']}\n")
            f.write(f"  tarf: {entry['tarf'][:150]}\n")
            f.write(f"  all matched hadith_ids: {c['hadith_ids']} | MISMATCHED: {c['mismatched_ids']}\n")
            for hid in c["mismatched_ids"]:
                f.write(f"    id={hid}: {text_by_id.get(hid, '')[:250]}\n")
            f.write("\n")

        f.write(f"\n=== {len(unverifiable)} UNVERIFIABLE clusters (ambiguous companion field, needs manual read) ===\n\n")
        for c in unverifiable:
            entry = entries_by_serial[c["serial"]]
            f.write(f"[{c['kind']}] serial {c['serial']} | companion field: {entry['companion']}\n")
            f.write(f"  tarf: {entry['tarf'][:150]}\n")
            f.write(f"  matched hadith_ids: {c['hadith_ids']}\n\n")

    print(f"wrote report: {report_path}")


if __name__ == "__main__":
    main()

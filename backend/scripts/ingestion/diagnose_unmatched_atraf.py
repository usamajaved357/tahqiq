"""Diagnostic pass over atraf citations that match_atraf.py could not confirm
(even after fuzzy matching), to find out WHY they failed. Writes nothing to
any database or to the real review files — this is a one-off investigation
to decide whether further matching investment is worth it, per the "no
mistake is bearable" standard: before spending more effort loosening
thresholds, we need real evidence of what's actually causing the gap.

For each unmatched citation, records the single best-scoring candidate in
the target collection (even though it's below FUZZY_THRESHOLD), so a human
reviewer can tell "same hadith, different wording" apart from "genuinely a
different/absent hadith" by reading the tarf against that near-miss text.

Usage: python -m scripts.ingestion.diagnose_unmatched_atraf <parsed.json> <output.txt> [sample_size]
"""
import json
import random
import sys
from difflib import SequenceMatcher

from scripts.ingestion.match_atraf import (
    CODE_TO_COLLECTION,
    MIN_TARF_LEN,
    MIN_WORD_LEN,
    build_word_index,
    clean_tarf,
    excerpt_for_display,
    find_match,
    fuzzy_find_match,
    load_hadith_index,
    normalize,
)
from scripts.ingestion.common import hadith_db_session


def best_candidate_even_below_threshold(tarf_norm, candidates, word_index):
    """Same narrowing logic as fuzzy_find_match, but returns the top
    candidate regardless of score, for diagnostic display."""
    words = sorted(set(tarf_norm.split()), key=lambda w: (-len(w), w))
    anchor_words = [w for w in words if len(w) >= MIN_WORD_LEN][:2]
    if not anchor_words:
        return None

    candidate_positions = set()
    for w in anchor_words:
        candidate_positions |= word_index.get(w, set())
    if not candidate_positions:
        return None

    scored = []
    for i in candidate_positions:
        hid, num, orig, norm, idx_map = candidates[i]
        sm = SequenceMatcher(None, tarf_norm, norm, autojunk=False)
        m = sm.find_longest_match(0, len(tarf_norm), 0, len(norm))
        score = m.size / len(tarf_norm) if tarf_norm else 0
        scored.append((score, hid, num, orig, norm, idx_map))
    if not scored:
        return None
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored[0]


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    sample_size = int(sys.argv[3]) if len(sys.argv) == 4 else 50

    with open(in_path, encoding="utf-8") as f:
        parsed = json.load(f)
    entries = parsed["entries"]

    with hadith_db_session() as session:
        index = load_hadith_index(session)
    word_indexes = {name: build_word_index(cands) for name, cands in index.items()}

    unmatched = []  # (entry, code, coll_name, tarf_norm)
    for e in entries:
        codes = {c["code"] for c in e["citations"]}
        resolvable = {c for c in codes if c in CODE_TO_COLLECTION}
        if len(resolvable) < 2:
            continue
        tarf_norm = normalize(clean_tarf(e["tarf"]))
        if len(tarf_norm) < MIN_TARF_LEN:
            continue
        for code in resolvable:
            for coll_name in CODE_TO_COLLECTION[code]:
                candidates = index.get(coll_name, [])
                if find_match(tarf_norm, candidates):
                    continue
                if fuzzy_find_match(tarf_norm, candidates, word_indexes.get(coll_name, {})):
                    continue
                unmatched.append((e, code, coll_name, tarf_norm))

    print(f"total unmatched citations found: {len(unmatched)}")
    random.seed(42)  # reproducible sample
    sample = random.sample(unmatched, min(sample_size, len(unmatched)))

    with open(out_path, "w", encoding="utf-8") as f:
        for e, code, coll_name, tarf_norm in sample:
            f.write(f"=== serial {e['serial']} | companion: {e['companion']} | citing [{code}] {coll_name} ===\n")
            f.write(f"tarf (cleaned): {tarf_norm}\n")
            best = best_candidate_even_below_threshold(tarf_norm, index.get(coll_name, []), word_indexes.get(coll_name, {}))
            if best is None:
                f.write("  no candidate found at all (no shared distinctive word with anything in this collection)\n")
            else:
                score, hid, num, orig, norm, idx_map = best
                excerpt = excerpt_for_display(tarf_norm, orig, norm, idx_map)
                f.write(f"  best near-miss: [{coll_name} #{num}] score={score:.2f} (below {0.78} threshold)\n")
                f.write(f"  ...{excerpt}...\n")
            f.write("\n")

    print(f"wrote {len(sample)}-entry diagnostic sample to {out_path}")


if __name__ == "__main__":
    main()

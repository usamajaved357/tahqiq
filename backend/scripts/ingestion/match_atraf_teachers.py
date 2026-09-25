"""Phase 2 cross-referencing, second Tuhfat al-Ashraf matcher: finds, for each
atraf entry, the hadith row in each cited collection by THREE independent
signals, instead of match_atraf.py's opening-words (tarf) search alone.

Why: match_atraf.py searches a whole collection for the tarf — Mizzi's few
opening words — and on a re-run over the full book (2026-09-24) it left
16,739 citations unmatched and produced only 572 candidate clusters. The
tarf is short and shared by many hadith, so on its own it cannot single one
out. But every citation also names the compiler's OWN teacher, the first
link of that book's isnad ("خ في الصوم (٩٤) عن عبد الله بن محمد، عن هشام"
= Bukhari heard it from Abd Allah b. Muhammad), and the entry heading names
the shared chain down to the companion ("معمر بن راشد، عن الزهري، عن عروة،
عن عائشة"). A row is accepted only when all three agree:
  1. teacher: a distinctive word of the teacher's name sits in the first
     words of the row (where "حدثنا X" is);
  2. chain: most of the chain's distinctive words appear in the row;
  3. tarf: the opening words' report vocabulary (global IDF, narrator names
     excluded — see match_citations.build_corpus_stats) is contained in it;
and it beats the next-best row by a clear margin. Nasa'i's code (س) is
matched against both our Sunan an-Nasa'i (al-Mujtaba) and Sunan al-Kubra,
since Mizzi cites al-Kubra throughout ("س في الاعتكاف (الكبرى ١٢: ١)").

This script does not modify match_atraf.py (whose output was reviewed and
applied) and writes nothing to the database: it writes a review file and a
clusters file for apply_atraf.py.

Usage: python -m scripts.ingestion.match_atraf_teachers <parsed_atraf.json> <review_output.txt>
(parsed_atraf.json from parse_atraf.py, which keeps each entry's raw "text")
"""
import itertools
import json
import re
import sys
from collections import Counter

from sqlalchemy import select

from app.models.hadith import Hadith
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.match_citations import (
    Scorer,
    build_corpus_stats,
    load_cited_hadith_index,
    load_number_index,
    normalize,
)

CODE_COLLECTIONS = {
    "خ": ["Sahih al-Bukhari"],
    "م": ["Sahih Muslim"],
    "د": ["Sunan Abu Dawud"],
    "ت": ["Jami At-Tirmidhi"],
    "س": ["Sunan an-Nasa'i", "Sunan al-Kubra"],
    "ق": ["Sunan Ibn Majah"],
}
# A citation segment starts with a code letter standing alone, optionally
# bracketed, followed by "في"/"فيه"/"وفي" (the book) — "م فيه (النكاح ٥: ٦)"
# = "Muslim, in the same book". The parse_atraf.CITATION_RE needs "في"
# followed by a space, so it never matched "فيه" segments.
SEGMENT_RE = re.compile(r"(?:^|(?<=[\s.\-–)\]⦘]))[\[(]?([خمدتسق])[\])]?\s*(?=(?:في|فيه|وفي)\b)")
TEACHER_STOP = {
    "بن", "ابن", "بنت", "ابي", "ابو", "ابا", "عبد", "الله", "محمد", "احمد", "الرحمن", "علي",
    "كلاهما", "ثلاثتهم", "اربعتهم", "جميعا", "به", "نحوه", "بمعناه", "وعن", "عن", "و",
}
TEACHER_HEAD_WORDS = 12  # "حدثنا X بن Y، قال: حدثنا" — the teacher sits here
MIN_TARF_MATN_WEIGHT = 12.0  # tarf too generic to judge otherwise
# Calibrated by reading (2026-09-24): at 0.60/0.60, 25 random + the 20
# lowest-scoring clusters were all correct; lowering both to 0.50 added 741
# clusters, and 25 random of those + the 15 lowest overall were all correct.
TARF_THRESHOLD = 0.50
CHAIN_THRESHOLD = 0.50
MARGIN = 0.10


def split_segments(text: str) -> list[tuple[str, str]]:
    """[(code, segment_text)] in order of appearance."""
    starts = [(m.start(), m.group(1)) for m in SEGMENT_RE.finditer(text)]
    return [(code, text[pos : (starts[i + 1][0] if i + 1 < len(starts) else len(text))]) for i, (pos, code) in enumerate(starts)]


def teachers_of(segment: str) -> list[list[str]]:
    """Distinctive name words of each teacher named right after the first
    parenthetical: "... (١٢: ٢) عن علي بن حجر وابن أبي عمر، كلاهما عن سفيان"
    -> [["حجر"], ["عمر"]]."""
    m = re.search(r"\)\s*عن\s+([^،\-–.]{2,80})", segment)
    if not m:
        return []
    out = []
    for name in re.split(r"\s+و(?=\S)", m.group(1)):
        name = re.sub(r"\s+عن\s.*$", "", name)  # stop at the next "عن" link
        words = [w for w in normalize(name).split() if len(w) >= 3 and w not in TEACHER_STOP]
        if words:
            out.append(words)
    return out


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    parsed_path, out_path = sys.argv[1], sys.argv[2]
    entries = json.load(open(parsed_path, encoding="utf-8"))["entries"]

    with hadith_db_session() as session:
        cited_index = load_cited_hadith_index(session)
        number_index = load_number_index(session)
    idf, isnad_words, unseen_idf = build_corpus_stats(cited_index)
    scorer = Scorer(idf, isnad_words, unseen_idf, set())

    rows: dict[str, list[tuple[int, str, list[str]]]] = {}
    inverted: dict[str, dict[str, set[int]]] = {}
    for coll in {c for cs in CODE_COLLECTIONS.values() for c in cs}:
        rows[coll] = []
        inverted[coll] = {}
        for rs in number_index[coll].values():
            for hid, norm in rs:
                if not norm.strip():
                    continue
                i = len(rows[coll])
                words = norm.split()
                rows[coll].append((hid, norm, words))
                for w in set(words):
                    if scorer._weight(w) > 5.0:
                        inverted[coll].setdefault(w, set()).add(i)

    def distinctive(words: list[str]) -> list[str]:
        return [w for w in words if w not in TEACHER_STOP and len(w) >= 3]

    clusters, review, reasons = [], [], Counter()
    for e in entries:
        codes = {c for c in e["codes"] if c in CODE_COLLECTIONS}
        if len(codes) < 2 or not e.get("text"):
            continue
        tarf = Counter(normalize(e["tarf"]).split())
        if scorer.matn_weight(tarf) < MIN_TARF_MATN_WEIGHT:
            reasons["tarf_too_generic"] += 1
            continue
        chain = distinctive(normalize(re.sub(r"\(.*?\)", " ", e["companion"] or "")).split())
        segments = split_segments(e["text"])
        matched = []
        for code in sorted(codes):
            teacher_sets = [t for c, seg in segments if c == code for t in teachers_of(seg)]
            if not teacher_sets:
                reasons[(code, "no_teacher_parsed")] += 1
                continue
            best = []
            for coll in CODE_COLLECTIONS[code]:
                votes = Counter(i for w in tarf if scorer._weight(w) > 5.0 for i in inverted[coll].get(w, ()))
                for w in set(chain):
                    for i in inverted[coll].get(w, ()):
                        votes[i] += 1
                for i, _ in votes.most_common(60):
                    hid, norm, words = rows[coll][i]
                    head = set(words[:TEACHER_HEAD_WORDS])
                    # at least half of a teacher's distinctive name words — the
                    # book may drop a nisba ("هارون بن سعيد" for "… الأيلي")
                    if not any(sum(w in head for w in t) * 2 >= len(t) for t in teacher_sets):
                        continue
                    wset = set(words)
                    chain_score = sum(1 for w in chain if w in wset) / len(chain) if chain else 1.0
                    tarf_score = scorer.containment(tarf, wset, matn_only=True)
                    best.append((tarf_score + chain_score, tarf_score, chain_score, coll, hid))
            best.sort(reverse=True)
            if not best:
                reasons[(code, "teacher_not_found")] += 1
                continue
            top = best[0]
            if top[1] < TARF_THRESHOLD or top[2] < CHAIN_THRESHOLD:
                reasons[(code, "weak")] += 1
                continue
            if len(best) > 1 and top[0] - best[1][0] < MARGIN and best[1][4] != top[4]:
                reasons[(code, "ambiguous")] += 1
                continue
            reasons[(code, "ok")] += 1
            matched.append({"code": code, "collection": top[3], "hadith_id": top[4], "tarf": round(top[1], 3), "chain": round(top[2], 3)})
        ids = sorted({m["hadith_id"] for m in matched})
        if len(ids) >= 2:
            clusters.append({"serial": e["serial"], "hadith_ids": ids})
            review.append({"serial": e["serial"], "companion": e["companion"], "tarf": e["tarf"], "matched": matched})

    for k, n in sorted(reasons.items(), key=str):
        print(f"  {k}: {n}")
    print(f"clusters: {len(clusters)}  (link pairs: {sum(len(c['hadith_ids']) * (len(c['hadith_ids']) - 1) // 2 for c in clusters)})")

    with hadith_db_session() as session:
        all_ids = {m["hadith_id"] for r in review for m in r["matched"]}
        text_by_id = dict(session.execute(select(Hadith.id, Hadith.text_ar).where(Hadith.id.in_(all_ids))).all())
    with open(out_path, "w", encoding="utf-8") as f:
        for r in sorted(review, key=lambda r: min(m["tarf"] for m in r["matched"])):
            f.write(f"=== serial {r['serial']} | {r['companion']}\ntarf: {r['tarf'][:160]}\n")
            for m in r["matched"]:
                f.write(f"  [{m['collection']} id={m['hadith_id']} tarf {m['tarf']} chain {m['chain']}] {text_by_id.get(m['hadith_id'], '')[:300]}\n")
            f.write("\n")
    with open(out_path + ".review.json", "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=1)
    with open(out_path + ".clusters.json", "w", encoding="utf-8") as f:
        # every member of an entry is the same report (Mizzi indexes one
        # report per entry, and each member matched that report's opening
        # and chain) — linked pairwise, as 2-member clusters
        json.dump([{"hadith_ids": list(p)} for c in clusters for p in itertools.combinations(c["hadith_ids"], 2)],
                  f, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}, .review.json and .clusters.json")


if __name__ == "__main__":
    main()

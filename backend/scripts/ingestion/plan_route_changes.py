"""Build the link plan for the Sahih Muslim route refinement (2026-09-25):
which links to remove and which to add, for replace_links.py to execute
after review.

Input: the ORIGINAL pairs files (as applied) with the <stem>.pairs.refined.json
that refine_muslim_routes.py wrote beside each, plus every other file that
justified a link now in the database.

A link is only ever a REMOVAL CANDIDATE if a refined citation took it away:
  - the hub link (citing hadith S -> old Muslim row O), when O is no longer
    cited by S at all after refinement;
  - a sibling link (O <-> X) that S's clusters file carried, X another hadith
    S cites (siblings = two books citing the same hadith, linked directly
    only when their texts are the same report, SIBLING_THRESHOLD).
A candidate is REMOVED only if nothing else still justifies it:
  - it is a hub link of some refined citation (another footnote cites it);
  - it is a sibling carried by some clusters file whose two ends are still
    cited together by some hub after refinement;
  - it is in an INDEPENDENT source (Tuhfat al-Ashraf clusters, the v1 links
    kept, ...);
  - it is in a PROTECT file — the first Tuhfat al-Ashraf pass (~500 clusters,
    2026-09-13) was applied from a file that no longer exists, so its two
    stale review files, which list a superset of those pairs by book and
    number, are used to make sure none of them is ever removed.
ADDITIONS: the new hub link (S -> new row N) and a sibling link N <-> X for
each other hadith X that S cites when their texts are the same report
(sibling_similarity >= SIBLING_THRESHOLD, exactly as match_citations.py).

REMOVALS ARE OFF BY DEFAULT (decision 2026-09-25, from the stand-in
rehearsal): every old row is the SAME hadith (same Abd al-Baqi number and
block, same companion) — only a different route — and the removal candidates
included plainly correct links, e.g. Bukhari 6118 <-> Muslim 14226, both
Jarir -> Mansur -> Abu Wa'il -> 'Abd Allah, "إن الصدق يهدي إلى البر",
dropped only because the Musnad hub moved to another row of that number. So
the refinement ADDS the exact route and keeps the old links; --remove plans
the removals too, for a case where a link is proven WRONG.

Writes <out>.json (the plan: remove / add / protect) and <out>.review.txt
(every change with the texts, for reading before anything is applied).

Usage: python -m scripts.ingestion.plan_route_changes <config.json> <out_stem> [--remove]
config: {"pairs": [original .pairs.json files],
         "derived_clusters": [the .clusters.json written with those pairs],
         "independent_clusters": [other applied .clusters.json files],
         "independent_pairs": [JSON lists whose items start [id_a, id_b, ...]],
         "protect_reviews": [Tuhfat review .txt files listing "[Collection #N]"],
         "exclude_pairs": [JSON lists of {"a": [collection, number], "b": [...], "reason": ...}]}
exclude_pairs: sibling additions read and found WRONG (2026-09-26: 8 of the
158 sibling additions — mostly one composite hub, Ibn Majah 1326 "من صام
رمضان وقامه", whose two statements (fasting / night prayer) share almost all
their words, so text similarity cannot separate them) are never added.
"""
import itertools
import json
import re
import sys

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.match_citations import (
    SIBLING_THRESHOLD,
    Scorer,
    build_corpus_stats,
    load_cited_hadith_index,
    normalize,
    sibling_similarity,
)

_REVIEW_MEMBER_RE = re.compile(r"^\s+\[(.+?) #(\S+?)\]")


def pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def cluster_links(paths) -> set[tuple[int, int]]:
    out = set()
    for path in paths:
        for c in json.load(open(path, encoding="utf-8")):
            out |= {pair(a, b) for a, b in itertools.combinations(sorted(set(c["hadith_ids"])), 2)}
    return out


def review_pairs(paths) -> set[tuple[tuple[str, str], tuple[str, str]]]:
    """(collection, number) pairs listed together under one "=== serial" block."""
    out, block = set(), []
    for path in paths:
        for line in list(open(path, encoding="utf-8")) + ["=== end"]:
            if line.startswith("==="):
                out |= {tuple(sorted(p)) for p in itertools.combinations(sorted(set(block)), 2)}
                block = []
            m = _REVIEW_MEMBER_RE.match(line)
            if m:
                block.append((m.group(1), m.group(2)))
    return out


def main() -> None:
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--remove"):
        print(__doc__)
        sys.exit(1)
    with_removals = len(sys.argv) == 4
    config = json.load(open(sys.argv[1], encoding="utf-8"))
    out_stem = sys.argv[2]

    original, refined = [], []
    for path in config["pairs"]:
        original += json.load(open(path, encoding="utf-8"))
        refined += json.load(open(path[: -len(".pairs.json")] + ".pairs.refined.json", encoding="utf-8"))
    cited_old: dict[int, set[int]] = {}
    for p in original:
        cited_old.setdefault(p["musnad_hadith_id"], set()).add(p["cited_hadith_id"])
    cited_new: dict[int, set[int]] = {}
    for p in refined:
        cited_new.setdefault(p["musnad_hadith_id"], set()).add(p["cited_hadith_id"])
    changes = {(p["musnad_hadith_id"], p["route_refined_from"], p["cited_hadith_id"]) for p in refined if "route_refined_from" in p}

    derived = cluster_links(config.get("derived_clusters", []))
    independent = cluster_links(config.get("independent_clusters", []))
    for path in config.get("independent_pairs", []):
        independent |= {pair(int(x[0]), int(x[1])) for x in json.load(open(path, encoding="utf-8"))}
    hub_links = {pair(s, c) for s, cs in cited_new.items() for c in cs}
    sibling_justified = {
        l for s, cs in cited_new.items() for l in (pair(a, b) for a, b in itertools.combinations(sorted(cs), 2)) if l in derived
    }

    with hadith_db_session() as session:
        key_to_id = {}
        for hid, coll, number in session.execute(
            select(Hadith.id, HadithCollection.name, Hadith.hadith_number)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .join(HadithCollection, HadithBook.collection_id == HadithCollection.id)
        ):
            key_to_id.setdefault((coll, number), []).append(hid)
        cited_index = load_cited_hadith_index(session)
        involved = {i for s, o, n in changes for i in (s, o, n)} | {x for s, _, _ in changes for x in cited_new.get(s, ())}
        text = dict(session.execute(select(Hadith.id, Hadith.text_ar).where(Hadith.id.in_(involved))).all())
    protect = set()
    unresolved_protect = 0
    for a, b in review_pairs(config.get("protect_reviews", [])):
        ia, ib = key_to_id.get(a, []), key_to_id.get(b, [])
        if len(ia) == 1 and len(ib) == 1:
            protect.add(pair(ia[0], ib[0]))
        else:
            unresolved_protect += 1
    excluded = set()
    for path in config.get("exclude_pairs", []):
        for x in json.load(open(path, encoding="utf-8")):
            ia, ib = key_to_id.get(tuple(x["a"]), []), key_to_id.get(tuple(x["b"]), [])
            if len(ia) != 1 or len(ib) != 1:
                sys.exit(f"exclusion not resolvable to one row each: {x}")
            excluded.add(pair(ia[0], ib[0]))
    idf, isnad_words, unseen_idf = build_corpus_stats(cited_index)
    scorer = Scorer(idf, isnad_words, unseen_idf, set())
    norm = {i: normalize(t or "") for i, t in text.items()}

    candidates: dict[tuple[int, int], str] = {}
    for s, o, n in changes:
        if o in cited_new.get(s, set()):
            continue  # another footnote of the same hadith still cites the old row
        candidates[pair(s, o)] = f"hub link of {s} re-routed {o} -> {n}"
        for x in cited_old.get(s, set()) - {o}:
            if pair(o, x) in derived:
                candidates.setdefault(pair(o, x), f"sibling via hub {s} (re-routed {o} -> {n})")
    kept, remove = {}, []
    for link, why in sorted(candidates.items()):
        reason = ("still a hub link" if link in hub_links else
                  "sibling of another hub" if link in sibling_justified else
                  "independent source" if link in independent else
                  "protected (first Tuhfat pass)" if link in protect else None)
        if reason:
            kept[link] = reason
        else:
            remove.append((link, why))

    add, sibling_checks = {}, []
    for s, o, n in sorted(changes):
        add[pair(s, n)] = f"hub link of {s} (was {o})"
        for x in sorted(cited_new.get(s, set()) - {n}):
            sim = sibling_similarity(scorer, norm.get(n, ""), norm.get(x, ""))
            sibling_checks.append((n, x, round(sim, 3)))
            if sim >= SIBLING_THRESHOLD and pair(n, x) not in excluded:
                add.setdefault(pair(n, x), f"sibling via hub {s}, similarity {sim:.2f}")

    plan = {
        "remove": [list(l) for l, _ in remove] if with_removals else [],
        "add": [list(l) for l in sorted(add)],
        "protect": [list(l) for l in sorted(kept)],
    }
    with open(out_stem + ".json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    clip = lambda i: re.sub(r"[ً-ْٰ]", "", re.sub(r"\s+", " ", text.get(i, "")))[:220]
    with open(out_stem + ".review.txt", "w", encoding="utf-8") as f:
        f.write(f"{len(changes)} re-routed citations; add {len(add)}; "
                + (f"remove {len(remove)}, candidates kept {len(kept)}" if with_removals
                   else f"removals NOT planned ({len(remove)} candidates listed for information)") + "\n\n")
        for (a, b), why in remove:
            f.write(f"{'REMOVE' if with_removals else 'would-remove'} {a} <-> {b}  ({why})\n  {a}: {clip(a)}\n  {b}: {clip(b)}\n\n")
        for (a, b), why in sorted(add.items()):
            f.write(f"ADD    {a} <-> {b}  ({why})\n  {a}: {clip(a)}\n  {b}: {clip(b)}\n\n")
        for (a, b), why in sorted(kept.items()):
            f.write(f"KEEP   {a} <-> {b}  ({why})\n")
    print(f"re-routed citations: {len(changes)}")
    print(f"removal candidates: {len(candidates)}  -> remove {len(remove)}, keep {len(kept)} "
          f"({dict(__import__('collections').Counter(kept.values()))})")
    print(f"additions: {len(add)} (hub {len({s for s, _, _ in changes})} sources; sibling checks {len(sibling_checks)}, "
          f"{sum(1 for *_, v in sibling_checks if v >= SIBLING_THRESHOLD)} >= {SIBLING_THRESHOLD})")
    print(f"protect pairs resolved: {len(protect)} (unresolved {unresolved_protect}); excluded sibling pairs: {len(excluded)}")
    print(f"wrote {out_stem}.json and {out_stem}.review.txt")


if __name__ == "__main__":
    main()

"""Phase 2 cross-referencing for Musnad Ahmad: resolves each citation from
extract_citations.py into an actual (Musnad Ahmad hadith, cited hadith) pair
and writes a human-readable review file. Writes nothing to any database —
apply_atraf.py (already generic enough to reuse as-is) applies reviewed
clusters, same as the Tuhfat al-Ashraf pipeline.

Why this is a separate, content-verified step rather than trusting the
citation's own page position: extract_citations.py only knows WHICH page a
citation appeared on, not which of the (possibly several) Musnad Ahmad
hadith on that page it actually belongs to — parse_takhrij_book.py's own
per-marker footnote attribution was found to accumulate unbounded
positional drift over a book this size (see docs/HADITH_CROSS_REFERENCING.md),
so "whichever hadith is nearby" cannot be trusted either. Instead: pull
EVERY Musnad Ahmad hadith whose body touches the citation's page as a
candidate, look up the actual text of the hadith the citation claims to
cite (from our own already-ingested Six Books data), and score each
candidate by content overlap against that cited text — reusing the same
IDF-weighted containment scoring already validated on Tuhfat al-Ashraf
(match_atraf.py). A citation only resolves when exactly one candidate
scores confidently above the threshold; multi-candidate pages with no clear
winner, or all-candidates-low-score pages, are logged as unresolved rather
than guessed at.

Usage: python -m scripts.ingestion.match_citations <citations.json> <raw_pages.jsonl> <review_output.txt>
"""
import json
import re
import sys
from collections import Counter

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.match_atraf import build_idf, normalize as _atraf_normalize, weighted_containment
from scripts.ingestion.parse_takhrij_book import load_pages, parse_book

# Whole-hadith-vs-whole-hadith comparison (this script) is a fundamentally
# looser match than match_atraf.py's original use case (a quoted OPENING
# fragment expected to be a near-verbatim prefix of the full stored text) —
# two independent editions narrating the SAME report routinely differ in a
# narrator's name-form (e.g. "غندر" vs "محمد بن جعفر" — the same person),
# minor phrasing, and boilerplate ordering. Reusing match_atraf.py's
# OVERLAP_THRESHOLD (0.85) directly here suppressed even a confirmed-correct
# match to 0.65. Recalibrated empirically (see docs/HADITH_CROSS_REFERENCING.md)
# rather than guessed.
MATCH_THRESHOLD = 0.55
MATCH_MARGIN = 0.15  # keep the margin requirement strict — this is what actually
# protects against a wrong pick among several genuinely similar page-mates,
# more so than the absolute threshold does.

_PUNCT_RE = re.compile(r"[،,.:؛;\"'()»«\-]")


def normalize(s: str) -> str:
    """match_atraf.py's normalize() plus punctuation stripping. NOT applied
    to match_atraf.py's own normalize() directly — that function is shared
    with the already-reviewed-and-applied Tuhfat al-Ashraf pipeline, and
    changing its behavior would risk silently changing results a human
    already reviewed. Confirmed necessary here, not cosmetic: without it,
    "عمير،" (with a trailing comma) and "عمير" tokenize as different words,
    silently losing real overlap credit on every single citation compared."""
    return _PUNCT_RE.sub(" ", _atraf_normalize(s))

_EASTERN_TO_WESTERN = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def to_western_digits(s: str) -> str:
    """The six-books collections store hadith_number in Western/ASCII digits
    (confirmed: Sahih al-Bukhari's rows read '1008', not '١٠٠٨'), but
    citations are extracted from the source text in Eastern Arabic digits —
    without this conversion every lookup below silently misses."""
    return s.translate(_EASTERN_TO_WESTERN)


def build_page_candidates(raw_pages_path: str) -> dict[str, list[tuple[str, str, str]]]:
    """Returns page_id -> [(serial, heading, normalized_text), ...] for every
    Musnad Ahmad hadith whose body touches that page — a hadith spanning
    multiple pages is indexed under every page_id it touches."""
    pages = load_pages(raw_pages_path)
    entries, _ = parse_book(pages)
    index: dict[str, list[tuple[str, str, str]]] = {}
    for e in entries:
        norm = normalize(e.text)
        for pid in e.page_ids:
            index.setdefault(pid, []).append((e.serial, e.heading or "(no heading)", norm))
    return index


def load_cited_hadith_index(session) -> dict[str, dict[str, tuple[int, str]]]:
    """collection_name -> {hadith_number: (hadith_id, normalized_text)}."""
    index: dict[str, dict[str, tuple[int, str]]] = {}
    collections = {c.id: c.name for c in session.scalars(select(HadithCollection))}
    rows = session.execute(
        select(Hadith.id, Hadith.hadith_number, Hadith.text_ar, HadithBook.collection_id).join(
            HadithBook, Hadith.book_id == HadithBook.id
        )
    ).all()
    for hid, number, text, coll_id in rows:
        name = collections.get(coll_id)
        if name is None:
            continue
        index.setdefault(name, {})[number] = (hid, normalize(text or ""))
    return index


# A citation names a source ("وأخرجه X (num)... من طريق NARRATOR، بهذا
# الإسناد") to say "the isnad down to NARRATOR is the same" — NARRATOR is
# very often NOT the first (top) narrator, but a shared point of
# convergence further down a chain that different top-level transmitters
# reported through (completely normal in hadith transmission — multiple
# "turuq" to the same report). A first attempt at using this signal
# compared a FIXED-length window from the START of each text (assuming the
# isnad's own top matters most) and made results measurably WORSE (1674 ->
# 1221 resolved) — confirmed by inspecting a regressed case: two hadith
# reporting the identical chain from "الليث" downward differed only in
# their TOP narrator (إسحاق بن عيسى vs عبد الله بن مسلمة, two different
# people who both heard it from the same teacher further down) — comparing
# fixed-length windows weighted that expected, legitimate difference
# equally against the genuinely shared portion, dragging the score down
# for exactly the common case this was meant to help. The correct signal
# is narrower: does the SPECIFIC named narrator appear ANYWHERE in a
# candidate's text (not a fixed window), used as a targeted bonus on top
# of whole-text scoring — not a replacement for it, and inert (identical to
# the original whole-text-only baseline) whenever no via_hint is available.
VIA_HINT_BONUS = 0.20
_STRUCTURAL_LINK_WORDS = {"بن", "ابن", "بنت", "ابنة", "ابي", "ابو", "عن", "و"}


def via_hint_words(via_hint: str) -> list[str]:
    words = [w for w in normalize(via_hint).split() if len(w) >= 3 and w not in _STRUCTURAL_LINK_WORDS]
    return words


def find_best_candidate(
    cited_norm: str, candidates: list[tuple[str, str, str]], via_hint: str | None = None
) -> tuple[tuple[str, str, str], float] | None:
    if not candidates:
        return None
    cited_words = Counter(cited_norm.split())
    idf = build_idf([(i, "", "", norm, []) for i, (_, _, norm) in enumerate(candidates)])
    median_idf = sorted(idf.values())[len(idf) // 2] if idf else 1.0
    hint_words = via_hint_words(via_hint) if via_hint else []
    scores = []
    for cand in candidates:
        cand_words = Counter(cand[2].split())
        score, _ = weighted_containment(cited_words, cand_words, idf, median_idf)
        if hint_words and any(w in cand[2] for w in hint_words):
            score += VIA_HINT_BONUS
        scores.append((cand, score))
    scores.sort(key=lambda x: -x[1])
    best, best_score = scores[0]
    if best_score < MATCH_THRESHOLD:
        return None
    if len(scores) > 1 and (best_score - scores[1][1]) < MATCH_MARGIN:
        return None  # ambiguous — two candidates too close to call
    return best, best_score


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    citations_path, raw_pages_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(citations_path, encoding="utf-8") as f:
        citations = json.load(f)
    print(f"loaded {len(citations)} citations")

    print("re-parsing Musnad Ahmad to index candidates by page...")
    page_candidates = build_page_candidates(raw_pages_path)
    print(f"indexed {len(page_candidates)} pages with hadith content")

    with hadith_db_session() as session:
        cited_index = load_cited_hadith_index(session)

        # (musnad_serial, musnad_heading) -> (musnad_hadith_id) lookup, so the
        # review file and eventual apply step can reference real DB ids —
        # built the same deterministic way ingest_takhrij_book.py numbers
        # books, so it reproduces the SAME ids that are actually live.
        musnad_coll = session.scalar(select(HadithCollection).filter_by(name="Musnad Ahmad"))
        musnad_books = session.scalars(
            select(HadithBook).filter_by(collection_id=musnad_coll.id)
        ).all()
        heading_by_book_number = {b.book_number: b.name_ar for b in musnad_books}
        book_number_by_heading = {h: n for n, h in heading_by_book_number.items()}
        musnad_hadiths = session.execute(
            select(Hadith.id, Hadith.hadith_number, Hadith.book_id).join(
                HadithBook, Hadith.book_id == HadithBook.id
            ).filter(HadithBook.collection_id == musnad_coll.id)
        ).all()
        book_number_by_book_id = {b.id: b.book_number for b in musnad_books}
        musnad_id_by_key: dict[tuple[int, str], int] = {
            (book_number_by_book_id[book_id], number): hid for hid, number, book_id in musnad_hadiths
        }

    resolved = []
    unresolved_no_candidates = 0
    unresolved_no_confident_match = 0
    unresolved_cited_not_in_db = 0

    for c in citations:
        candidates = page_candidates.get(c["page_id"], [])
        if not candidates:
            unresolved_no_candidates += 1
            continue

        cited_hid = None
        cited_norm = None
        for number in c["numbers"]:
            hit = cited_index.get(c["collection"], {}).get(to_western_digits(number))
            if hit:
                cited_hid, cited_norm = hit
                break
        if cited_hid is None:
            unresolved_cited_not_in_db += 1
            continue

        best = find_best_candidate(cited_norm, candidates, c.get("via_hint"))
        if best is None:
            unresolved_no_confident_match += 1
            continue
        (serial, heading, _), score = best
        book_number = book_number_by_heading.get(heading)
        musnad_hid = musnad_id_by_key.get((book_number, serial)) if book_number else None
        if musnad_hid is None:
            unresolved_no_confident_match += 1
            continue

        resolved.append(
            {
                "musnad_hadith_id": musnad_hid,
                "musnad_serial": serial,
                "musnad_heading": heading,
                "cited_collection": c["collection"],
                "cited_hadith_id": cited_hid,
                "cited_number": c["numbers"],
                "score": round(score, 3),
                "span_text": c["span_text"],
            }
        )

    print(f"resolved: {len(resolved)}")
    print(f"unresolved (no Musnad Ahmad candidate on that page): {unresolved_no_candidates}")
    print(f"unresolved (cited hadith number not found in our DB): {unresolved_cited_not_in_db}")
    print(f"unresolved (no confident single-candidate match): {unresolved_no_confident_match}")

    with hadith_db_session() as session:
        all_ids = {r["musnad_hadith_id"] for r in resolved} | {r["cited_hadith_id"] for r in resolved}
        rows = session.scalars(select(Hadith).where(Hadith.id.in_(all_ids))).all()
        text_by_id = {h.id: h.text_ar for h in rows}

    # Sorted lowest-score-first (riskiest, most worth a human's attention) and
    # grouped under their own Musnad Ahmad hadith (a hadith citing 2+ other
    # books shows once with all its matches together, not as repeated,
    # disconnected entries) — a flat per-citation dump in extraction order
    # buries the handful of borderline calls a reviewer actually needs to
    # see inside thousands of high-confidence, uninteresting ones.
    by_musnad_for_review: dict[int, list[dict]] = {}
    for r in resolved:
        by_musnad_for_review.setdefault(r["musnad_hadith_id"], []).append(r)
    grouped = list(by_musnad_for_review.items())
    grouped.sort(key=lambda kv: min(r["score"] for r in kv[1]))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            f"{len(resolved)} resolved citations across {len(grouped)} Musnad Ahmad hadith, "
            "sorted lowest-confidence-score first.\n"
            "Each cluster links every hadith shown to every other (a clique) via apply_atraf.py.\n\n"
        )
        for musnad_hid, entries in grouped:
            entries.sort(key=lambda r: r["score"])
            first = entries[0]
            f.write(f"##### Musnad Ahmad #{first['musnad_serial']} (id={musnad_hid}, {first['musnad_heading']})\n")
            f.write(f"  {text_by_id.get(musnad_hid, '?')}\n\n")
            for r in entries:
                f.write(f"  --- score {r['score']} ---\n")
                f.write(f"  {r['cited_collection']} #{'/'.join(r['cited_number'])} (id={r['cited_hadith_id']}):\n")
                f.write(f"    {text_by_id.get(r['cited_hadith_id'], '?')}\n")
                f.write(f"  citation text: {r['span_text']}\n\n")
            f.write("\n")

    with open(out_path + ".clusters.json", "w", encoding="utf-8") as f:
        # Group ALL resolved citations by their Musnad Ahmad hadith before
        # writing clusters — NOT one cluster per citation. apply_atraf.py
        # links every member of a cluster to every OTHER member (a clique),
        # not just back to a common hub. A Musnad Ahmad hadith that cites
        # both Bukhari and Abu Dawud for the same report needs one 3-member
        # cluster {musnad, bukhari, abudawud} so Bukhari and Abu Dawud end
        # up directly linked to each other too — two separate 2-member
        # clusters would leave retrieval starting from Bukhari's hadith
        # blind to the Abu Dawud version entirely (confirmed real: 130 of
        # 1,530 resolved Musnad Ahmad hadith cite 2+ different other books).
        by_musnad: dict[int, set[int]] = {}
        for r in resolved:
            by_musnad.setdefault(r["musnad_hadith_id"], set()).add(r["cited_hadith_id"])
        clusters = [
            {"hadith_ids": sorted({musnad_id} | cited_ids)}
            for musnad_id, cited_ids in by_musnad.items()
        ]
        json.dump(clusters, f, ensure_ascii=False, indent=1)

    print(f"wrote {out_path} and {out_path}.clusters.json")


if __name__ == "__main__":
    main()

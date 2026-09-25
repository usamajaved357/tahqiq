"""Phase 2 cross-referencing for Musnad Ahmad: resolves each citation from
extract_citations.py into an actual (Musnad Ahmad hadith, cited hadith) pair
and writes a human-readable review file plus a clusters file for
apply_atraf.py. Writes nothing to any database itself.

Why this is a separate, content-verified step rather than trusting the
citation's own page position: extract_citations.py only knows WHICH page a
citation appeared on, not which of the (possibly several) Musnad Ahmad
hadith on that page it actually belongs to — parse_takhrij_book.py's own
per-marker footnote attribution was found to accumulate unbounded
positional drift over a book this size (see docs/HADITH_CROSS_REFERENCING.md),
so "whichever hadith is nearby" cannot be trusted either. Instead: pull
every Musnad Ahmad hadith whose body touches the citation's page (or the
page before it — see candidates_for_page) as a candidate, look up the text
of the hadith the citation claims to cite, and score each candidate by
content overlap. A citation only resolves when one candidate wins clearly
on BOTH the whole text and the matn (report) alone.

v3 (2026-09-24) — rebuilt after a re-audit found 5 wrong links among the
2,261 applied by the previous version (see docs Section 4c for each one):
  - IDF is computed ONCE over every hadith text in the database, not over
    the handful of candidates on one page. The old per-page IDF gave every
    word the same weight whenever a page had a single candidate (19% of
    applied links), so filler ("حدثنا", "عن", "رسول الله") counted as
    evidence and a short cited text scored ~0.55 against any long hadith.
  - A matn score (words that are NOT narrator names, learned from the
    corpus — see build_corpus_stats) must also clear a threshold, so a
    shared isnad alone can never produce a match (the old failure mode:
    same chain, different hadith).
  - Candidates include the previous page: a footnote often spills onto the
    page after its hadith, and the true target was then not even compared.
  - The narrator hint ("من طريق X") only breaks ties between candidates; it
    no longer lifts a weak match over the threshold.
  - Sahih Muslim now resolves: the footnotes cite Muslim by Fu'ad Abd
    al-Baqi's number, our DB stores fawazahmed0's sequential number, and
    fawazahmed0's English edition carries the Abd al-Baqi number for every
    hadith (see load_muslim_abd_al_baqi_groups).

Usage: python -m scripts.ingestion.match_citations <citations.json> <raw_pages.jsonl> <review_output.txt> [source collection] [risala_tirmidhi_pages.jsonl]

The source defaults to "Musnad Ahmad". For the Arna'ut Sunan editions pass
"Sunan Abu Dawud", "Jami At-Tirmidhi", "Sunan Ibn Majah" or "Sunan an-Nasa'i":
their entries are first mapped to our existing rows of that collection (see
map_edition_entries_to_db) and those rows are what get linked. Abu Dawud
and Ibn Majah also need the Risala Tirmidhi pages file, because they cite
Tirmidhi by that edition's numbering (see RISALA_TIRMIDHI_NUMBERING_SOURCES).
"""
import itertools
import json
import math
import re
import sys
from collections import Counter

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.ingest_hadith import fetch_edition
from scripts.ingestion.match_atraf import normalize as _atraf_normalize
from scripts.ingestion.parse_takhrij_book import load_pages, parse_book

# Thresholds, calibrated against every link the previous version applied
# (2,261, of which 5 were confirmed wrong by reading them) and then checked
# by reading 140 newly-accepted links (random + lowest-score samples, all
# correct). FULL/MATN reject all 5 known-wrong links; about 5% of correct
# links fall below them too, which is the accepted price of precision.
FULL_THRESHOLD = 0.45
MATN_THRESHOLD = 0.40
# A cited text whose matn carries less IDF weight than this is a back-
# reference ("بمثله", "بهذا الإسناد نحوه") with too little report text to
# compare — it must instead clear a stricter whole-text bar.
MIN_MATN_WEIGHT = 30.0
BACKREF_FULL_THRESHOLD = 0.60
MARGIN = 0.08  # over the best DIFFERENT Musnad hadith
VIA_HINT_TIEBREAK = 0.15
# Cited<->cited links (two books citing the same source hadith) are only
# made when the two texts are themselves the same report, so the parts of a
# composite source hadith (e.g. Aisha on night prayer vs on fasting) stay
# connected only through the source hadith, per the product decision.
# Calibrated twice by reading pairs: 0.40 separated all 9 fragment pairs in
# the first (Musnad-only) set, but at the scale of all five editions a
# random sample of the 0.40-0.50 band still held a fragment pair (Nubaysha's
# "كنت نهيتكم عن لحوم الأضاحي" vs his "أيام التشريق أيام أكل وشرب") while
# 39/40 of the 0.50-0.60 band read as the same report.
SIBLING_THRESHOLD = 0.50

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


def map_musnad_entries_to_db(entries, musnad_numbers: dict[str, list[tuple[int, str]]]) -> list[dict]:
    """Map each parsed Musnad Ahmad entry to its row by printed number, and
    by content where one printed number is shared by two hadith (~90 such
    numbers — a real feature of the edition, see docs Section 3). Keyed on
    the number and text, NOT on the section heading: section assignment was
    found to be wrong for a large part of the book (docs Section 5) and is
    being corrected separately, so the citation links must not depend on it."""
    mapped = []
    for e in entries:
        rows = musnad_numbers.get(e.serial, [])
        if not rows:
            continue
        text_norm = normalize(e.text)
        if len(rows) == 1:
            hid = rows[0][0]
        else:
            words = set(text_norm.split())
            hid = max(rows, key=lambda r: len(words & set(r[1].split())) / max(1, len(set(r[1].split()))))[0]
        mapped.append({"serial": e.serial, "page_ids": e.page_ids, "hadith_id": hid, "text_norm": text_norm})
    return mapped


def candidates_for_page(page_candidates, page_id: str) -> list[tuple[str, str, str]]:
    """The citation's own page plus the page before it. Confirmed necessary:
    a footnote for the last hadith on page N regularly continues onto page
    N+1's footnote area — one applied link attached an Ibn Majah Eid-prayer
    citation to the only hadith on page N+1 ("مروا صبيانكم") because the
    real target, on page N, was never a candidate."""
    book, n = page_id.split("-")
    seen, out = set(), []
    for pid in (page_id, f"{book}-{int(n) - 1}"):
        for cand in page_candidates.get(pid, []):
            if (cand[0], cand[1]) not in seen:
                seen.add((cand[0], cand[1]))
                out.append(cand)
    return out


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


def load_number_index(session) -> dict[str, dict[str, list[tuple[int, str]]]]:
    """collection_name -> {hadith_number: [(hadith_id, normalized_text), ...]}.
    A list, unlike load_cited_hadith_index: Musnad Ahmad has ~90 printed
    numbers shared by two distinct hadith (Section 3 of the docs), and a
    citation of such a number must be allowed to resolve to either by
    content. Musnad Ahmad and Sunan al-Kubra store Eastern-digit numbers
    (the Risala editions' own), the six books Western digits."""
    index: dict[str, dict[str, list[tuple[int, str]]]] = {}
    collections = {c.id: c.name for c in session.scalars(select(HadithCollection))}
    rows = session.execute(
        select(Hadith.id, Hadith.hadith_number, Hadith.text_ar, HadithBook.collection_id).join(
            HadithBook, Hadith.book_id == HadithBook.id
        )
    ).all()
    for hid, number, text, coll_id in rows:
        name = collections.get(coll_id)
        if name is not None:
            index.setdefault(name, {}).setdefault(number, []).append((hid, normalize(text or "")))
    return index


EASTERN_DIGIT_COLLECTIONS = {"Musnad Ahmad", "Sunan al-Kubra"}
EDITION_ACCEPT, EDITION_MARGIN = 0.5, 0.1

# Which numbering a source edition's footnotes use for a target — MEASURED by
# resolving every citation under each hypothesis with the same content check
# (2026-09-24), not assumed:
#   Abu Dawud -> Tirmidhi:  our number 5,  Risala-Tirmidhi number 383 (+28 both)
#   Ibn Majah -> Tirmidhi:  our number 9,  Risala-Tirmidhi number 385 (+22 both)
#   Nasa'i    -> Tirmidhi:  our number 268, Risala-Tirmidhi number 1
#   Ibn Majah -> "النسائي (N)": Mujtaba number 0, Sunan al-Kubra number 39
#   Abu Dawud -> "النسائي (N)": Mujtaba number 285, Sunan al-Kubra number 191 (mixed)
# (Musnad Ahmad's own Tirmidhi citations use our numbering: 957 resolve.)
# The Risala Tirmidhi's numbering does not line up with ours (only ~6% of its
# entries), so those citations are translated through map_edition_entries_to_db.
RISALA_TIRMIDHI_NUMBERING_SOURCES = {"Sunan Abu Dawud", "Sunan Ibn Majah"}
NASAI_NUMBER_MEANS = {"Sunan Ibn Majah": ["Sunan al-Kubra"], "Sunan Abu Dawud": ["Sunan an-Nasa'i", "Sunan al-Kubra"]}


def map_edition_entries_to_db(entries, collection_index: dict[str, tuple[int, str]], scorer: "Scorer") -> list[dict]:
    """Map each parsed entry of an Arna'ut Sunan edition to our row of the
    same collection. The printed number is only the first guess, accepted
    when the texts agree (score >= 0.5): verified to hold for ~99% of Abu
    Dawud, Ibn Majah and Nasa'i entries, but only ~6% of Tirmidhi's — the
    Risala Tirmidhi numbers 4,292 entries against our 3,998 with drifting
    offsets. Otherwise the entry is searched by content (a +-60 window for
    the aligned editions; rare-word voting across the whole collection for
    Tirmidhi) and accepted only if clearly ahead of the runner-up. Short
    follow-up entries the edition numbers separately ("وبهذا الإسناد…",
    "نحوه") map to the row that contains them — checked by reading 25
    random content-mapped Tirmidhi entries, all correct."""
    by_num = {int(k): v for k, v in collection_index.items() if k.isdigit()}

    def guess_of(e) -> int | None:
        m = re.match(r"\s*(\d+)", to_western_digits(e.serial))
        return int(m.group(1)) if m else None

    def agrees(e, num) -> bool:
        return num in by_num and scorer.containment(
            Counter(normalize(e.text).split()), set(by_num[num][1].split()), False
        ) >= EDITION_ACCEPT

    aligned = sum(1 for e in entries if (g := guess_of(e)) is not None and agrees(e, g))
    whole_collection = aligned < 0.8 * len(entries)  # numbering doesn't line up -> search everywhere
    inverted: dict[str, set[int]] = {}
    for num, (_, norm) in by_num.items():
        for w in set(norm.split()):
            if scorer._weight(w) > 6.0:
                inverted.setdefault(w, set()).add(num)
    mapped = []
    for e in entries:
        text_norm = normalize(e.text)
        words = Counter(text_norm.split())
        guess = guess_of(e)

        def score(num: int) -> float:
            return scorer.containment(words, set(by_num[num][1].split()), False) if num in by_num else -1.0

        chosen = guess if guess is not None and score(guess) >= EDITION_ACCEPT else None
        if chosen is None:
            if whole_collection or guess is None:
                votes = Counter(n for w in set(text_norm.split()) if scorer._weight(w) > 6.0 for n in inverted.get(w, ()))
                pool = [n for n, _ in votes.most_common(30)]
            else:
                pool = [n for n in range(guess - 60, guess + 61) if n in by_num]
            ranked = sorted(((score(n), n) for n in pool), reverse=True)
            if ranked and ranked[0][0] >= EDITION_ACCEPT and (len(ranked) < 2 or ranked[0][0] - ranked[1][0] >= EDITION_MARGIN):
                chosen = ranked[0][1]
        if chosen is not None:
            mapped.append({"serial": e.serial, "page_ids": e.page_ids, "hadith_id": by_num[chosen][0], "text_norm": text_norm})
    return mapped


def load_muslim_abd_al_baqi_groups(muslim_index: dict[str, tuple[int, str]]) -> dict[int, list[tuple[int, str]]]:
    """Abd al-Baqi global number -> [(hadith_id, normalized_text), ...].

    Musnad Ahmad's footnotes cite Muslim as "مسلم (١٨١٢) (١٤٢)": Abd al-
    Baqi's global number, then his in-book number. Our Sahih Muslim rows are
    numbered by fawazahmed0's own sequence (1..7563), so looking the printed
    number up directly lands on an unrelated hadith (the reason every Muslim
    citation was excluded until now). fawazahmed0's English edition — same
    source and same hadithnumber keys our rows were ingested from — carries
    `arabicnumber` = Abd al-Baqi number with a sub-narration suffix (2763.04);
    verified against five well-known hadith (8, 223, 1599, 1907, 2564). The
    in-book number is NOT used: fawazahmed0's in-book numbering drifts from
    Abd al-Baqi's by an offset that grows through each book (checked), so
    the right sub-narration is chosen by content instead."""
    groups: dict[int, list[tuple[int, str]]] = {}
    for h in fetch_edition("eng-muslim")["hadiths"]:
        if h.get("arabicnumber") is None:
            continue
        hit = muslim_index.get(str(h["hadithnumber"]))
        if hit and hit[1].strip():  # 203 Muslim rows have no Arabic text to compare
            groups.setdefault(int(float(h["arabicnumber"])), []).append(hit)
    return groups


# Normalized-text markers where the matn typically begins — only used to
# LEARN which words are narrator names, never to split an individual text.
_MATN_MARKER = re.compile(
    r"\b(قال رسول الله|ان رسول الله|عن رسول الله|سمعت رسول الله|ان النبي|عن النبي|قال النبي|سمعت النبي)\b"
)


def build_corpus_stats(cited_index) -> tuple[dict[str, float], set[str], float]:
    """(idf, isnad_words, unseen_word_idf) over every hadith text we hold.

    isnad_words: words at least 60% of whose occurrences fall before the
    first matn marker, across every text that has one — i.e. narrator names
    and transmission formulae (وكيع، شعيب، جده، حدثنا …), learned from the
    corpus rather than listed by hand, and without trusting any single
    text's isnad/matn boundary (per-text splitting was tried in this
    re-audit and proved too fragile to gate on)."""
    texts = [norm for coll in cited_index.values() for _, norm in coll.values() if norm.strip()]
    n_docs = len(texts)
    doc_freq: Counter[str] = Counter()
    before: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for t in texts:
        doc_freq.update(set(t.split()))
        m = _MATN_MARKER.search(t)
        if not m or m.start() < 20:
            continue
        before.update(t[: m.start()].split())
        total.update(t.split())
    idf = {w: math.log((n_docs + 1) / (df + 1)) + 1.0 for w, df in doc_freq.items()}
    isnad_words = {w for w, n in total.items() if n >= 5 and before[w] / n >= 0.6}
    return idf, isnad_words, max(idf.values())


# Companion-name lexicon for the sub-narration guard. Built from Musnad
# Ahmad's own section headings (each names a companion) plus the largest
# musnads, whose headings the Shamela source does not contain at all.
_MAJOR_COMPANIONS = {"هريره", "انس", "جابر", "عباس", "عائشه", "مسعود", "الخدري", "عمر"}
_HEADING_GENERIC = set(
    """حديث مسند ومن من بقيه بقية النبي رسول الله عبد عبيد بن ابن ابي ابو بنت ام عن زوج اخت اخي اخو رضي
    تعالي عنه عنها عنهما الانصاري الانصاريه الاسلمي الاشعري الغفاري الثقفي المزني الجهني الكناني السلمي
    الليثي الاسدي القرشي العدوي الضبي السوائي الباهلي الخزاعي الغفاريه الدوسي حب صاحب رجل رجال امراه
    نسوه اصحاب اهل وكانت له صحبه كان يقال ويقال عم خال مولي مولاه جد جده اخبار مسانيد المكيين المدنيين
    الشاميين الكوفيين البصريين الانصار القبائل النساء العشره المبشرين بالجنه المضاف الاصل في وعن الصديق
    الصديقه المؤمنين المومنين عمرو""".split()
)
# Name tokens that are also ordinary words once normalized (على -> علي,
# عمه = "his uncle", …) — each one made unrelated texts look like they
# shared a companion, which is how two wrong-companion links got through.
_AMBIGUOUS_NAME_TOKENS = set(
    """علي عمه بلغني حرام الي عليه امه ابيه جده حديثه صلي سنه يوسف نزل ربه وكان منها هاهنا مره وهي بعض
    ولد فقال لرسول للنبي اني كان الانصاري الجاهليه ثلاثين""".split()
)


def build_companion_lexicon(headings: list[str], cited_index) -> set[str]:
    lex = set(_MAJOR_COMPANIONS)
    for h in headings:
        for t in normalize(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", h)).split():
            t = t.strip("﵁﵂﵄ﷺ")
            if len(t) >= 3 and t not in _HEADING_GENERIC and not re.search(r"[0-9٠-٩]", t):
                lex.add(t)
    texts = [norm for coll in cited_index.values() for _, norm in coll.values()]
    doc_freq: Counter[str] = Counter()
    for t in texts:
        doc_freq.update(set(t.split()))
    # a "name" present in >3% of all hadith is a common word or a very common
    # narrator (سفيان، الزهري …) and cannot tell companions apart
    lex = {t for t in lex if t in _MAJOR_COMPANIONS or doc_freq[t] / len(texts) <= 0.03}
    return lex - _AMBIGUOUS_NAME_TOKENS


_VIA_STOP = {"بن", "ابن", "بنت", "ابنة", "ابي", "ابو", "عن", "و", "طريق", "طرق", "حدثنا"}


def via_hint_words(via_hint: str) -> list[str]:
    return [w for w in normalize(via_hint).split() if len(w) >= 3 and w not in _VIA_STOP]


class Scorer:
    def __init__(self, idf: dict[str, float], isnad_words: set[str], unseen_idf: float, lexicon: set[str]):
        self.idf, self.isnad_words, self.unseen_idf, self.lexicon = idf, isnad_words, unseen_idf, lexicon

    def _weight(self, w: str) -> float:
        return self.idf.get(w, self.unseen_idf)

    def containment(self, cited: Counter, cand_words: set[str], matn_only: bool) -> float:
        total = shared = 0.0
        for w, c in cited.items():
            if matn_only and (w in self.isnad_words or len(w) < 2):
                continue
            x = self._weight(w) * c
            total += x
            if w in cand_words:
                shared += x
        return shared / total if total else 0.0

    def matn_weight(self, cited: Counter) -> float:
        return sum(self._weight(w) * c for w, c in cited.items() if w not in self.isnad_words and len(w) >= 2)

    def companions(self, norm: str) -> set[str]:
        return {w for w in norm.split() if w in self.lexicon}


def resolve_citation(scorer: Scorer, cited_list, candidates, via_hint: str | None):
    """cited_list: [(hadith_id, normalized_text)] — one entry, or every
    sub-narration of an Abd al-Baqi group for Muslim. Returns
    (winning candidate, cited_id, full, matn) or (None, reason)."""
    hint = via_hint_words(via_hint) if via_hint else []
    best_per_candidate: dict[tuple[str, str], dict] = {}
    for cited_id, cited_norm in cited_list:
        cited = Counter(cited_norm.split())
        mw = scorer.matn_weight(cited)
        cited_comp = scorer.companions(cited_norm)
        for cand in candidates:
            cand_words = set(cand[2].split())
            # "بهذا الإسناد" means the same companion: a Muslim sub-narration
            # (or any cited text) naming companions, none of which the Musnad
            # hadith names, is a different chain — e.g. Abu Hurayra's version
            # of Jabir's "أمرت أن أقاتل الناس" under the same Abd al-Baqi number.
            cand_comp = scorer.companions(cand[2])
            if cited_comp and cand_comp and not (cited_comp & cand_comp):
                continue
            full = scorer.containment(cited, cand_words, matn_only=False)
            row = {
                "cand": cand, "cited_id": cited_id, "full": full, "matn_w": mw,
                "matn": scorer.containment(cited, cand_words, matn_only=True),
                "rank": full + (VIA_HINT_TIEBREAK if hint and any(w in cand_words for w in hint) else 0.0),
            }
            key = (cand[0], cand[1])
            if key not in best_per_candidate or row["rank"] > best_per_candidate[key]["rank"]:
                best_per_candidate[key] = row
    if not best_per_candidate:
        return None, "companion_mismatch"
    ranked = sorted(best_per_candidate.values(), key=lambda r: -r["rank"])
    top = ranked[0]
    if top["matn_w"] >= MIN_MATN_WEIGHT:
        if top["full"] < FULL_THRESHOLD:
            return None, "low_full"
        if top["matn"] < MATN_THRESHOLD:
            return None, "low_matn"
    elif top["full"] < BACKREF_FULL_THRESHOLD:
        return None, "low_full_backref"
    if len(ranked) > 1 and top["rank"] - ranked[1]["rank"] < MARGIN:
        return None, "ambiguous"
    return top, "ok"


def sibling_similarity(scorer: Scorer, a_norm: str, b_norm: str) -> float:
    """Matn containment in whichever direction is higher — one of two
    same-report texts is often an abridgement of the other."""
    a, b = Counter(a_norm.split()), Counter(b_norm.split())
    return max(
        scorer.containment(a, set(b), matn_only=True),
        scorer.containment(b, set(a), matn_only=True),
    )


def main() -> None:
    if len(sys.argv) not in (4, 5, 6):
        print(__doc__)
        sys.exit(1)
    citations_path, raw_pages_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    source = sys.argv[4] if len(sys.argv) >= 5 else "Musnad Ahmad"
    tirmidhi_pages_path = sys.argv[5] if len(sys.argv) == 6 else None
    if source in RISALA_TIRMIDHI_NUMBERING_SOURCES and not tirmidhi_pages_path:
        sys.exit(f"{source} cites Tirmidhi by the Risala edition's numbering — pass its pages file as the 5th argument")

    with open(citations_path, encoding="utf-8") as f:
        citations = json.load(f)
    print(f"loaded {len(citations)} citations (source: {source})")

    with hadith_db_session() as session:
        cited_index = load_cited_hadith_index(session)
        number_index = load_number_index(session)

        musnad_coll = session.scalar(select(HadithCollection).filter_by(name="Musnad Ahmad"))
        musnad_books = session.scalars(select(HadithBook).filter_by(collection_id=musnad_coll.id)).all()
        headings = [b.name_ar for b in musnad_books if b.name_ar]

    print("fetching Sahih Muslim's Abd al-Baqi numbering...")
    muslim_groups = load_muslim_abd_al_baqi_groups(cited_index["Sahih Muslim"])
    print("building corpus statistics...")
    idf, isnad_words, unseen_idf = build_corpus_stats(cited_index)
    scorer = Scorer(idf, isnad_words, unseen_idf, build_companion_lexicon(headings, cited_index))

    # page_id -> [(key1, key2, normalized_text)], and a resolver from the
    # winning candidate back to the hadith id that gets linked.
    pages = load_pages(raw_pages_path)
    # true page order — the Musnad Ahmad export is NOT stored in page order
    # (5 out-of-order segments; see docs Section 5)
    pages.sort(key=lambda p: int(p["id"].split("-")[1]))
    entries, _ = parse_book(pages)
    if source == "Musnad Ahmad":
        print("mapping Musnad Ahmad entries to our rows by number (+ content for shared numbers)...")
        mapped = map_musnad_entries_to_db(entries, number_index["Musnad Ahmad"])
    else:
        print(f"mapping the {source} edition's entries to our rows...")
        mapped = map_edition_entries_to_db(entries, cited_index[source], scorer)
    print(f"mapped {len(mapped)} of {len(entries)} edition entries to our {source} rows")
    page_candidates: dict[str, list[tuple[str, str, str]]] = {}
    for e in mapped:
        for pid in e["page_ids"]:
            page_candidates.setdefault(pid, []).append((e["serial"], str(e["hadith_id"]), e["text_norm"]))

    def source_hadith_id(cand) -> int | None:
        return int(cand[1])
    print(f"indexed {len(page_candidates)} pages with hadith content")

    risala_tirmidhi_to_db: dict[str, tuple[int, str]] = {}
    if source in RISALA_TIRMIDHI_NUMBERING_SOURCES:
        print("mapping the Risala Tirmidhi's numbering to our Tirmidhi rows...")
        tir_pages = load_pages(tirmidhi_pages_path)
        tir_pages.sort(key=lambda p: int(p["id"].split("-")[1]))
        tir_entries, _ = parse_book(tir_pages)
        tir_by_id = {hid: (hid, norm) for hid, norm in cited_index["Jami At-Tirmidhi"].values()}
        for e in map_edition_entries_to_db(tir_entries, cited_index["Jami At-Tirmidhi"], scorer):
            m = re.match(r"\s*([٠-٩]+)", e["serial"])
            if m and m.group(1) not in risala_tirmidhi_to_db:
                risala_tirmidhi_to_db[m.group(1)] = tir_by_id[e["hadith_id"]]
        print(f"  {len(risala_tirmidhi_to_db)} Risala Tirmidhi numbers mapped")

    collection_of_id = {hid: name for name, rows in cited_index.items() for hid, _ in rows.values()}

    resolved = []
    reasons: Counter[tuple[str, str]] = Counter()
    for c in citations:
        if c["collection"] == source:
            continue  # a book citing itself ("سلف برقم"-style) is not a cross-collection link
        if source == "Musnad Ahmad" and c["collection"] == "Sunan al-Kubra":
            # Musnad Ahmad's volumes (1993-2001) predate the Risala Sunan
            # al-Kubra (2001) we hold and cite an older edition's numbering:
            # 5/5 sampled citations landed on unrelated hadith, 1,507 of
            # 1,904 failed the companion guard. Excluded, not guessed at.
            reasons[(c["collection"], "excluded_other_edition_numbering")] += 1
            continue
        if c["collection"] == "Sahih Muslim":
            cited_list = muslim_groups.get(int(to_western_digits(c["numbers"][0])), [])
        elif c["collection"] == "Jami At-Tirmidhi" and source in RISALA_TIRMIDHI_NUMBERING_SOURCES:
            hit = risala_tirmidhi_to_db.get(c["numbers"][0])
            cited_list = [hit] if hit else []
        else:
            targets = NASAI_NUMBER_MEANS.get(source, [c["collection"]]) if c["collection"] == "Sunan an-Nasa'i" else [c["collection"]]
            cited_list = []
            for target in targets:
                for number in c["numbers"]:
                    key = number if target in EASTERN_DIGIT_COLLECTIONS else to_western_digits(number)
                    found = number_index.get(target, {}).get(key, [])
                    if found:
                        cited_list = cited_list + found
                        break
        candidates = candidates_for_page(page_candidates, c["page_id"])
        if not cited_list or not candidates:
            reasons[(c["collection"], "cited_not_in_db" if not cited_list else "no_candidates")] += 1
            continue
        top, reason = resolve_citation(scorer, cited_list, candidates, c.get("via_hint"))
        if top is None:
            reasons[(c["collection"], reason)] += 1
            continue
        source_hid = source_hadith_id(top["cand"])
        if source_hid is None:
            reasons[(c["collection"], "no_source_id")] += 1
            continue
        reasons[(c["collection"], "ok")] += 1
        resolved.append(
            {
                "musnad_hadith_id": source_hid,  # the SOURCE edition's hadith (key name kept for continuity)
                "musnad_serial": top["cand"][0],
                "musnad_heading": top["cand"][1],
                "cited_collection": collection_of_id.get(top["cited_id"], c["collection"]),
                "cited_hadith_id": top["cited_id"],
                "cited_number": c["numbers"],
                "kind": c.get("kind", "same_isnad"),
                "full": round(top["full"], 4),
                "matn": round(top["matn"], 4),
                "span_text": c["span_text"],
            }
        )

    for (coll, reason), n in sorted(reasons.items()):
        print(f"  {coll:18} {reason:20} {n}")
    pairs = {(r["musnad_hadith_id"], r["cited_hadith_id"]) for r in resolved}
    print(f"resolved citations: {len(resolved)}  unique (musnad, cited) pairs: {len(pairs)}")

    with hadith_db_session() as session:
        all_ids = {i for p in pairs for i in p}
        text_by_id = {h.id: h.text_ar for h in session.scalars(select(Hadith).where(Hadith.id.in_(all_ids)))}
    norm_by_id = {i: normalize(t or "") for i, t in text_by_id.items()}

    # Hub links (Musnad hadith <-> each cited hadith), plus a direct link
    # between two cited hadith only when they are the same report — NOT a
    # blanket clique (see SIBLING_THRESHOLD). Written as 2-member clusters,
    # which apply_atraf.py links exactly pairwise.
    cited_by_musnad: dict[int, set[int]] = {}
    for m, c in pairs:
        cited_by_musnad.setdefault(m, set()).add(c)
    cluster_pairs = set(pairs)
    skipped_siblings = 0
    for m, cited_ids in cited_by_musnad.items():
        for a, b in itertools.combinations(sorted(cited_ids), 2):
            if sibling_similarity(scorer, norm_by_id[a], norm_by_id[b]) >= SIBLING_THRESHOLD:
                cluster_pairs.add((a, b))
            else:
                skipped_siblings += 1
    print(f"links: {len(pairs)} hub + {len(cluster_pairs) - len(pairs)} sibling "
          f"({skipped_siblings} dissimilar sibling pairs left hub-only)")

    # Review file: grouped by Musnad hadith, lowest-scoring first — the few
    # borderline calls a reviewer should read, not thousands of easy ones.
    by_musnad: dict[int, list[dict]] = {}
    for r in resolved:
        by_musnad.setdefault(r["musnad_hadith_id"], []).append(r)
    grouped = sorted(by_musnad.items(), key=lambda kv: min(r["matn"] for r in kv[1]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{len(resolved)} resolved citations across {len(grouped)} Musnad Ahmad hadith, "
                "sorted lowest matn score first.\n\n")
        for musnad_hid, entries in grouped:
            first = entries[0]
            f.write(f"##### Musnad Ahmad #{first['musnad_serial']} (id={musnad_hid}, {first['musnad_heading']})\n")
            f.write(f"  {text_by_id.get(musnad_hid, '?')}\n\n")
            for r in sorted(entries, key=lambda r: r["matn"]):
                f.write(f"  --- full {r['full']} / matn {r['matn']} ---\n")
                f.write(f"  {r['cited_collection']} #{'/'.join(r['cited_number'])} (id={r['cited_hadith_id']}):\n")
                f.write(f"    {text_by_id.get(r['cited_hadith_id'], '?')}\n")
                f.write(f"  citation text: {r['span_text']}\n\n")
            f.write("\n")
    with open(out_path + ".pairs.json", "w", encoding="utf-8") as f:
        json.dump(resolved, f, ensure_ascii=False, indent=1)
    with open(out_path + ".clusters.json", "w", encoding="utf-8") as f:
        json.dump([{"hadith_ids": sorted(p)} for p in sorted(cluster_pairs)], f, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}, .pairs.json and .clusters.json")


if __name__ == "__main__":
    main()

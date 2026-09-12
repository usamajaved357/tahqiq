"""Phase 2 cross-referencing: match parsed atraf entries (from
parse_atraf.py) against our own ingested hadith text, and produce a
human-readable review file. Writes nothing to any database — see
apply_atraf.py for that, which only runs after a human has reviewed this
script's output (per docs/project-documentation.md Section 13 Phase 2).

Matching strategy: for each atraf entry citing 2+ collections, resolve each
citation's classical symbol to one of our six collections (symbols for
Mizzi's supplementary sources, which we don't have, are skipped rather than
guessed), then look for a hadith in that collection whose Arabic text starts
with the entry's tarf (diacritics-normalized). Exact prefix match only — no
fuzzy matching — and short tarfs are skipped rather than risking a match on
a common opening phrase shared by many hadith.

Usage: python -m scripts.ingestion.match_atraf <parsed.json> <review_output.txt>
"""
import json
import math
import re
import sys
from collections import Counter
from difflib import SequenceMatcher

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session

MIN_TARF_LEN = 15  # characters, after normalization — below this, too risky to trust a prefix match
MIN_WORD_LEN = 4  # for the fuzzy pass's inverted index — skip short/common words as anchors
FUZZY_THRESHOLD = 0.78  # longest-common-substring / tarf length
FUZZY_MARGIN = 0.1  # best candidate must beat the runner-up by this much, or be the only one above threshold

# --- Pass 3: order-independent, rarity-weighted word overlap ---------------
# The LCS-based fuzzy pass above only credits ONE contiguous matching span,
# so it structurally cannot recognize two texts that say the same thing with
# words in a different order (e.g. "the Messenger of Allah took a ring" vs
# "took, the Messenger of Allah, a ring") — confirmed on real cases where the
# LCS score was suppressed (0.39) for a citation that reads, word-for-word,
# almost identically once order is ignored. This pass scores by how much of
# the tarf's WEIGHT (not count) of words is found anywhere in the candidate,
# where each word is weighted by its rarity (IDF) in that collection — so
# two hadith matching only on boilerplate ("قال رسول الله صلى الله عليه
# وسلم", present in nearly every hadith) score near zero, while matching on
# the distinctive content words of the report scores high regardless of
# word order.
OVERLAP_THRESHOLD = 0.85  # weighted containment of tarf's words in the candidate
OVERLAP_MARGIN = 0.08
OVERLAP_ANCHOR_COUNT = 6  # top-N highest-IDF tarf words used to narrow candidates
OVERLAP_MIN_DISTINCTIVE_WORDS = 3  # guard against short/generic tarfs matching on noise


def build_idf(candidates: list[tuple[int, str, str, str, list[int]]]) -> dict[str, float]:
    """word -> IDF weight within one collection's hadith texts. Common
    boilerplate (isnad phrases, "the Messenger of Allah said") appears in
    nearly every document and gets weighted near zero; rare, content-bearing
    words get weighted high — this is what lets order-independent overlap
    scoring tell "same report, reordered" apart from "two unrelated hadith
    that both mention the Prophet"."""
    n_docs = len(candidates)
    doc_freq: Counter[str] = Counter()
    for hid, num, orig, norm, idx_map in candidates:
        doc_freq.update(set(norm.split()))
    return {w: math.log((n_docs + 1) / (df + 1)) + 1.0 for w, df in doc_freq.items()}


def weighted_containment(
    tarf_words: Counter, cand_words: Counter, idf: dict[str, float], median_idf: float
) -> tuple[float, int]:
    """Returns (score, distinctive_word_count) where score is the IDF-weighted
    fraction of the tarf's own words found in the candidate (containment, not
    symmetric similarity — the candidate's stored text is the full hadith and
    is expected to be longer than Mizzi's quoted opening fragment).

    median_idf is passed in rather than computed here: sorting an entire
    collection's vocabulary to find its median is O(V log V), and this
    function is called once per candidate for every still-unresolved
    citation — recomputing that sort per candidate (as an earlier version of
    this function did) made a few-thousand-word vocabulary sort run tens of
    thousands of times, which is what actually made the very first version
    of this pass impractically slow (confirmed: killing that run and
    reprofiling showed this, not the Counter rebuilding, as the dominant
    cost)."""
    shared_weight = 0.0
    total_weight = 0.0
    distinctive = 0
    for w, c in tarf_words.items():
        weight = idf.get(w, median_idf)
        total_weight += weight * c
        if w in cand_words:
            shared_weight += weight * min(c, cand_words[w])
            if weight >= median_idf:
                distinctive += 1
    score = shared_weight / total_weight if total_weight else 0.0
    return score, distinctive


def build_word_counts(candidates: list[tuple[int, str, str, str, list[int]]]) -> list[Counter]:
    """Precompute each candidate's word-count Counter once. overlap_find_match
    is called once per still-unresolved citation (thousands of calls), and
    each call scores a narrowed candidate set — recomputing Counter(norm.split())
    from scratch inside that hot loop redoes the same tokenization work for the
    same hadith over and over across calls. Computing it once per hadith here,
    aligned by index with `candidates`, turned an impractically slow run into
    a fast one without changing any scoring behavior."""
    return [Counter(norm.split()) for hid, num, orig, norm, idx_map in candidates]


def overlap_find_match(
    tarf_norm: str,
    candidates: list[tuple[int, str, str, str, list[int]]],
    word_index: dict[str, set[int]],
    idf: dict[str, float],
    cand_word_counts: list[Counter],
    median_idf: float,
) -> tuple[int, str, float] | None:
    """Order-independent counterpart to fuzzy_find_match(). Narrows candidates
    using the tarf's highest-IDF (most distinctive) words rather than its
    longest words, then ranks by weighted word-overlap instead of longest
    contiguous substring."""
    tarf_words = Counter(tarf_norm.split())
    ranked_words = sorted(
        (w for w in tarf_words if len(w) >= MIN_WORD_LEN),
        key=lambda w: -idf.get(w, median_idf),
    )
    anchor_words = ranked_words[:OVERLAP_ANCHOR_COUNT]
    if not anchor_words:
        return None

    candidate_positions: set[int] = set()
    for w in anchor_words:
        candidate_positions |= word_index.get(w, set())
    if not candidate_positions:
        return None

    scored = []
    for i in candidate_positions:
        hid, num, orig, norm, idx_map = candidates[i]
        cand_words = cand_word_counts[i]
        score, distinctive = weighted_containment(tarf_words, cand_words, idf, median_idf)
        if distinctive >= OVERLAP_MIN_DISTINCTIVE_WORDS:
            scored.append((score, hid, num))
    scored.sort(reverse=True)

    if not scored or scored[0][0] < OVERLAP_THRESHOLD:
        return None
    if len(scored) > 1 and (scored[0][0] - scored[1][0]) < OVERLAP_MARGIN:
        return None  # too close to the runner-up — don't guess
    return scored[0][1], scored[0][2], scored[0][0]


# --- Companion cross-check ---------------------------------------------
# Text-similarity scoring alone can't tell "same report, different wording"
# apart from "different report that happens to state the same fixed legal
# ruling" — confirmed on real data: a citation attributed to Ubada ibn
# al-Samit matched, purely on shared opening text, three hadith actually
# narrated by Fadala ibn Ubayd and Abu Sa'id al-Khudri (a formulaic
# gold-for-gold ruling independently narrated by several companions). Mizzi
# always names the companion for every citation — that's information the
# text-similarity passes above never look at. This check is deliberately
# name-level, not full-isnad-level: two books legitimately transmit the same
# companion's report via different immediate narrators (e.g. Bukhari via
# Ibn Tawus vs. Muslim/Abu Dawud via Malik, both from Abu Huraira) — that's
# not an error, so requiring the whole chain to match would reject good
# matches. Requiring just the named companion to appear somewhere in the
# hadith's isnad catches the real failure (wrong companion entirely) without
# rejecting legitimate multi-route narrations of the same companion's report.
FILLER_RELATION_RE = re.compile(r"^(أبيه|جده|عمه|أمه|أخيه|ابنه|جدته|عمته|خاله)\s+")


def extract_companion_name(companion_field: str) -> tuple[str, str | None] | None:
    """Mizzi's companion field is a chain description ending in the sahabi
    (e.g. "X، عن Y، عن Z" -> Z is the companion). Returns (companion_name,
    parent_name) where parent_name is the narrator immediately above the
    companion in the same chain (Y above), or None if there is none — needed
    because hadith isnads very often name that companion only implicitly, as
    "...، عن أبيه" ("...، from HIS FATHER") once the parent narrator's own
    name already establishes who the father is (e.g. "سالم، عن أبيه" for
    Salim ibn Abdullah ibn Umar means "from Ibn Umar" without ever spelling
    out "ابن عمر"). Returns None when the chain is deliberately ambiguous at
    the companion level (e.g. "عن بعض أزواج النبي" — "from one of the
    Prophet's wives", left unnamed) — that case needs a human to check, not
    a guessed name that could produce a false reject."""
    field = companion_field.strip()
    field = re.sub(r"\[|\]", "", field)  # editorial brackets, not part of the name
    field = re.sub(r"-[^-]*-", " ", field)  # strip parenthetical asides
    # "ومن مسند X" ("and from the musnad of X") headings name the companion
    # right after "مسند" — any following "، عن جده/أبيه النبي" clause just
    # clarifies X narrates straight from the Prophet, it isn't a second,
    # different companion, so take only the part before the first "عن"
    m = re.match(r"^ومن\s+مسند\s+(.+)$", field)
    if m:
        name = re.split(r"،?\s*عن\s+", m.group(1))[0].strip()
        return (name, None) if name else None
    parts = [p.strip() for p in re.split(r"،?\s*عن\s+", field) if p.strip()]
    if not parts:
        return None
    last = parts[-1]
    if re.search(r"\bبعض\b", last):
        return None
    if re.fullmatch(r"(النبي|رسول الله)( ﷺ)?", last):
        return None
    # "عن أبيه سعد" ("from his father Sa'd") — the relational word describes
    # HOW the previous narrator relates to this one, it's not part of this
    # companion's own name and won't appear verbatim in the hadith's isnad
    last = FILLER_RELATION_RE.sub("", last).strip()
    if not last:
        return None
    parent = parts[-2] if len(parts) >= 2 else None
    return last, parent


STRUCTURAL_LINK_WORDS = {"بن", "ابن", "بنت", "ابنة", "ابي", "ابو"}

# "الله" is part of "عبد الله" (Abdullah) — one of the most common companion
# names (Ibn Umar, Ibn Abbas, Ibn Amr, Ibn Mas'ud, Ibn al-Zubayr are all
# "Abdullah ibn X") — but it is USELESS as a distinguishing check word: it
# also appears in virtually every hadith regardless of companion, via
# "رسول الله" / "صلى الله عليه وسلم". Confirmed on real data: a citation for
# "عبد الله بن عمرو" (Abdullah ibn Amr) matched a hadith actually narrated
# by "ابن عمر" (Ibn Umar, Abdullah ibn UMAR — a different person entirely)
# and passed anyway, purely because "الله" — picked as one of the two
# "distinctive" words for "عبد الله بن عمرو" — is present in essentially
# every hadith's boilerplate "the Messenger of Allah" phrase, independent of
# which companion the text is actually about.
UNIVERSAL_WORDS = {"الله"}


def _distinctive_words(name: str) -> list[str]:
    """Picks up to 2 words to check for. Arabic names are conventionally
    [given name] بن [father] ... [tribal/geographic nisba], and a later
    brief citation is far more likely to keep the given name than a trailing
    nisba — confirmed on real data: "سفيان بن أبي زهير الأزدي الشنوي" only
    ever recurs as "سفيان بن أبي زهير" in actual hadith isnads; picking the
    top-2 LONGEST words instead (as an earlier version of this function did)
    selected "الأزدي"/"الشنوي" — both real words, neither of which the
    matched (and genuinely correct) hadith ever repeats — and false-rejected
    it. So the first substantive word (the ism) is always kept, plus the
    single longest word among the rest, rather than ranking purely by
    length. Pure linking/kunya words ("بن"/"ابن"/"بنت"/"ابنة"/"أبي"/"أبو")
    are structural, not identifying, and are never picked on their own — a
    companion cited as just "أبي سعيد" (Abu Sa'id) needs "سعيد" to do the
    actual distinguishing, since "أبي" alone recurs in dozens of unrelated
    companions' kunyas (Abu Huraira, Abu Bakr, Abu al-Darda', ...) and would
    make the check pass against almost any hadith with any "Abu X" narrator
    mentioned anywhere in its isnad."""
    words = [
        w for w in normalize(name).split()
        if len(w) >= 3 and w not in STRUCTURAL_LINK_WORDS and w not in UNIVERSAL_WORDS
    ]
    if not words:
        # The whole name reduced to nothing once kunya/linking words were
        # excluded — confirmed on real data: the companion "أُبَيّ" (Ubayy
        # ibn Ka'b, a real proper name) normalizes to the exact same string
        # as the generic kunya word "أبي" ("my father" / the Abu- prefix),
        # so excluding it here would leave zero words to check and — since
        # check_companion_in_text treats "nothing distinctive" as "don't
        # false-reject" — silently turn into an unconditional pass, masking
        # a real mismatch (that specific cluster's second hadith was
        # actually Ibn Abbas's report, not Ubayy's, and this bug let it
        # through). Falling back to the raw word here (without the kunya
        # exclusion) keeps a real check in place for this edge case, at the
        # cost of the same weak-word risk the exclusion exists to prevent —
        # better than no check at all.
        words = [w for w in normalize(name).split() if len(w) >= 3 and w not in UNIVERSAL_WORDS]
        if not words:
            return []
    first = words[0]
    rest = sorted(words[1:], key=lambda w: (-len(w), w))
    result = [first]
    if rest and rest[0] != first:
        result.append(rest[0])
    return result


def check_companion_in_text(name_and_parent: tuple[str, str | None], hadith_text: str) -> bool:
    """True if the companion is plausibly present in the matched hadith's
    text — the isnad always precedes the matn in our stored text_ar, so a
    substring check against the whole text is sufficient. Requires only ONE
    of the name's top-2 distinctive words to appear, not both: classical
    citations routinely drop a well-known companion's disambiguating nasab
    once the name is unambiguous in context (e.g. a hadith's isnad naming
    just "سهل" where Mizzi's fuller citation says "سهل بن سعد" — the
    father's name is context-dependent and legitimately omitted) —
    requiring every word of the full name would false-reject those
    correctly-matched citations. Requiring at least one still rejects the
    real failure mode this check exists for (a citation attributed to
    "عبادة بن الصامت" matching a hadith from a completely different
    companion contains NEITHER "عبادة" nor "الصامت" at all).

    Falls back to checking "[parent's name] ... أبيه" (the parent narrator's
    name followed by "his father") when the companion's own name isn't
    found — an isnad naming the parent and then saying "من أبيه" refers to
    this exact companion without ever spelling their name out (confirmed on
    real data: three separate Ibn Umar citations via "سالم، عن أبيه" were
    false-flagged before this fallback was added)."""
    companion_name, parent_name = name_and_parent
    text_norm = normalize(hadith_text)
    distinctive = _distinctive_words(companion_name)
    if not distinctive:
        return True  # nothing distinctive enough to check — don't false-reject
    if any(w in text_norm for w in distinctive):
        return True
    if parent_name:
        parent_distinctive = _distinctive_words(parent_name)
        if parent_distinctive and any(w in text_norm for w in parent_distinctive) and "ابيه" in text_norm:
            return True
    return False


CODE_TO_COLLECTION = {
    "خ": ["Sahih al-Bukhari"],
    "م": ["Sahih Muslim"],
    "د": ["Sunan Abu Dawud"],
    "ت": ["Jami At-Tirmidhi"],
    "س": ["Sunan an-Nasa'i"],
    "ق": ["Sunan Ibn Majah"],
    "ع": [
        "Sahih al-Bukhari", "Sahih Muslim", "Sunan Abu Dawud",
        "Jami At-Tirmidhi", "Sunan an-Nasa'i", "Sunan Ibn Majah",
    ],
    # سي, ي, ح, and other combinations reference Mizzi's supplementary works
    # (e.g. Nasa'i's 'Amal al-Yawm wa'l-Laylah) — not in our DB, skipped.
}


def clean_tarf(tarf: str) -> str:
    """Tuhfat al-Ashraf quotes only an opening fragment, often trailing off
    with ellipsis dots ("... الحديث") meaning "and the hadith continues" —
    that trailing text never appears in our full hadith text, so it has to
    be cut before matching, not treated as part of the quoted content."""
    m = re.search(r"\.{2,}", tarf)
    if m:
        tarf = tarf[: m.start()]
    tarf = re.sub(r"[.\s]*\b(الحديث|موقوف)\b.*$", "", tarf)
    # Mizzi's own editorial shorthand ("- مختصر" = "[abridged]") is his note
    # about the citation, not part of the quoted hadith text — left in, it
    # dilutes the match score against the real (unabridged) stored text.
    tarf = re.sub(r"[-\s]*مختصر\s*\.?\s*$", "", tarf)
    return tarf.strip()


DIACRITIC_RE = re.compile(r"[ً-ْٰٕ-ٟ]")

# Ashraf's source text (and our tarf extraction from it) writes "the Prophet"
# using this single ligature character; our own stored hadith text always
# spells the phrase out in full. Left unexpanded, this single-character vs.
# multi-word mismatch sits right in the middle of most citations and splits
# an otherwise near-identical match into two shorter pieces — since scoring
# only credits one contiguous matching span, a true near-exact match reads
# as a bad one (confirmed by manually reading diagnostic samples where the
# tarf and stored text were essentially word-for-word identical except for
# this substitution, e.g. "ﷺ" vs "صلى الله عليه وسلم"). Expanding both sides
# to the same spelled-out form before scoring fixes this without weakening
# match precision — we're not adding tolerance, just fixing an artificial
# gap between two things that already mean the same thing.
SALLALLAHU_RE = re.compile(r"ﷺ")


def _expand_sallallahu(s: str) -> str:
    return SALLALLAHU_RE.sub(" صلى الله عليه وسلم ", s)


def normalize(s: str) -> str:
    s = _expand_sallallahu(s)
    s = DIACRITIC_RE.sub("", s)  # strip diacritics
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return s


def normalize_with_map(s: str) -> tuple[str, list[int]]:
    """Same transformation as normalize(), but also returns idx_map where
    idx_map[j] is the index into the ORIGINAL string s that produced
    normalized char j. Lets the review-file writer locate, in the original
    (undiacritized) text, exactly where a match against the normalized text
    occurred — instead of showing an arbitrary fixed-length prefix that's
    usually just isnad chain and never reaches the matched content."""
    chars, idxs = [], []
    for i, ch in enumerate(s):
        if DIACRITIC_RE.match(ch):
            continue
        if ch == "ﷺ":
            # expand to the spelled-out phrase (see _expand_sallallahu) —
            # every expanded char maps back to this same original index,
            # which is all the display excerpt needs (a wider window here
            # is fine; it's for human review, not for matching).
            for exp_ch in " صلى الله عليه وسلم ":
                chars.append(exp_ch)
                idxs.append(i)
            continue
        chars.append(ch)
        idxs.append(i)

    coll_chars, coll_idxs = [], []
    prev_ws = False
    for ch, idx in zip(chars, idxs):
        if ch.isspace():
            if prev_ws:
                continue
            coll_chars.append(" ")
            coll_idxs.append(idx)
            prev_ws = True
        else:
            coll_chars.append(ch)
            coll_idxs.append(idx)
            prev_ws = False

    start = 0
    while start < len(coll_chars) and coll_chars[start] == " ":
        start += 1
    end = len(coll_chars)
    while end > start and coll_chars[end - 1] == " ":
        end -= 1
    coll_chars, coll_idxs = coll_chars[start:end], coll_idxs[start:end]

    norm_chars = [
        {"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}.get(ch, ch) for ch in coll_chars
    ]
    return "".join(norm_chars), coll_idxs


EXCERPT_CONTEXT = 40  # chars of surrounding original text to show around a match


def excerpt_for_display(tarf_norm: str, orig: str, norm: str, idx_map: list[int]) -> str:
    """Find where tarf_norm actually matched inside norm (exact substring,
    falling back to fuzzy longest-common-substring), then map that span back
    into the ORIGINAL (diacritized) text via idx_map and return a window
    around it — the matn text a reviewer needs to see, not the isnad
    that happens to sit at position 0."""
    pos = norm.find(tarf_norm)
    if pos != -1:
        length = len(tarf_norm)
    else:
        sm = SequenceMatcher(None, tarf_norm, norm, autojunk=False)
        m = sm.find_longest_match(0, len(tarf_norm), 0, len(norm))
        pos, length = m.b, m.size
    if length == 0 or pos >= len(idx_map):
        return orig[:100]
    start_orig = idx_map[pos]
    end_norm = min(pos + length - 1, len(idx_map) - 1)
    end_orig = idx_map[end_norm] + 1
    lo = max(0, start_orig - EXCERPT_CONTEXT)
    hi = min(len(orig), end_orig + EXCERPT_CONTEXT)
    return orig[lo:hi]


def load_hadith_index(session) -> dict[str, list[tuple[int, str, str, str, list[int]]]]:
    """collection_name -> [(hadith_id, hadith_number, original_text_ar, normalized_text_ar, idx_map), ...]"""
    rows = session.execute(
        select(HadithCollection.name, Hadith.id, Hadith.hadith_number, Hadith.text_ar)
        .select_from(Hadith)
        .join(HadithBook, HadithBook.id == Hadith.book_id)
        .join(HadithCollection, HadithCollection.id == HadithBook.collection_id)
    ).all()
    index: dict[str, list[tuple[int, str, str, str, list[int]]]] = {}
    for name, hid, num, text in rows:
        if text is None:
            continue
        norm, idx_map = normalize_with_map(text)
        index.setdefault(name, []).append((hid, num, text, norm, idx_map))
    return index


def find_match(tarf_norm: str, candidates: list[tuple[int, str, str, str, list[int]]]) -> tuple[int, str] | None:
    # Our stored text_ar always starts with the isnad chain ("Haddathana
    # so-and-so...") before the matn — the tarf (Tuhfat al-Ashraf's quoted
    # opening) is matn-only, so it can appear anywhere in the text, not
    # necessarily at the start. Containment, not prefix.
    hits = [(hid, num) for hid, num, orig, norm, idx_map in candidates if tarf_norm in norm]
    if len(hits) == 1:
        return hits[0]
    return None  # zero or ambiguous (2+) matches — don't guess


def build_word_index(candidates: list[tuple[int, str, str, str, list[int]]]) -> dict[str, set[int]]:
    """word -> set of positions (indices into `candidates`) whose text contains it.
    Only indexes words of MIN_WORD_LEN+ chars — short/common words make poor
    anchors and would blow up candidate-set sizes."""
    index: dict[str, set[int]] = {}
    for i, (hid, num, orig, norm, idx_map) in enumerate(candidates):
        for word in set(norm.split()):
            if len(word) >= MIN_WORD_LEN:
                index.setdefault(word, set()).add(i)
    return index


def fuzzy_find_match(
    tarf_norm: str,
    candidates: list[tuple[int, str, str, str, list[int]]],
    word_index: dict[str, set[int]],
) -> tuple[int, str, float] | None:
    """Fallback for when exact substring matching fails. Narrows to a small
    candidate set via the two longest (most distinctive) words in the tarf,
    then scores each candidate by longest-common-substring coverage of the
    tarf. Only accepts a single, clearly-best candidate above threshold —
    never guesses between close contenders."""
    # tiebreak by the word itself (not just length) so anchor-word selection
    # is deterministic — sorting a set() by length alone leaves same-length
    # words ordered by Python's hash-randomized set iteration, which made
    # results vary between runs when the tarf had 2+ same-length longest words
    words = sorted(set(tarf_norm.split()), key=lambda w: (-len(w), w))
    anchor_words = [w for w in words if len(w) >= MIN_WORD_LEN][:2]
    if not anchor_words:
        return None

    candidate_positions: set[int] | None = None
    for w in anchor_words:
        positions = word_index.get(w, set())
        candidate_positions = positions if candidate_positions is None else (candidate_positions & positions)
    if not candidate_positions:
        # intersection empty — fall back to union so a single strong anchor word still gets tried
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
        scored.append((score, hid, num))
    scored.sort(reverse=True)

    if not scored or scored[0][0] < FUZZY_THRESHOLD:
        return None
    if len(scored) > 1 and (scored[0][0] - scored[1][0]) < FUZZY_MARGIN:
        return None  # too close to the runner-up — don't guess
    return scored[0][1], scored[0][2], scored[0][0]


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    clusters_json_path = out_path.rsplit(".", 1)[0] + "_clusters.json"

    with open(in_path, encoding="utf-8") as f:
        parsed = json.load(f)
    entries = parsed["entries"]

    with hadith_db_session() as session:
        index = load_hadith_index(session)

    print("hadith loaded per collection:", {k: len(v) for k, v in index.items()})
    word_indexes = {name: build_word_index(cands) for name, cands in index.items()}
    idf_indexes = {name: build_idf(cands) for name, cands in index.items()}
    median_idf_indexes = {
        name: (sorted(idf.values())[len(idf) // 2] if idf else 1.0) for name, idf in idf_indexes.items()
    }
    word_counts_indexes = {name: build_word_counts(cands) for name, cands in index.items()}
    sys.stdout.flush()

    clusters = []       # exact-only — identical to the already-applied 94
    fuzzy_clusters = []  # newly found, involve at least one fuzzy (LCS) match — needs fresh review
    overlap_clusters = []  # newly found, involve at least one order-independent overlap match
    too_short = 0
    no_resolvable_collection = 0
    unmatched_citations = 0
    fuzzy_recovered = 0
    overlap_recovered = 0

    for entry_i, e in enumerate(entries):
        if entry_i % 1000 == 0:
            print(f"...progress: {entry_i}/{len(entries)} entries processed")
            sys.stdout.flush()
        # Use the entry HEADER's codes (e["codes"], from parse_atraf.py's
        # ENTRY_RE) as the authoritative list of which collections Mizzi
        # cites — not a fresh re-derivation from e["citations"]'s per-citation
        # regex. The per-citation parse is fragile: dense classical prose runs
        # a narrator's name directly into the next citation's code letter with
        # no separator (e.g. "...عن مسدّدم في النكاح" = "...from Musaddad" +
        # "م [Muslim] in An-Nikah", no space), and CITATION_RE's code group
        # would grab the trailing "د" off "مسدّد" along with the real "م",
        # producing a bogus 2-letter "دم" that matches no known collection —
        # silently dropping a real Muslim citation. Confirmed on real data:
        # 5,808 of 17,330 entries (8,620 individual citations) had at least
        # one collection present in the header but missing from the
        # per-citation parse, including entries dropped from matching
        # entirely (e.g. serial ٥, a legitimate Tirmidhi+Nasa'i entry per its
        # header, for which the per-citation parse found zero resolvable
        # codes). The header parse doesn't have this failure mode: it comes
        # from ENTRY_RE's own boundary (up to "حديث"), not from scanning
        # prose for a "في bookname" pattern.
        codes = set(e["codes"])
        resolvable = {c for c in codes if c in CODE_TO_COLLECTION}
        if len(resolvable) < 2:
            if codes and not resolvable:
                no_resolvable_collection += 1
            continue

        tarf_norm = normalize(clean_tarf(e["tarf"]))
        if len(tarf_norm) < MIN_TARF_LEN:
            too_short += 1
            continue

        matched_exact = []
        matched_fuzzy = []
        matched_overlap = []
        for code in resolvable:
            for coll_name in CODE_TO_COLLECTION[code]:
                candidates = index.get(coll_name, [])
                m = find_match(tarf_norm, candidates)
                if m:
                    matched_exact.append((coll_name, m[0], m[1]))
                    continue
                fm = fuzzy_find_match(tarf_norm, candidates, word_indexes.get(coll_name, {}))
                if fm:
                    matched_fuzzy.append((coll_name, fm[0], fm[1], fm[2]))
                    fuzzy_recovered += 1
                    continue
                om = overlap_find_match(
                    tarf_norm,
                    candidates,
                    word_indexes.get(coll_name, {}),
                    idf_indexes.get(coll_name, {}),
                    word_counts_indexes.get(coll_name, []),
                    median_idf_indexes.get(coll_name, 1.0),
                )
                if om:
                    matched_overlap.append((coll_name, om[0], om[1], om[2]))
                    overlap_recovered += 1
                else:
                    unmatched_citations += 1

        all_matched = (
            matched_exact
            + [(c, h, n) for c, h, n, score in matched_fuzzy]
            + [(c, h, n) for c, h, n, score in matched_overlap]
        )
        distinct_hadith = {(coll, hid) for coll, hid, num in all_matched}
        if len(distinct_hadith) < 2:
            continue

        if matched_overlap:
            overlap_clusters.append({"entry": e, "matched": all_matched, "overlap": matched_overlap})
        elif matched_fuzzy:
            fuzzy_clusters.append({"entry": e, "matched": all_matched, "fuzzy": matched_fuzzy})
        else:
            clusters.append({"entry": e, "matched": all_matched})

    print(f"total entries: {len(entries)}")
    print(f"entries skipped (tarf too short): {too_short}")
    print(f"entries skipped (no resolvable collection): {no_resolvable_collection}")
    print(f"citations that found no matching hadith (even after fuzzy+overlap): {unmatched_citations}")
    print(f"citations recovered by the fuzzy (LCS) pass: {fuzzy_recovered}")
    print(f"citations recovered by the overlap (order-independent) pass: {overlap_recovered}")
    print(f"confirmed cross-reference clusters (exact match, already known): {len(clusters)}")
    print(f"NEW clusters found via fuzzy (LCS) matching: {len(fuzzy_clusters)}")
    print(f"NEW clusters found via overlap (order-independent) matching: {len(overlap_clusters)}")

    with open(out_path, "w", encoding="utf-8") as f:
        for c in clusters:
            e = c["entry"]
            tarf_norm = normalize(clean_tarf(e["tarf"]))
            f.write(f"=== serial {e['serial']} | companion: {e['companion']} ===\n")
            f.write(f"tarf: {e['tarf'][:100]}\n")
            for coll_name, hid, num in c["matched"]:
                # excerpt centered on the actually-matched text, not a fixed
                # prefix (which is always isnad, never the matched matn)
                hit = next(
                    ((orig, norm, idx_map) for h, n, orig, norm, idx_map in index[coll_name] if h == hid),
                    None,
                )
                excerpt = excerpt_for_display(tarf_norm, *hit) if hit else ""
                f.write(f"  [{coll_name} #{num}] ...{excerpt}...\n")
            f.write("\n")
    print(f"wrote review file: {out_path}")

    # machine-readable form (hadith ids, not just numbers) for apply_atraf.py
    with open(clusters_json_path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "serial": c["entry"]["serial"],
                    "hadith_ids": sorted({hid for _, hid, _ in c["matched"]}),
                }
                for c in clusters
            ],
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"wrote clusters json: {clusters_json_path}")

    if fuzzy_clusters:
        fuzzy_out_path = out_path.rsplit(".", 1)[0] + "_fuzzy.txt"
        fuzzy_json_path = out_path.rsplit(".", 1)[0] + "_fuzzy_clusters.json"

        with open(fuzzy_out_path, "w", encoding="utf-8") as f:
            for c in fuzzy_clusters:
                e = c["entry"]
                tarf_norm = normalize(clean_tarf(e["tarf"]))
                fuzzy_hids = {hid for _, hid, _, _ in c["fuzzy"]}
                f.write(f"=== serial {e['serial']} | companion: {e['companion']} ===\n")
                f.write(f"tarf: {e['tarf'][:100]}\n")
                for coll_name, hid, num in c["matched"]:
                    hit = next(
                        ((orig, norm, idx_map) for h, n, orig, norm, idx_map in index[coll_name] if h == hid),
                        None,
                    )
                    excerpt = excerpt_for_display(tarf_norm, *hit) if hit else ""
                    score = next((s for _, h, _, s in c["fuzzy"] if h == hid), None)
                    tag = f" [FUZZY score={score:.2f}]" if hid in fuzzy_hids else " [exact]"
                    f.write(f"  [{coll_name} #{num}]{tag} ...{excerpt}...\n")
                f.write("\n")
        print(f"wrote NEW fuzzy review file (needs your review): {fuzzy_out_path}")

        with open(fuzzy_json_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "serial": c["entry"]["serial"],
                        "hadith_ids": sorted({hid for _, hid, _ in c["matched"]}),
                    }
                    for c in fuzzy_clusters
                ],
                f,
                ensure_ascii=False,
                indent=1,
            )
        print(f"wrote NEW fuzzy clusters json: {fuzzy_json_path}")

    if overlap_clusters:
        overlap_out_path = out_path.rsplit(".", 1)[0] + "_overlap.txt"
        overlap_json_path = out_path.rsplit(".", 1)[0] + "_overlap_clusters.json"

        with open(overlap_out_path, "w", encoding="utf-8") as f:
            for c in overlap_clusters:
                e = c["entry"]
                tarf_norm = normalize(clean_tarf(e["tarf"]))
                overlap_hids = {hid for _, hid, _, _ in c["overlap"]}
                f.write(f"=== serial {e['serial']} | companion: {e['companion']} ===\n")
                f.write(f"tarf: {e['tarf'][:100]}\n")
                for coll_name, hid, num in c["matched"]:
                    hit = next(
                        ((orig, norm, idx_map) for h, n, orig, norm, idx_map in index[coll_name] if h == hid),
                        None,
                    )
                    excerpt = excerpt_for_display(tarf_norm, *hit) if hit else ""
                    score = next((s for _, h, _, s in c["overlap"] if h == hid), None)
                    tag = f" [OVERLAP score={score:.2f}]" if hid in overlap_hids else " [exact/fuzzy]"
                    f.write(f"  [{coll_name} #{num}]{tag} ...{excerpt}...\n")
                f.write("\n")
        print(f"wrote NEW overlap review file (needs your review): {overlap_out_path}")

        with open(overlap_json_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "serial": c["entry"]["serial"],
                        "hadith_ids": sorted({hid for _, hid, _ in c["matched"]}),
                    }
                    for c in overlap_clusters
                ],
                f,
                ensure_ascii=False,
                indent=1,
            )
        print(f"wrote NEW overlap clusters json: {overlap_json_path}")


if __name__ == "__main__":
    main()

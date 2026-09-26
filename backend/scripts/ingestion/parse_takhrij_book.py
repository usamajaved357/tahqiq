"""Parses one Al-Arna'ut/Risala-style critical edition (extracted from Shamela
via the same Lucene store used for Tuhfat al-Ashraf — see
scripts/ingestion/parse_atraf.py's docstring for that extraction method) into
structured hadith entries: chapter/companion heading, hadith number, isnad+matn
text, and the raw footnote text attached to it.

This module only parses structure — it does not touch any database and does
not yet interpret footnote content (grading, cross-references). That's
deliberate: extracting "what text belongs to which hadith" is a precondition
both for ingesting the hadith text itself (scripts/ingestion/ingest_takhrij_book.py)
and for the separate, much higher-stakes citation-classification work, and
each should be verified on its own before being trusted.

Format notes verified against real extracted pages (Abu Dawud, Tirmidhi,
Ibn Majah, Musnad Ahmad, Sunan al-Kubra — all 6 use the same underlying
markup since they come from the same publisher's Shamela editions):
  - A page's "body" field holds the isnad+matn text, with chapter/section
    headings embedded as <span data-type='title'>...</span> markers (same
    convention as Tuhfat al-Ashraf) and footnote-reference markers embedded
    inline as "(¬N)" or plain "(N)" depending on the specific edition.
  - A page's "foot" field holds that page's footnote TEXT, already separated
    by Shamela's own indexer — we don't have to split it out of the body
    ourselves, only associate the right slice of it with the right hadith
    when a page contains more than one.
  - A hadith entry starts at "{number} - {حدثنا|أخبرنا|حدثني|وقال|قال}..." and
    runs until the next such marker (or a chapter heading, or end of book).
    The "وقال"/"قال" forms matter: Musnad Ahmad routinely numbers a new
    hadith that continues the SAME isnad as the one before it (one narrator
    reporting several sayings in a row) without repeating "حدثنا" — missing
    this boundary silently merged a later hadith's text into the previous
    one's entry, confirmed on real data (hadith #8115 absorbing #8226's
    text whole) before this pattern was added. The
    number+verb boundary itself never carries diacritics that would break a
    bare-letter match, but words AROUND it do, so matching is done against a
    diacritic-stripped copy with an index map back to the original — same
    technique as match_atraf.py's normalize_with_map() — to split the
    ORIGINAL (fully-diacritized) text without corrupting it.
  - A hadith with more than one recorded isnad variant is numbered as a
    COMPOSITE "N/ M" (e.g. "١٢٤٥/ ١" then "١٢٤٥/ ٢" for hadith 1245's two
    chains) — confirmed on real data, and a first version of this parser
    that captured only the trailing digit after the slash produced dozens
    of bogus same-serial collisions (e.g. "serial ١" matching 14 unrelated
    hadith from a "N/ ١" pattern each time) by discarding the real leading
    number. The serial pattern below captures the full "N/ M" as one string
    when present, falling back to a bare number otherwise.
  - Musnad Ahmad is organized by companion (headings name a sahabi's
    musnad), the four Sunan-style books by fiqh topic — structurally
    identical to parse for our purposes, since both just use heading spans.
  - The boundary-verb set is NOT limited to حدثنا/أخبرنا/حدثني/وقال/قال.
    Musnad Ahmad's editors use several other classical transmission
    formulas to open a new numbered hadith without repeating "حدثنا":
    وحدثنا/حدثناه/وحدثناه (same verb, prefixed/suffixed), قرأت/قرئ
    ("recited to" / "was recited to" — the qira'ah transmission mode, a
    real alternative to سماع), وبهذا (الإسناد)/وبإسناده/وبه ("and with
    this/his isnad"), وذكر ("and he mentioned"), وجدت (في كتاب أبي)
    ("I found in my father's book" — 'Abdullah ibn Ahmad's own formula
    for hadith he added from his father's papers rather than heard
    directly), وسمعت ("and I heard"), وعن ("and from [narrator]").
    Confirmed real and not a parser artifact: without these, a numbered
    entry using one of these verbs silently appended into the PRECEDING
    hadith's text instead of starting its own (e.g. hadith ٢٠٨٨٥'s stored
    text ran on to include "٢٠٨٨٦ - وبهذا الإسناد..." verbatim before this
    was added). Several superficially similar "N - <word>" occurrences are
    NOT hadith and must NOT be added as triggers — they come from the
    editor's front-matter (numbered lists of manuscript copies, the
    author's other books, biographical notices, editorial-methodology
    notes): نسخة, وروى, كتاب, وأما, وضعنا, وفيه, قطعة. These were checked
    against their actual source context (not assumed) before exclusion.
  - A second, separate composite-serial format was found alongside the
    "N/ M" isnad-variant format: "N م" (and occasionally "N م M"), e.g.
    "٢٢٣٤٥ م" — "م" here abbreviates "متن" (matn), marking a recorded
    variant WORDING of the same base hadith number rather than a variant
    isnad. Confirmed genuine on 41 distinct real occurrences, all followed
    by real isnad/matn text (never front matter). Captured as part of the
    serial for the same reason as "N/ M": discarding it would silently
    collide multiple distinct matn-variants onto one base serial.
  - Also: وقال/قال were previously required to be followed by whitespace
    to avoid matching "قالت"/"قالوا" (a different narrator/grammatical
    person, not a new hadith) — real cases exist where a colon follows
    instead ("وَقَالَ: ..."), so the boundary now accepts whitespace OR a
    colon after these two verbs specifically.
  - Sunan al-Kubra (a different work/editorial team than Musnad Ahmad) uses
    a substantially different verb mix — checked independently against its
    own raw source before assuming Musnad Ahmad's trigger set transferred
    (see docs/HADITH_CROSS_REFERENCING.md's "sharper lesson" — an exhaustive
    DB-vs-parse diff only proves fidelity to whatever the parser produced,
    never that the trigger list is complete for a NEW book). Nasa'i-style
    editions favor أخبرنا/أخبرني over حدثنا, plus وأخبرنا/وأخبرني/وحدثني,
    أخبر (bare, subject-after-verb word order), أنبأنا/أنبأني (classical
    synonym of أخبر*), أملى ("dictated to us"), وفيما ("and among what
    [so-and-so] read to us" — a قراءة-mode variant, NOT to be confused with
    Musnad Ahmad's unrelated front-matter word وفيه), and bare عن (a
    compressed isnad style that starts directly with "from so-and-so" with
    no verb at all).
  - Short/generic triggers (عن, وعن, وبه, bare أخبر) require a trailing
    space/colon rather than a bare substring lookahead. Confirmed necessary,
    not defensive over-caution: an early version used a bare "عن" lookahead
    and it matched inside "عِنَايَة" (interest/care) — a front-matter
    table-of-contents entry in Musnad Ahmad's introduction ("مقدمة
    التحقيق"), not a hadith — because "عن" is a letter-prefix of many
    unrelated words. Every trigger is still gated on a preceding "digit -"
    the source itself printed, so requiring a trailing boundary too does not
    meaningfully reduce recall on genuine hadith, only on this false-positive
    class.
  - A small number of entries (~9 in Sunan al-Kubra, confirmed by checking
    each occurrence's actual context) open with a bare narrator's PROPER
    NAME and no verb at all before a transmission-mode phrase appears later
    in the sentence (e.g. "الحارث بن مسكين، قراءة عليه..."). A generic
    "any name" trigger is unsafe — common name words appear constantly
    mid-isnad and would cause severe over-splitting. Only "الحارث" (74
    occurrences, verified 74/74 to be this one specific narrator, "الحارث
    بن مسكين") is special-cased as a literal trigger; the handful of other
    one-off name-first openings are documented as a known residual
    limitation rather than guessed at generically.
"""
import json
import re
import sys
from dataclasses import dataclass, field

DIGITS = "٠١٢٣٤٥٦٧٨٩"
DIACRITIC_RE = re.compile(r"[ً-ْٰۖ-ۭ]")
TITLE_RE = re.compile(r"<span data-type=['\"]title['\"][^>]*>(.*?)</span>")

# Serial: a base number, optionally with a "/M" isnad-variant suffix or a
# "م[ M]" matn-variant suffix (see module docstring — both are real,
# distinct composite-numbering conventions confirmed on actual data).
_SERIAL = rf"[{DIGITS}]+(?:\s*/\s*[{DIGITS}]+|\s+م(?:\s*[{DIGITS}]+)?)?"

# Boundary-trigger verbs, confirmed genuine against real source context (see
# module docstring for the full list checked and why each false-positive
# candidate — نسخة/وروى/كتاب/وأما/وضعنا/وفيه/قطعة/فهرس/ترتيب/القيام/نبهنا/
# أبو/عبد/علي/حديث (front matter: manuscript-copy lists, bibliographies,
# editorial-methodology notes, and a companion/topic index) — was excluded).
_TRIGGERS = (
    r"حدثنا|أخبرنا|حدثني|حدثنى|حدثناه|وحدثناه|وحدثنا|"
    r"أخبرني|وأخبرنا|وأخبرني|وحدثني|أخبرنيه|أخبر\s|أنبأنا|أنبأني|"
    r"قرأت|قرئ|أملى|وقرأت|وقرأته|"
    r"وبهذا|وبإسناده|وبه[\s:]|"
    r"وذكر|وجدت|سمعت|وسمعت|وعن\s|وفيما|عن\s|وابن|"
    r"وإن|وأن|وكان|ولقد|وكذا|ثم\s*قال|و\"|"
    r"وأحسب|وسألته|وأهديت|وقلت|وكتب|وزائدة|"
    r"وقال[\s:]|قال[\s:]|"
    r"الحارث|والحارث|"
    rf"\.(?:\s*\.){{2,}}"
)

# An editorial bracketed aside can sit between the dash and the real
# trigger verb — confirmed on real data (167 occurrences in Musnad Ahmad
# alone, the single largest boundary-miss category found in this whole
# investigation): either a short attribution "[قال عبد الله بن أحمد]: "
# before the actual verb (e.g. وجدت), or a bare "[" that wraps the ENTIRE
# hadith with its closing "]" far away (e.g. "-[حدثنا ..."). Try the
# short/closed form first; fall back to a bare "[" so the long-wrapping
# form still matches immediately on the verb that follows it. Kept INSIDE
# the lookahead (non-consuming), same as the trigger verb itself, so the
# bracket text is preserved verbatim as part of the new entry's own text —
# the parser only decides WHERE to split, never rewrites content; stripping
# presentation artifacts (footnote markers, brackets) is clean_text()'s job
# at ingestion time, not the parser's.
_BRACKET_ASIDE = r"(?:\[[^\]\n]{1,80}\][\s:]*|\[)?"

# A footnote-style "(N)" marker can also sit between the dash and the verb
# (confirmed real, distinct from the footnote markers stripped from body
# text post-parse — this one interferes with boundary DETECTION itself).
_FOOTNOTE_ASIDE = rf"(?:\(\s*[{DIGITS}]+\s*\)\s*)?"

HADITH_START_RE = re.compile(
    rf"({_SERIAL})\s*-\s*(?={_BRACKET_ASIDE}{_FOOTNOTE_ASIDE}(?:{_TRIGGERS}))"
)


def strip_with_map(s: str) -> tuple[str, list[int]]:
    """Returns (stripped, idx_map) where idx_map[j] is the index into the
    ORIGINAL string `s` that produced stripped[j] — lets a match found in
    the diacritic-free copy be translated back to a split point in the
    original (fully-diacritized) text."""
    chars, idxs = [], []
    for i, ch in enumerate(s):
        if DIACRITIC_RE.match(ch):
            continue
        chars.append(ch)
        idxs.append(i)
    return "".join(chars), idxs


@dataclass
class ParsedHadith:
    serial: str  # this edition's own printed number, as a string of Eastern digits
    heading: str | None  # chapter/companion heading in effect when this hadith started
    text: str = ""  # isnad + matn, original diacritics preserved, footnote markers left in place
    footnote_text: str = ""  # this hadith's OWN footnote content, reassembled from the
    # numbered segments its own inline (N) markers actually reference — see
    # _FootnoteAssembler below, not a naive per-page concatenation.
    page_ids: list[str] = field(default_factory=list)


# Matches this edition's inline footnote-reference markers as they appear in
# BODY text, e.g. "...جَرْحَاهُمْ (٢)." ("(¬٢)" in the Sunan editions).
# Footnotes are numbered PER PAGE: the markers on a page run (١), (٢), (٣)…
# across however many hadith the page holds, and its footnote area holds
# segments (١), (٢), (٣)… (checked 2026-09-26: 99% of Musnad pages with
# markers, the rest are one note referenced twice or a page starting mid-run).
_INLINE_MARKER_RE = re.compile(rf"\(\s*¬?\s*([{DIGITS}]+)\s*\)")
_TO_WESTERN = str.maketrans(DIGITS, "0123456789")
_TO_EASTERN = str.maketrans("0123456789", DIGITS)


def _note_start(foot: str, number: int, pos: int) -> tuple[int, int] | None:
    """The segment "(number)" of a page's footnote area, at or after pos. A
    footnote's own text also cites hadith by number ("وأخرجه البخاري (٧٤١٢)",
    "مسلم (٢٣٨٠) (١٧٢)"), always after a space; a segment start follows the
    end of the previous note directly ("…بهذا الإسناد.(٢)", "…تحريف(٢)",
    "…(٢٦٤٥٢)(١)") or opens the area. Returns (start, end) in foot."""
    for m in re.compile(rf"\(\s*¬?\s*{str(number).translate(_TO_EASTERN)}\s*\)").finditer(foot, pos):
        if m.start() == 0 or not foot[m.start() - 1].isspace():
            return m.start(), m.end()
    return None


class _FootnoteAssembler:
    """Gives each hadith the footnotes its own markers point to (2026-09-26
    rewrite).

    The previous version queued every marker of the book in one FIFO and
    handed each footnote segment to the oldest waiting marker. Any page
    where the two counts differ — a heading's own marker, a segment start
    after a letter ("…وهو تحريف(٢)") that its splitter did not see, footnotes
    printed in the body — shifted every later footnote by one: on Musnad
    Ahmad the queue ran 29 deep at the median and up to 189, so a footnote
    usually reached a hadith ~30 markers away, or none at all (only 3,863 of
    27,797 hadith got one). Here a marker is matched to the segment with
    ITS number on ITS page; nothing carries over between pages except a
    footnote continued at the top of the next page ("= …").
    """

    def __init__(self) -> None:
        self._page_markers: list[tuple[ParsedHadith, int]] = []
        self._by_entry: dict[int, list[list[str]]] = {}  # id(entry) -> footnote cells, in order
        self._last_cell: list[str] | None = None  # the note a "= …" continuation extends
        self.markers_seen = 0
        self.markers_unmatched = 0  # diagnostics: a marker whose number the page's notes lack

    def note_markers(self, entry: ParsedHadith, text_chunk: str) -> None:
        """Call with each piece of ORIGINAL body text of `entry`, in order."""
        for m in _INLINE_MARKER_RE.finditer(text_chunk):
            self._page_markers.append((entry, int(m.group(1).translate(_TO_WESTERN))))

    def consume_page_foot(self, foot: str) -> None:
        """Call once per page, after all of the page's markers were noted."""
        markers, self._page_markers = self._page_markers, []
        self.markers_seen += len(markers)
        foot = foot or ""
        cells: dict[int, list[str]] = {}
        starts = []
        pos, k = 0, 1
        last_marker = max((n for _, n in markers), default=0)
        while True:
            found = _note_start(foot, k, pos)
            if found is not None:
                starts.append((k, found[0]))
                pos = found[1]
            elif k >= last_marker:
                break
            # a number missing from the notes (a misprint: "(١٢" for "(٢)"):
            # its marker stays without a note, the later numbers still split
            k += 1
        lead_end = starts[0][1] if starts else len(foot)
        if foot[:lead_end].lstrip().startswith("=") and self._last_cell is not None:
            self._last_cell[0] += " " + foot[:lead_end].strip().lstrip("= \n")
        for i, (k, at) in enumerate(starts):
            cells[k] = [foot[at : starts[i + 1][1] if i + 1 < len(starts) else len(foot)]]
        if starts:
            self._last_cell = cells[starts[-1][0]]
        for entry, k in markers:
            cell = cells.get(k)
            if cell is None:
                self.markers_unmatched += 1
                continue
            mine = self._by_entry.setdefault(id(entry), [])
            if not any(c is cell for c in mine):
                mine.append(cell)

    def footnote_for(self, entry: ParsedHadith) -> str:
        return "".join(c[0] for c in self._by_entry.get(id(entry), [])).strip()


def parse_book(pages: list[dict]) -> tuple[list[ParsedHadith], list[dict]]:
    """Walk pages in order, tracking the current heading, and split hadith
    text at each numbered start. Footnote text is attributed per-marker via
    `_FootnoteAssembler`, not by naive per-page concatenation (see its
    docstring for why the naive approach silently misattributes footnotes to
    the wrong hadith in the common case of a hadith's body ending mid-page)."""
    entries: list[ParsedHadith] = []
    unparsed_log: list[dict] = []
    assembler = _FootnoteAssembler()

    current_heading: str | None = None
    pending: ParsedHadith | None = None
    buffer = ""
    buffer_pages: list[str] = []

    def finalize() -> None:
        if pending is None:
            return
        pending.text = buffer.strip()
        pending.page_ids = list(buffer_pages)
        entries.append(pending)

    def consume(segment: str, page_id: str) -> None:
        """Split one stretch of page text (containing no titles) at every
        numbered hadith start, extending the hadith in progress first."""
        nonlocal pending, buffer, buffer_pages
        stripped, idx_map = strip_with_map(segment)
        pos = 0
        for m in HADITH_START_RE.finditer(stripped):
            orig_start = idx_map[m.start()] if m.start() < len(idx_map) else len(segment)
            orig_end = idx_map[m.end() - 1] + 1 if m.end() - 1 < len(idx_map) else len(segment)
            chunk = segment[pos:orig_start]
            buffer += chunk
            buffer_pages.append(page_id)
            if pending is not None:
                assembler.note_markers(pending, chunk)
            finalize()
            pending = ParsedHadith(serial=m.group(1), heading=current_heading)
            buffer, buffer_pages = "", []
            pos = orig_end

        tail_chunk = segment[pos:]
        buffer += tail_chunk
        if page_id not in buffer_pages:
            buffer_pages.append(page_id)
        if pending is not None and tail_chunk:
            assembler.note_markers(pending, tail_chunk)

    for page in pages:
        body = page.get("body") or ""
        foot = page.get("foot") or ""
        page_id = page["id"]
        titles = TITLE_RE.findall(body)
        # A page break is a word break: Shamela bodies neither end nor start
        # with whitespace, so appending the next page directly fused the last
        # word of one page to the first of the next ("الظلمات" + "إلى" ->
        # "الظلماتإلى") at 5,879 of Musnad Ahmad's 23,338 page joins
        # (2026-09-26). Arabic print never splits a word across pages. No
        # separator before a page that opens with punctuation ("ميت»" + ",").
        if buffer and not buffer[-1].isspace() and body[:1] and body[0] not in "،,.:؛;)]»!؟?":
            buffer += "\n"

        # Walk the page IN ORDER: text before a title still belongs to the
        # hadith in progress (and may itself start new hadith); each title
        # then closes the current hadith and starts a new section at exactly
        # that point. The earlier version finalized at the page's first title
        # BEFORE reading the text ahead of it (silently dropping the tails of
        # ~10 Musnad Ahmad hadith and 1 in Sunan al-Kubra), and gave every
        # hadith on a multi-title page the page's LAST title (found 2026-09-24).
        pos = 0
        for tm in TITLE_RE.finditer(body):
            consume(body[pos : tm.start()], page_id)
            finalize()
            pending = None
            buffer, buffer_pages = "", []
            current_heading = strip_with_map(tm.group(1))[0].strip()
            pos = tm.end()
        consume(body[pos:], page_id)
        assembler.consume_page_foot(foot)

        if pending is None and not titles and body.strip() and not buffer.strip():
            unparsed_log.append({"id": page_id, "reason": "no heading/hadith context yet"})

    finalize()
    # after the whole book: a hadith usually ends before its page's notes are read
    for e in entries:
        e.footnote_text = assembler.footnote_for(e)
    return entries, unparsed_log


def load_pages(path: str) -> list[dict]:
    pages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            pages.append(json.loads(line))
    return pages


_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670]")
_PAGE_HADITH_START_RE = re.compile(r"[٠-٩]+\s*[-–—]\s")
# A volume's front matter, reprinted at the start of every printed volume:
# the title page, the manuscripts used, the edition's symbols, statistics,
# and al-Sindi's biography of the companion. None of it is hadith text.
_FRONT_MATTER_RE = re.compile(
    r"مسند\s*الإمام\s*[اأ]حمد(?:\s*(?:بن|ابن)\s*حنبل)?\s*\(?\s*١٦٤|النسخ الخطية المعتمدة|اعتمدنا في تحقيق|"
    r"اعتمد في تحقيق هذا الجزء|الرموز المستعملة|عدد الأحاديث الصحيحة|بقلم\s*:?\s*السندي|حقق هذا الجزء|"
    r"الموسوعة الحديثية|المشرف العام|^\s*﷽?\s*استدراك|^\s*﷽\s*$|^\s*﷽?\s*مقدمة التحقيق|^\s*﷽?\s*ترجمة\s"
)
# An editor's label printed just before a title: "[سادس عشر الأنصار]"
# (or the first word of a heading left outside its tag: "أول<span …>مسند الكوفيين")
# (or a surah number before al-Kubra's Tafsir heading: "٧٩ -<span …>سورة النازعات")
_LABEL_BEFORE_TITLE_RE = re.compile(r"^\s*(?:\[[^\]<]{2,40}\]|أول|تتمة|[٠-٩]+\s*[-–—])\s*(?=<span data-type)")
# "تتمة مسند أبي هريرة" — a reminder printed at the start of a volume that
# continues the previous volume's musnad; not a new section, not hadith text.
_CONTINUATION_NOTE_RE = re.compile(r"^\s*﷽?\s*تتمة\s+(?:مسند|حديث)\s[^٠-٩<]{2,80}?(?=[٠-٩]+\s*[-–—]\s)")
_AHMAD_DATES_RE = re.compile(r"\(?\s*١٦٤\s*[-–—ـ]\s*٢٤١\s*هـ?\s*\)?")
# A page holding only a row of dots (". . . . ."), the print's end-of-volume
# / end-of-section separator: 1,863 such pages in Musnad Ahmad, which ran
# into the hadith before them (1,496 hadith ended in a row of dots).
_DOTS_ONLY_RE = re.compile(r"[\s.…·]*\.[\s.…·]*")
# ...and once (Musnad page 25794-5592) a dots row followed by the continuation
# of the previous page's footnote ("= وأخرجه النسائي…") in the BODY, which
# became 1,300 characters of hadith #7120's text: it is footnote text.
_DOTS_THEN_FOOTNOTE_RE = re.compile(r"[\s.…·]*\.[\s.…·]*(?==)")
# Four more Musnad pages (25794-2233, -4318, -7892, -19474) have an empty
# footnote field and their footnotes in the body after "=" (Shamela's mark
# for a note continued from the previous page), so they were stored as the
# end of hadith #٣١١٧, #٥٦٢٠, #١٠٢٠٠, #٢٣٤٣٣. "=" never occurs in hadith text.
_TAG_RE = re.compile(r"<[^>]+>")
# A section heading printed as plain text (not wrapped as a Shamela title):
# "مسند علي بن أبي طالب (١) ﵁", "حديث عقيل بن أبي طالب", "ومن مسند بني هاشم…"
_PLAIN_HEADING_RE = re.compile(
    r"^\s*((?:و?من |بقية |تمام |ومن )?(?:مسند|حديث|أحاديث)\s[^<]{2,200}?)\s*(?=(?:[٠-٩]+\s*[-–—]\s)|$)"
)
# The editors' end-of-section notes ("آخر مسند أبي هريرة ﵁", "هذا آخر مسند
# البصريين") — editorial, not hadith text.
_END_MARKER_RE = re.compile(r"(?:\[\s*آخر\s+(?:مسند|أحاديث|حديث)\s[^<\]]{2,80}\]|(?:هذا\s+)?آخر\s+(?:مسند|أحاديث|حديث)\s[^.<\]،]{2,80})\s*\.?\s*$")


def prepare_pages(pages: list[dict]) -> list[dict]:
    """Normalize a raw Shamela page export before parse_book() (2026-09-24).

    1. True page order: the Musnad Ahmad export is stored as 5 out-of-order
       segments, and parse_book() reads in list order.
    2. Volume front matter is dropped — previously glued onto the last
       hadith of each volume (44 Musnad hadith).
    3. Section headings printed as plain text — whole heading pages, or a
       heading line at the very start of a page — become title markup. The
       Shamela export (and its own table of contents) has no title for most
       of the largest musnads ('Ali, Ibn 'Abbas, Ibn 'Umar, Abu Hurayra,
       Anas, Jabir …); the headings exist only as plain text, which the
       parser glued onto the previous hadith instead of starting a section.
    4. Editorial end-of-section notes are removed from the end of a page.
    5. Pages holding only a row of dots (a separator) are emptied; footnote
       text after such a row, or after "=" on a page whose footnote field is
       empty, is moved to the page's footnotes.
    Only page-INITIAL headings are converted: a title mid-page would cut the
    previous hadith's tail from it (see parse_book)."""
    out = []
    in_front_matter = False
    for page in sorted(pages, key=lambda p: int(p["id"].split("-")[1])):
        body = page.get("body") or ""
        if _DOTS_ONLY_RE.fullmatch(body):
            out.append({**page, "body": ""})
            continue
        dots = _DOTS_THEN_FOOTNOTE_RE.match(body)
        if dots:
            out.append({**page, "body": "", "foot": body[dots.end() :] + (page.get("foot") or "")})
            continue
        if not (page.get("foot") or "").strip():
            eq = _TAG_RE.sub(lambda t: " " * len(t.group()), body).find("=")
            if eq >= 0 and body[eq + 1 :].strip():
                page = {**page, "foot": body[eq:]}
                body = body[:eq]
        label = _LABEL_BEFORE_TITLE_RE.match(_DIACRITICS_RE.sub("", body))  # the raw text is vowelled
        if label:
            body = body[_find_original_offset(body, label.end()) :]
        plain = _DIACRITICS_RE.sub("", TITLE_RE.sub(r"\1", body))
        # Imam Ahmad's dates on every volume title page, "(١٦٤ - ٢٤١ هـ)",
        # look exactly like "hadith #164 starts here" to a number-dash test
        # the parser's own hadith-start rule (number + dash + a transmission
        # verb), not a bare "number - ": front matter lists manuscripts as
        # "١ - نسخة المكتبة الظاهرية", which a bare test mistook for hadith
        has_hadith = bool(HADITH_START_RE.search(strip_with_map(_AHMAD_DATES_RE.sub(" ", plain))[0]))
        # only on untagged pages: with title tags present the parser already
        # treats "تتمة …" as a (zero-hadith) heading, and plain-text offsets
        # would not map onto the tagged body
        cont = None if TITLE_RE.search(body) else _CONTINUATION_NOTE_RE.match(plain)
        if cont:
            body = body[_find_original_offset(body, cont.end()) :]
            plain = plain[cont.end() :]
        # Front matter can run over several pages (an editor's introduction
        # to Abu Hurayra's musnad, al-Sindi's biography of Anas …) and only
        # the first page carries a recognizable marker: once in it, drop
        # every page until the first real hadith or heading. Volumes end on
        # a hadith boundary, so nothing of a hadith can be in between.
        if not has_hadith and _FRONT_MATTER_RE.search(plain):
            # a volume title page also LOOKS like a heading ("مسند الإمام
            # أحمد …"), so it must be dropped before the heading test below
            in_front_matter = True
            out.append({**page, "body": ""})
            continue
        starts_section = bool(TITLE_RE.search(body)) or bool(_PLAIN_HEADING_RE.match(plain)) or bool(cont)
        if in_front_matter:
            if has_hadith or starts_section:
                in_front_matter = False
            else:
                out.append({**page, "body": ""})
                continue
        if not TITLE_RE.search(body) and not has_hadith and len(plain.strip()) < 250:
            m = _PLAIN_HEADING_RE.match(plain)
            if m and "حدثنا" not in plain and "قال" not in plain and '"' not in plain:
                out.append({**page, "body": f"<span data-type='title'>{body.strip()}</span>"})
                continue
        if not TITLE_RE.match(body.strip()):
            m = _PLAIN_HEADING_RE.match(plain)
            if m and _PAGE_HADITH_START_RE.search(plain[m.end() : m.end() + 12]):
                cut = _find_original_offset(body, m.end())
                body = f"<span data-type='title'>{body[:cut].strip()}</span>{body[cut:]}"
        end = _END_MARKER_RE.search(_DIACRITICS_RE.sub("", body))
        if end:
            body = body[: _find_original_offset(body, end.start())]
        out.append({**page, "body": body})
    return out


def _find_original_offset(original: str, plain_offset: int) -> int:
    """Map an offset in the diacritics-stripped text back to the original."""
    seen = 0
    for i, ch in enumerate(original):
        if seen == plain_offset:
            return i
        if not _DIACRITICS_RE.match(ch):
            seen += 1
    return len(original)


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    pages = load_pages(in_path)
    print(f"loaded {len(pages)} pages")
    entries, unparsed = parse_book(prepare_pages(pages))
    print(f"parsed {len(entries)} hadith entries")
    print(f"unparsed/orphaned pages: {len(unparsed)}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "entries": [
                    {"serial": e.serial, "heading": e.heading, "text": e.text, "footnote_text": e.footnote_text}
                    for e in entries
                ],
                "unparsed_log": unparsed,
            },
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

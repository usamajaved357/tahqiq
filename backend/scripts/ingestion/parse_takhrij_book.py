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
"""
import json
import re
import sys
from dataclasses import dataclass, field

DIGITS = "٠١٢٣٤٥٦٧٨٩"
DIACRITIC_RE = re.compile(r"[ً-ْٰۖ-ۭ]")
TITLE_RE = re.compile(r"<span data-type=['\"]title['\"][^>]*>(.*?)</span>")
HADITH_START_RE = re.compile(
    rf"([{DIGITS}]+(?:\s*/\s*[{DIGITS}]+)?)\s*-\s*(?=(?:حدثنا|أخبرنا|حدثني|وقال\s|قال\s))"
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
    footnote_text: str = ""  # this hadith's slice of the page(s)' footnote text
    page_ids: list[str] = field(default_factory=list)


def parse_book(pages: list[dict]) -> tuple[list[ParsedHadith], list[dict]]:
    """Walk pages in order, tracking the current heading, and split hadith
    text at each numbered start. A hadith's footnote text is the
    concatenation of every page its body text touches — imprecise when two
    hadith share a page and both have footnotes (their footnote text gets
    merged), a known limitation flagged for the caller rather than silently
    guessed away."""
    entries: list[ParsedHadith] = []
    unparsed_log: list[dict] = []

    current_heading: str | None = None
    pending: ParsedHadith | None = None
    buffer = ""
    buffer_pages: list[str] = []
    buffer_foot = ""

    def finalize() -> None:
        if pending is None:
            return
        pending.text = buffer.strip()
        pending.footnote_text = buffer_foot.strip()
        pending.page_ids = list(buffer_pages)
        entries.append(pending)

    for page in pages:
        body = page.get("body") or ""
        foot = page.get("foot") or ""
        page_id = page["id"]

        titles = TITLE_RE.findall(body)
        remaining = TITLE_RE.sub("\n", body)

        if titles:
            finalize()
            pending = None
            buffer, buffer_pages, buffer_foot = "", [], ""
            current_heading = strip_with_map(titles[-1])[0].strip()

        stripped, idx_map = strip_with_map(remaining)
        pos = 0
        for m in HADITH_START_RE.finditer(stripped):
            orig_start = idx_map[m.start()] if m.start() < len(idx_map) else len(remaining)
            orig_end = idx_map[m.end() - 1] + 1 if m.end() - 1 < len(idx_map) else len(remaining)
            buffer += remaining[pos:orig_start]
            buffer_pages.append(page_id)
            finalize()
            pending = ParsedHadith(serial=m.group(1), heading=current_heading)
            buffer, buffer_pages, buffer_foot = "", [], ""
            pos = orig_end

        buffer += remaining[pos:]
        if page_id not in buffer_pages:
            buffer_pages.append(page_id)
        if foot:
            buffer_foot = (buffer_foot + "\n" + foot) if buffer_foot else foot

        if pending is None and not titles and remaining.strip() and not buffer.strip():
            unparsed_log.append({"id": page_id, "reason": "no heading/hadith context yet"})

    finalize()
    return entries, unparsed_log


def load_pages(path: str) -> list[dict]:
    pages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            pages.append(json.loads(line))
    return pages


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    pages = load_pages(in_path)
    print(f"loaded {len(pages)} pages")
    entries, unparsed = parse_book(pages)
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

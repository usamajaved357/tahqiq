"""Phase 2 cross-referencing: parse the extracted Tuhfat al-Ashraf text into
structured atraf entries (companion -> hadith tarf -> cited collections).

Source: the JSONL extracted from Maktabah Shamela's local Lucene index
earlier this project (see project notes) — NOT re-extracted here, read
directly from wherever the user saved it.

This script only parses structure. It does not touch any database and does
not try to resolve citation numbers against our own hadith numbering (the
two don't map cleanly — see project notes on edition-specific numbering).
Output is a staging JSON file for review before match_atraf.py runs.

Usage: python -m scripts.ingestion.parse_atraf <path-to-tuhfat_alashraf_pages.jsonl> <output.json>
"""
import json
import re
import sys

TITLE_RE = re.compile(r'<span data-type="title"[^>]*>(.*?)</span>')

DIGITS = "٠١٢٣٤٥٦٧٨٩"
CODE_CHARS = "خمدتسقعيح"  # Bukhari, Muslim, Abu Dawud, Tirmidhi, Nasa'i, Ibn Majah,
# all-six, plus symbols this edition uses for Mizzi's supplementary sources
# (e.g. سي = Nasa'i's supplementary work). Entries citing only supplementary
# symbols won't have anything to match against our six-collection DB, but
# capturing them still improves overall entry-detection coverage for mixed
# citations (e.g. "ت سي" includes Tirmidhi, which we do have).

# Entry header: serial number - collection code(s) حديث. Codes are seen
# bracketed ([خ م ت س]), parenthesized ((خ)), or bare (خ) depending on
# context — accept any/none of those wrappers. Real-data variants also
# found (verified against the actual extracted pages, not assumed): the
# serial repeated inside the code parens ("٤٩ - (٤٩ س) حديث"), a page-break
# marker between codes and حديث ("(دق) ⦗٥٤⦘ حديث"), a second bracketed code
# group ("[خت] (د ت س ق) حديث"), and an "ألف" variant-entry marker mixed in
# with codes ("(ألف خت) حديث"). All of these keep the header content
# restricted to digits/codes/brackets/"ألف" — never ordinary prose — so
# widening the middle group to that character set (capped, non-greedy)
# catches the real variants without risking a false match on unrelated text
# (prose can't satisfy that restricted character class for 30+ chars).
ENTRY_MIDDLE_RE = rf"(?:ألف|[{CODE_CHARS}\[\]\(\)⦗⦘/{DIGITS}\s]){{0,30}}?"
ENTRY_RE = re.compile(
    rf"([{DIGITS}]+)\s*[-–—]\s*({ENTRY_MIDDLE_RE})\s*و?حديث\s*:?"
)

# Citation segment: code, MANDATORY "في" + book name, then a parenthetical
# (number, book:chapter, or annotation — kept as raw text, not parsed further
# here). "في" is required — without it this pattern false-matches on
# unrelated parenthetical content in the prose, like embedded Quran verse
# references (e.g. a hadith's tarf mentioning Surah 112 as "(١١٢: ١)"), which
# never have "في book-name" immediately before them.
CITATION_RE = re.compile(
    rf"[\[\(]?([{CODE_CHARS}]{{1,2}})[\]\)]?\s*:?\s*في\s+([^()\[\]\n]{{2,40}}?)\(([^()]{{0,60}})\)"
)


def strip_diacritics(s: str) -> str:
    return re.sub(r"[ً-ْٰ]", "", s)


# Shamela's <span data-type="title"> markup isn't used only for companion
# section headings — for some entries it also wraps that ONE hadith's own
# citation text (apparently for the book's internal table-of-contents /
# deep-link anchors, not to mark a new companion). Confirmed on real pages,
# e.g. page 11385-3149: `<span data-type="title">حديث: نهى عن ثمن الكلب
# والسنور.د في البيوع (٦٤: ١) عن إبراهيم بن موسى...</span>` — that's a full
# citation (book name, parenthesized numbers, narrator chain), not a
# companion name. Treating every title span as a new companion heading (the
# original logic) let this kind of title silently overwrite the real
# companion name with hadith text — corrupting not just that entry but every
# entry after it until the next real heading. A genuine companion heading is
# just a short chain-of-narrators ending in a sahabi ("X، عن Y، عن Z") and
# never starts with "حديث" (a citation) or "وبه" (Mizzi's "and via this same
# chain" continuation marker) or embeds a parenthesized citation number.
FAKE_TITLE_RE = re.compile(r"^\s*(حديث|وبه)\b|\([٠-٩]")


def is_real_companion_title(title: str) -> bool:
    return not FAKE_TITLE_RE.search(title) and len(title) < 100


SERIAL_MAX_SKIP = 3  # the print skips a few numbers (e.g. 6827, 11051 appear nowhere)
SERIAL_SEARCH_WINDOW = 60000  # characters ahead in which the next serial must appear
_ENTRY_HEAD_RE = re.compile(rf"\s*[-–—]\s*({ENTRY_MIDDLE_RE})\s*(?:و?حديث|و?به)")
_SEGMENT_CODE_RE = re.compile(r"(?:^|(?<=[\s.\-–)\]⦘]))[\[(]?([خمدتسق])[\])]?\s*(?=(?:في|فيه|وفي)\b)")
_HEADING_MARK = "\x00H{}\x00"
_HEADING_MARK_RE = re.compile("\x00H(\\d+)\x00")


def _eastern(n: int) -> str:
    return "".join(DIGITS[int(c)] for c in str(n))


def parse_entries_sequential(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Find entries by their serial numbers, which run 1, 2, 3… through the
    whole book (2026-09-25 rewrite; parse_entries below is the original).

    parse_entries missed 2,453 of the 19,626 entries: it removed every title
    span before looking for "N - … حديث", but Shamela wraps many ENTRIES in a
    title span ("١٠١٧ -<span data-type="title"> حديث: أنّ النبيَّ ﷺ أعتق
    صفيَّة…</span>"), and entries continuing the previous chain start "وبه"
    ("١٤٧٥٠ - وبه فيه (الصلاة ٩٠: ٢) يتعاقبون فيكم ملائكة…"), not "حديث".
    Here the next header is found as exactly the NEXT expected number followed
    by a dash, at a line start or after a sentence end — a number inside the
    text ("(١٨ - ٧٦)") is never the next serial at such a position. A title
    span directly after "N -" is that entry's text; any other title span is a
    companion heading, as before (is_real_companion_title)."""
    headings: list[str] = []
    parts = []
    for page in pages:
        body = page["body"]
        out, last = [], 0
        for m in TITLE_RE.finditer(body):
            out.append(body[last : m.start()])
            inner = m.group(1)
            # an entry's own text in a title span follows its "N -" and reads
            # "حديث…"/"وبه…" or carries citation numbers; a companion heading
            # can ALSO follow a bare "N -" ("١٤٣٢١ -<span…> محمد بن عبد الرحمن
            # بن أبي ذئب، عن سعيد المقبري…</span>") and is still a heading
            if not is_real_companion_title(inner):
                out.append(" " + inner + " ")
            else:
                headings.append(strip_diacritics(inner).strip())
                out.append("\n" + _HEADING_MARK.format(len(headings) - 1) + "\n")
            last = m.end()
        out.append(body[last:])
        parts.append("".join(out))
    text = "\n".join(parts)

    found: dict[int, re.Match] = {}
    pos, n, last_serial = 0, 1, 0
    while True:
        best = None
        for k in range(SERIAL_MAX_SKIP + 1):
            m = re.compile(rf"(?:^|(?<=[\n.\]\)»\"\x00]))\s*{_eastern(n + k)}(?=\s*[-–—])").search(text, pos, pos + SERIAL_SEARCH_WINDOW)
            if m and (best is None or m.start() < best[1].start()):
                best = (n + k, m)
            if m and k == 0:
                break
        if best is None:
            break
        found[best[0]] = best[1]
        pos, n, last_serial = best[1].end(), best[0] + 1, best[0]
    # a header glued to the heading before it ("…عن سهل بن سعد٤٧٠٥ - د حديث"):
    # look again, between its neighbours only, without the line-start condition
    for s in range(1, last_serial):
        if s in found:
            continue
        lo = max((found[k].end() for k in found if k < s), default=0)
        hi = min((found[k].start() for k in found if k > s), default=len(text))
        m = re.compile(rf"(?<![{DIGITS}]){_eastern(s)}(?=\s*[-–—]\s*{ENTRY_MIDDLE_RE}\s*(?:و?حديث|و?به))").search(text, lo, hi)
        if m:
            found[s] = m

    serials = sorted(found)
    entries, heading_at = [], None
    heading_marks = [(m.start(), int(m.group(1))) for m in _HEADING_MARK_RE.finditer(text)]
    hi_idx = 0
    for i, s in enumerate(serials):
        start = found[s].end()
        end = found[serials[i + 1]].start() if i + 1 < len(serials) else len(text)
        while hi_idx < len(heading_marks) and heading_marks[hi_idx][0] < found[s].start():
            heading_at = headings[heading_marks[hi_idx][1]]
            hi_idx += 1
        chunk = _HEADING_MARK_RE.sub(" ", text[start:end])
        head = _ENTRY_HEAD_RE.match(chunk)
        header_codes = "".join(re.findall(rf"[{CODE_CHARS}]", head.group(1))) if head else ""
        body = chunk[head.end() :] if head else re.sub(r"^\s*[-–—]", "", chunk)
        body = re.sub(r"^\s*:?", "", body).strip()
        citations = [
            {"code": m.group(1), "book": m.group(2).strip(), "detail": m.group(3).strip()}
            for m in CITATION_RE.finditer(body)
        ]
        cm = CITATION_RE.search(body)
        codes = header_codes or "".join(sorted({m.group(1) for m in _SEGMENT_CODE_RE.finditer(body)}))
        entries.append(
            {
                "serial": _eastern(s),
                "codes": codes,
                "companion": heading_at,
                "tarf": body[: cm.start()].strip() if cm else body,
                "citations": citations,
                "text": body,
                "continues_chain": bool(head and re.search(r"و?به$", head.group(0).strip())),
            }
        )
    missing = [s for s in range(1, last_serial + 1) if s not in found]
    return entries, [{"missing_serial": _eastern(s)} for s in missing]


def parse_entries(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Walk pages in order, tracking the current companion section, and
    accumulating entry text across page boundaries until the next entry (or
    a new companion heading) starts. Returns (entries, unparsed_pages_log)."""
    entries = []
    unparsed_log = []

    current_companion = None
    buffer = ""  # text since the last entry-start, not yet finalized
    pending_entry = None  # {serial, codes, companion} for the entry buffer holds

    def finalize_pending():
        if pending_entry is None:
            return
        text = buffer.strip()
        citations = [
            {"code": m.group(1), "book": m.group(2).strip(), "detail": m.group(3).strip()}
            for m in CITATION_RE.finditer(text)
        ]
        first_citation_pos = None
        cm = CITATION_RE.search(text)
        if cm:
            first_citation_pos = cm.start()
        tarf = text[:first_citation_pos].strip() if first_citation_pos else text
        entries.append(
            {
                "serial": pending_entry["serial"],
                "codes": pending_entry["codes"],
                "companion": pending_entry["companion"],
                "tarf": tarf,
                "citations": citations,
                # the whole entry as printed — the citation parse above misses
                # "م فيه (…)" ("Muslim, in the same book") segments and keeps
                # no narrator names; match_atraf_teachers.py reads the
                # compiler's own teacher ("عن X") from this raw text
                "text": text,
            }
        )

    for page in pages:
        body = page["body"]
        page_id = page["id"]

        # Companion headings reset context — anything buffered without a
        # pending entry (e.g. section intro prose) is discarded, not forced
        # into a fake entry.
        titles = TITLE_RE.findall(body)
        remaining = TITLE_RE.sub("\n", body)

        if titles:
            # a title on this page ends whatever entry was in progress,
            # regardless of whether it's a real companion heading or a
            # per-hadith citation title
            finalize_pending()
            pending_entry = None
            buffer = ""
            real_titles = [t for t in titles if is_real_companion_title(t)]
            if real_titles:
                current_companion = strip_diacritics(real_titles[-1]).strip()
            # else: this page's title(s) are per-hadith citation text, not a
            # companion heading — keep current_companion as it was (the last
            # real heading still applies to entries on this page)

        pos = 0
        for m in ENTRY_RE.finditer(remaining):
            # text between previous position and this new entry belongs to
            # the entry currently being accumulated
            buffer += remaining[pos:m.start()]
            finalize_pending()
            pending_entry = {
                "serial": m.group(1),
                # header middle group now also tolerates digits/brackets/"ألف"
                # (see ENTRY_MIDDLE_RE) — filter back down to just the real
                # collection-code letters for this field.
                "codes": "".join(re.findall(rf"[{CODE_CHARS}]", m.group(2))),
                "companion": current_companion,
            }
            buffer = ""
            pos = m.end()

        buffer += remaining[pos:]

        if pending_entry is None and titles == [] and remaining.strip():
            # a page with real content but no entry-start and no title —
            # likely mid-entry continuation (handled above via buffer) or a
            # page we genuinely can't place; only log if buffer is also empty
            # (i.e. truly orphaned text with no context at all)
            if not buffer.strip():
                pass
            elif current_companion is None:
                unparsed_log.append({"id": page_id, "reason": "no companion context yet"})

    finalize_pending()
    return entries, unparsed_log


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]

    pages = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            pages.append(json.loads(line))
    print(f"loaded {len(pages)} pages")

    entries, unparsed_log = parse_entries_sequential(pages)

    multi_collection = [e for e in entries if len(set(e["codes"]) & set("خمدتسق")) >= 2]

    print(f"parsed {len(entries)} entries total")
    print(f"entries citing 2+ of the six books: {len(multi_collection)}")
    print(f"entries with zero citations found: {sum(1 for e in entries if not e['citations'])}")
    print(f"serials not found in the text: {[x['missing_serial'] for x in unparsed_log]}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"entries": entries, "unparsed_log": unparsed_log}, f, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

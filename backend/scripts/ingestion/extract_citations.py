"""Extracts "same hadith" cross-reference citations from a takhrij-style
edition's raw footnote text (currently: Musnad Ahmad only — see
docs/HADITH_CROSS_REFERENCING.md for why Sunan al-Kubra has none).

Deliberately does NOT trust per-hadith footnote attribution (parse_takhrij_
book.py's own _FootnoteAssembler was found, empirically, to accumulate
unbounded positional drift over a book this size — see that module's git
history / docs for the investigation). Instead this extracts citations at
the PAGE level and leaves "which specific hadith on this page does this
citation belong to" as a separate, content-verified step in
match_citations.py — the same "never trust position, verify by content"
discipline already used for Tuhfat al-Ashraf.

Only the single clearest, safest signal is extracted: a citation phrase
ending in "بهذا الإسناد" ("with this [same] isnad") or "بهذا الحديث" ("with
this [same] hadith") — confirmed, per docs/HADITH_CROSS_REFERENCING.md
Section 5, to unambiguously mean "same hadith, safe to link." Everything
else (شاهد/corroborating-witness citations, "سلف برقم" self-references to a
DIFFERENT companion, "وفي الباب عن" topic cross-references) names a
DIFFERENT hadith and is deliberately NOT extracted here — building that
classifier is separate, even-higher-stakes work, not a "few more patterns"
extension of this one.

Usage: python -m scripts.ingestion.extract_citations <raw_pages.jsonl> <out.json>
"""
import json
import re
import sys

DIGITS = "٠١٢٣٤٥٦٧٨٩"

# One "أخرجه ... بهذا الإسناد/الحديث" span can name several collections
# before its closing phrase (e.g. "وأخرجه البخاري (٧٢٢٢)، والبيهقي في
# 'الدلائل' ٦/ ٥١٩، والبغوي (٤٢٣٧) من طريق محمد بن جعفر، بهذا الإسناد") —
# every one of those named collections is being claimed as the same isnad,
# not just the first. Bounded to 350 chars to avoid a runaway match if a
# page happens to lack the closing phrase within a reasonable distance.
SPAN_RE = re.compile(r"(?:و)?أخرجه(.{5,350}?)(?:بهذا الإسناد|بهذا الحديث)")

# A citation within a span names a collection then a parenthesized number,
# e.g. "البخاري (٧٢٢٢)" — sometimes with an in-parens sub-reference like
# "مسلم (٢١٤) (٣٦٥)" (book/hadith-number pair, both kept as the full number
# string; see docs' note on Muslim's numbering variability). A name can also
# appear with NO number at all (e.g. "والطبري ٢٤/ ٢٧", a volume/page
# reference, not a hadith number) — those are skipped, not guessed at.
CITATION_RE = re.compile(rf"([^\d\(\.,،؛]{{1,30}}?)\s*((?:\(\s*[{DIGITS}]+\s*\)\s*){{1,2}})")

# Maps a citation's own name text to our HadithCollection.name — every
# variant actually observed in real Musnad Ahmad footnotes (see the name
# frequency table gathered during investigation). Deliberately EXCLUDES
# name-alike sources that are a DIFFERENT book, not the collection we mean:
# "البخاري في الأدب المفرد" is Bukhari's separate work, not Sahih al-
# Bukhari; "النسائي في الكبرى" is the Sunan al-Kubra already ingested
# separately (its own collection, with no footnotes of its own — see
# Section 4b) — citations INTO it from Musnad Ahmad are legitimate future
# work but are not resolved by this script (the "Sunan an-Nasa'i" row in
# our DB is the STANDARD Mujtaba recension only).
COLLECTION_NAME_MAP: dict[str, str] = {
    "البخاري": "Sahih al-Bukhari",
    "مسلم": "Sahih Muslim",
    "أبو داود": "Sunan Abu Dawud",
    "الترمذي": "Jami At-Tirmidhi",
    "ابن ماجه": "Sunan Ibn Majah",
    "النسائي": "Sunan an-Nasa'i",
    "النسائي في المجتبى": "Sunan an-Nasa'i",
}
# Names that look like a match but must resolve to nothing (checked against
# real occurrences, not assumed):
EXCLUDED_NAME_HINTS = (
    "الأدب المفرد",
    "في الكبرى",  # Nasa'i's Kubra — different collection, see docstring
    "الأوسط",
    "الصغرى",  # Bayhaqi/Tabarani's separate works, not a Nasa'i-Kubra alias
)


def resolve_collection(raw_name: str, following_context: str) -> str | None:
    name = raw_name.strip().strip("و")
    if any(h in following_context[:20] for h in EXCLUDED_NAME_HINTS):
        return None
    return COLLECTION_NAME_MAP.get(name)


def extract_page_citations(page_id: str, foot: str) -> list[dict]:
    results = []
    for span_m in SPAN_RE.finditer(foot):
        span_text = span_m.group(0)
        inner = span_m.group(1)
        # "من طريق X" — the narrator chain this citation claims is shared —
        # kept as a disambiguation hint for match_citations.py, not required.
        via_m = re.search(r"من\s+طريق(?:ين)?\s+([^.،؛]{1,60})", inner)
        via_hint = via_m.group(1).strip() if via_m else None

        for cite_m in CITATION_RE.finditer(inner):
            name_raw, numbers_raw = cite_m.group(1), cite_m.group(2)
            collection = resolve_collection(name_raw, inner[cite_m.end(1) :])
            if not collection:
                continue
            numbers = re.findall(rf"[{DIGITS}]+", numbers_raw)
            if not numbers:
                continue
            results.append(
                {
                    "page_id": page_id,
                    "collection": collection,
                    "numbers": numbers,
                    "via_hint": via_hint,
                    "span_text": span_text.strip(),
                }
            )
    return results


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]

    citations = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            page = json.loads(line)
            foot = page.get("foot") or ""
            if not foot:
                continue
            citations.extend(extract_page_citations(page["id"], foot))

    print(f"extracted {len(citations)} same-isnad citations into our six collections")
    by_collection: dict[str, int] = {}
    for c in citations:
        by_collection[c["collection"]] = by_collection.get(c["collection"], 0) + 1
    for name, count in sorted(by_collection.items(), key=lambda x: -x[1]):
        print(f"  {count:6d}  {name}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(citations, f, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

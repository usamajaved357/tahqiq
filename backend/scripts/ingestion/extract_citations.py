"""Extracts "same hadith" cross-reference citations from a takhrij-style
edition's raw footnote text — any of the Arna'ut/Risala editions (Musnad
Ahmad, Sunan Abu Dawud, Jami' Tirmidhi, Sunan Ibn Majah, Sunan an-Nasa'i);
see docs/HADITH_CROSS_REFERENCING.md for why Sunan al-Kubra has none.

Deliberately does NOT trust per-hadith footnote attribution (parse_takhrij_
book.py's own _FootnoteAssembler was found, empirically, to accumulate
unbounded positional drift over a book this size — see that module's git
history / docs for the investigation). Instead this extracts citations at
the PAGE level and leaves "which specific hadith on this page does this
citation belong to" as a separate, content-verified step in
match_citations.py — the same "never trust position, verify by content"
discipline already used for Tuhfat al-Ashraf.

Only "same hadith" signals are extracted (kind):
  - "same_isnad": an "أخرجه ..." span closed by "بهذا الإسناد", "بهذا
    الحديث" or the editors' short form "، به" ("with it", i.e. with this
    same isnad) — every book named inside the span is claimed as the same
    isnad, not just the first.
  - "is_in": the editors' explicit statement that THIS hadith is found at a
    given number elsewhere — 'وهو في "مسند أحمد" (N)' (Sunan editions) and
    'وهو في "السنن الكبرى" برقم (N)' (Nasa'i's Mujtaba edition).
Everything else (شاهد/corroborating-witness citations, "سلف برقم" self-
references, "وفي الباب عن" topic cross-references) names a DIFFERENT hadith
and is deliberately NOT extracted here.

Usage: python -m scripts.ingestion.extract_citations <raw_pages.jsonl> <out.json>
"""
import json
import re
import sys

DIGITS = "٠١٢٣٤٥٦٧٨٩"

# Bounded to 350 chars to avoid a runaway match if a page happens to lack the
# closing phrase within a reasonable distance. "، به" must be followed by
# punctuation/space so words merely starting with "به" (بهز …) don't close
# a span.
SPAN_RE = re.compile(r"(?:و)?أخرجه(.{5,350}?)(?:بهذا الإسناد|بهذا الحديث|،\s*به(?=[.،:؛\s]))")

# One or two parenthesized numbers: "(٧٢٢٢)", Muslim's "(٢١٤) (٣٦٥)" /
# "(١٨٩٥): (١٣٥)" (Abd al-Baqi global number first).
NUMBERS_RE = re.compile(rf"\(\s*([{DIGITS}]+)\s*\)(?:\s*:?\s*\(\s*([{DIGITS}]+)\s*\))?")

# A citation is a known book name IMMEDIATELY to the left of its number,
# optionally qualified by which of the author's works is meant. Anchoring on
# the number and matching leftwards is deliberate: a first version matched
# up to 30 characters rightwards from wherever the scan happened to start,
# so "وأخرجه من طريق يحيى بن سعيد البخاري (٥٥٨١)" produced the "name"
# "ن طريق يحيى بن سعيد البخاري" — unmapped, silently dropped.
NAME_BEFORE_NUMBER_RE = re.compile(
    r'(?:^|[\s،,.؛:)])و?(البخاري|مسلم|أبو داود|الترمذي|ابن ماجه|النسائي|أحمد)'
    r'(?:\s+في\s*"([^"]{1,25})")?\s*$'
)
NARRATOR_NAME_PREFIXES = {"أبو", "أبي", "أبا", "بن", "ابن", "عن", "حدثنا", "أخبرنا"}
BASE_COLLECTION = {
    "البخاري": "Sahih al-Bukhari",
    "مسلم": "Sahih Muslim",
    "أبو داود": "Sunan Abu Dawud",
    "الترمذي": "Jami At-Tirmidhi",
    "ابن ماجه": "Sunan Ibn Majah",
    "النسائي": "Sunan an-Nasa'i",
    "أحمد": "Musnad Ahmad",
}
# A qualifier names WHICH of the author's works is cited. Only these map to
# a collection we hold; any other qualifier ("الأدب المفرد", "التاريخ الكبير",
# "خلق أفعال العباد", "عمل اليوم والليلة" …) is a different book and the
# citation is dropped rather than guessed at.
QUALIFIED_COLLECTION = {
    ("أحمد", "مسنده"): "Musnad Ahmad",
    ("النسائي", "الكبرى"): "Sunan al-Kubra",
    ("النسائي", "المجتبى"): "Sunan an-Nasa'i",
}

IS_IN_PATTERNS = [
    (re.compile(rf'وهو في\s*"مسند أحمد"\s*((?:\(\s*[{DIGITS}]+\s*\)\s*(?:و\s*)?)+)'), "Musnad Ahmad"),
    (re.compile(rf'وهو في\s*"السنن الكبرى"\s*برقم\s*((?:\(\s*[{DIGITS}]+\s*\)\s*(?:و\s*)?)+)'), "Sunan al-Kubra"),
]


def resolve_collection(name: str, qualifier: str | None) -> str | None:
    if qualifier is None:
        return BASE_COLLECTION.get(name)
    return QUALIFIED_COLLECTION.get((name, qualifier.strip()))


# A page's footnote area holds several hadith's notes back to back. A span
# must not cross from one note into the next — confirmed real: "أخرجه أحمد
# (…)… وهذا الإسناد غير محفوظ.(¬٢) إسناده حسن… بهذا الحديث" read the first
# note's non-matching citations as if the second note's closing phrase
# covered them. "(¬N)" always starts a note; a plain "(N)" only right after
# a sentence end (Muslim's "(٣٠) (٤٨)" sub-numbers never follow a period).
NOTE_BOUNDARY_RE = re.compile(rf"\(¬[{DIGITS}]{{1,3}}\)|(?<=\.)\s*\([{DIGITS}]{{1,2}}\)")


def split_notes(foot: str) -> list[str]:
    return [seg for seg in NOTE_BOUNDARY_RE.split(foot) if seg.strip()]


def extract_page_citations(page_id: str, foot: str) -> list[dict]:
    results = []
    for note in split_notes(foot):
        results.extend(_extract_note_citations(page_id, note))
    return results


def _extract_note_citations(page_id: str, foot: str) -> list[dict]:
    results = []
    for span_m in SPAN_RE.finditer(foot):
        span_text = span_m.group(0)
        inner = span_m.group(1)
        # "من طريق X" — the narrator chain this citation claims is shared —
        # kept as a disambiguation hint for match_citations.py, not required.
        via_m = re.search(r"من\s+طريق(?:ين)?\s+([^.،؛]{1,60})", inner)
        via_hint = via_m.group(1).strip() if via_m else None

        last_collection, last_end = None, -1
        for num_m in NUMBERS_RE.finditer(inner):
            window = inner[max(0, num_m.start() - 45) : num_m.start()]
            name_m = NAME_BEFORE_NUMBER_RE.search(window)
            collection = None
            if name_m:
                # "أبو أحمد", "محمد بن أحمد", "عن مسلم" — a narrator, not the book
                preceding = window[: name_m.start(1)].split()
                if not (preceding and preceding[-1] in NARRATOR_NAME_PREFIXES):
                    collection = resolve_collection(name_m.group(1), name_m.group(2))
            elif last_collection and re.fullmatch(r"\s*و\s*", inner[last_end : num_m.start()]):
                # "أحمد (١٩٣٥٩) و (١٩٣٦٦)" — a further number of the same book
                collection = last_collection
            last_collection, last_end = collection, num_m.end()
            if not collection:
                continue
            numbers = [n for n in num_m.groups() if n]
            results.append(
                {
                    "page_id": page_id,
                    "collection": collection,
                    "numbers": numbers,
                    "via_hint": via_hint,
                    "span_text": span_text.strip(),
                    "kind": "same_isnad",
                }
            )

    for pattern, collection in IS_IN_PATTERNS:
        for m in pattern.finditer(foot):
            for number in re.findall(rf"[{DIGITS}]+", m.group(1)):
                results.append(
                    {
                        "page_id": page_id,
                        "collection": collection,
                        "numbers": [number],
                        "via_hint": None,
                        "span_text": m.group(0).strip(),
                        "kind": "is_in",
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

    print(f"extracted {len(citations)} same-hadith citations into collections we hold")
    by_collection: dict[tuple[str, str], int] = {}
    for c in citations:
        key = (c["collection"], c["kind"])
        by_collection[key] = by_collection.get(key, 0) + 1
    for (name, kind), count in sorted(by_collection.items(), key=lambda x: -x[1]):
        print(f"  {count:6d}  {name}  ({kind})")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(citations, f, ensure_ascii=False, indent=1)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

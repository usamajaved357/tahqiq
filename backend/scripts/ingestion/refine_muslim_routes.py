"""Phase 2 cross-referencing: choose the right Sahih Muslim ROUTE for every
resolved footnote citation (2026-09-25).

match_citations.py resolves "مسلم (١٩٩٥) (٣٥)" to Abd al-Baqi's number 1995
and then picks one row of that number by matn similarity. But one Abd al-Baqi
number holds EVERY route Muslim gives for the hadith — 1995 has six rows from
'A'isha (via al-Aswad, Mu'adha, Thumama…) — and the wording barely separates
them, so the pick often landed on a sibling route. Musnad #24840 (Mansur →
Ibrahim → al-Aswad → 'A'isha) was linked to Muslim 5176 (… Ishaq b. Suwayd →
Mu'adha → 'A'isha) although the footnote names "من طريق جرير عن منصور", i.e.
Muslim 5172, word for word the Musnad's text. Every row of the number is the
hadith Arna'ut cited, so no link was to a WRONG hadith — but not always to the
narration he cited. The in-book number ("(35)") cannot pinpoint the row: it
agrees with fawazahmed0's in-book numbering in 5 of 118 checks.

So the route is decided by the chain of narrators, as 'ilm al-atraf does:

1. Only rows in the current link's BLOCK. A number Abd al-Baqi repeated
   "بإثر" another hadith sits in two places holding different reports — 561 is
   garlic at Khaybar (Muslim 1248) AND donkey meat after 1936 (Muslim 5008).
2. Narrators are the segments between transmission words in the isnad (text
   before the Prophet is named), kept only if made of chain vocabulary —
   words that occur >= 60% of the time before the Prophet is named, learned
   from the six books — so report text ("كان يكبر كلما خفض ورفع", "نوفا
   الشامي يزعم") is never read as a narrator.
3. Two narrators are the same person when their given name matches (two words
   for عبد/عبيد/ابو/ابن X — "عبيد الله بن موسى" is not "عبيد الله بن عبد
   الله") and their fathers match where both are given. Unambiguous
   equivalences only (الزهري = ابن شهاب, شقيق = أبو وائل, ابن علية = إسماعيل
   بن إبراهيم, ذكوان = أبو صالح): there are several men called Abu Ishaq, Abu
   Malik, Sulayman… "عن أبيه" / "عن جده" resolves to the citing chain's next
   narrator ("سفيان عن أبيه" = Sa'id b. Masruq).
4. A row that only gives an upper chain ("بهذا الإسناد", "بإسناد التيمي",
   "بمثله") inherits the lower chain of the row(s) above it; a row split off
   mid-hadith (its chain is at most the companion: "حدثنا أبي بن كعب…") is a
   fragment and never the preferred target.
5. Anchor: the companion is the deepest citing narrator the CURRENT row shares
   (links were made under a companion guard), the tabi'i is the one above it.
   If the current row shares names only far up the chain (a lone "يحيى"
   matches too loosely), the last parsed narrator is taken as the companion.
6. Keep the current row if it has the tabi'i and is not a fragment. Otherwise
   look among the rows that have it: the route the footnote names (its
   via_hint, or "من طريق X"), else the most shared narrators, else the closest
   text. Never a sideways move: the new row must share strictly more of the
   citing chain than the current one (unless the current one is a fragment).
   No row has the tabi'i -> keep (still the cited hadith).

Verification (2026-09-25, offline against the texts our rows were loaded
from): 64 hand-judged cases all come out as judged, and a fresh random 25 of
the final changes read in full: 9 fix a different tabi'i, 13 move to the exact
route, 3 are sideways within the same tabi'i, none made worse. Earlier drafts
of the rule were rejected on exactly the failures the numbered rules above now
prevent (single-word name collisions, companion-level false anchors, fragment
rows, cross-block moves, sideways moves).

Writes nothing to the database: for each <stem>.pairs.json it writes
<stem>.routes.json (every changed citation, old -> new, with the reason) and
<stem>.pairs.refined.json (the pairs with the refined cited ids).

Usage: python -m scripts.ingestion.refine_muslim_routes <stem.pairs.json>[=<citations.json>] [...]
(the citations file from extract_citations.py: <stem>.json beside the pairs
file unless given after "=" — the v3 run's is citations.json)
"""
import json
import re
import sys
from collections import Counter

from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection
from scripts.ingestion.common import hadith_db_session
from scripts.ingestion.ingest_hadith import fetch_edition
from scripts.ingestion.match_citations import normalize, to_western_digits

MUSLIM = "Sahih Muslim"
SIX_BOOKS = ["Sahih al-Bukhari", MUSLIM, "Sunan Abu Dawud", "Jami At-Tirmidhi", "Sunan an-Nasa'i", "Sunan Ibn Majah"]

# ---- narrator parsing ------------------------------------------------------
_TOKEN_MAP = {"ابن": "بن", "ابي": "ابو", "ابا": "ابو", "لابن": "بن", "لابي": "ابو"}
_TRANSMIT = (
    "حدثتنا|حدثتني|اخبرتنا|اخبرتني|قيل|حدثك|اخبرك|قلت|حدثنا|حدثني|حدثناه|حدثه|حدثهم|اخبرنا|اخبرني|اخبرناه|"
    "اخبره|انبانا|عن|سمعت|سمع|سمعا|انه|انها|ان|قال|قالا|قالوا|قالت|يقول|تقول|يحدث|ح|اخبرته|فذكر"
)
_SPLIT_RE = re.compile(rf"(?:^|\s)و?(?:{_TRANSMIT})(?=\s|$)|،|,|-")
_PROPHET_RE = re.compile(r"صلى الله عليه وسلم|ﷺ|رسول الله|النبي|نبي الله|رسول")
_FILLER = {"يعني", "وهو", "هو", "و", "واللفظ", "له", "لفظ", "في", "حديثه", "جميعا", "كلاهما", "كلهم", "ثلاثتهم",
           "اربعتهم", "مولي", "زوج", "وحده", "وغيره", "نحوه", "مثله", "بهذا", "الاسناد", "باسناده"}
_NAME_GLUE = {"بن", "ابو", "ابي", "ام", "عبد", "عبيد", "الله"}
_TWO_WORD_NAME = ("ابو", "بن", "ام", "عبد", "عبيد")
_ALIAS = {"زهري": ["بن", "شهاب"], "ذكوان": ["ابو", "صالح"], "شقيق": ["ابو", "وائل"], "بن عليه": ["اسماعيل", "بن", "ابراهيم"]}
_ROUTE_ONLY_RE = re.compile(r"بهذا الاسناد|باسناده|باسناد|بمثل|بنحو|نحو حديث|مثل حديث")
_HINT_IN_SPAN_RE = re.compile(r"من (?:طريق|طريقي|طريقين|طرق)\s+(?:عن\s+)?([^،.]+)")
_DIACRITICS_RE = re.compile(r"[ً-ْٰ]")


def _tokens(s: str) -> list[str]:
    w = re.sub(r"[^ء-ي\s]", " ", s).split()
    # "ابي" -> kunya "ابو", except Ubayy ("ابي بن كعب"), a name followed by "بن"
    return [("ابي" if x == "ابي" and k + 1 < len(w) and w[k + 1] in ("بن", "ابن") else _TOKEN_MAP.get(x, x)) for k, x in enumerate(w)]


def raw_narrators(text: str) -> list[list[str]]:
    """Segments of the isnad between transmission words, in order."""
    n = normalize(text)
    m = _PROPHET_RE.search(n)
    isnad = n[: m.start()] if m else " ".join(n.split()[:40])
    isnad = re.sub(r"قرات علي|قرئ علي|قراءه علي", " عن ", isnad)
    isnad = re.sub(r"رضي الله عنهما|رضي الله عنها|رضي الله عنه|ام المؤمنين", " ", isnad)
    if " ح " in isnad:  # after a tahwil the last route carries the lower chain
        isnad = isnad.split(" ح ")[-1]
    out = []
    for seg in _SPLIT_RE.split(isnad):
        t = [w for w in _tokens(seg) if w not in _FILLER]
        if t and any(len(w) > 2 and w not in ("بن", "ابو", "عبد", "الله") for w in t):
            out.append(t)
    return out


def learn_chain_vocabulary(texts) -> set[str]:
    """Words that occur >= 60% of the time before the Prophet is named."""
    before, after = Counter(), Counter()
    for t in texts:
        n = normalize(t or "")
        m = _PROPHET_RE.search(n)
        if m:
            before.update(set(_tokens(n[: m.start()])))
            after.update(set(_tokens(n[m.end():])))
    return {w for w, c in before.items() if c >= 3 and c >= 0.6 * (c + after[w])}


class ChainReader:
    def __init__(self, vocabulary: set[str]):
        self.vocabulary = vocabulary

    def is_narrator(self, t: list[str]) -> bool:
        """A name, not a stretch of the report: short, every word chain
        vocabulary except at most one inside a longer name (a rare nisba) —
        never as the last word ("نوفا الشامي يزعم")."""
        if not t or len(t) > 7:
            return False
        bad = [k for k, w in enumerate(t) if w not in self.vocabulary and w not in _NAME_GLUE]
        return not bad or (len(bad) == 1 and len(t) >= 3 and bad[0] != len(t) - 1)

    def narrators(self, text: str) -> list[list[str]]:
        return [n for n in raw_narrators(text) if self.is_narrator(n)]


def canonical(t: list[str]) -> list[str]:
    t = list(t)
    if t[0].startswith("ال") and len(t[0]) > 4:
        t[0] = t[0][2:]
    t = [w[:-1] if len(w) > 3 and w.endswith("ا") else w for w in t]  # accusative "انسا"
    key = " ".join(t[:2]) if t[0] in ("ابو", "بن", "ام") else t[0]
    return _ALIAS.get(key, t)


def _given_name_and_father(t: list[str]):
    n = 2 if t[0] in _TWO_WORD_NAME and len(t) > 1 else 1
    rest = t[n:]
    father = rest[1:3] if len(rest) >= 2 and rest[0] == "بن" else None
    if father and father[0] not in _TWO_WORD_NAME:
        father = father[:1]
    return t[:n], father


def same_person(a: list[str], b: list[str]) -> bool:
    ga, fa = _given_name_and_father(a)
    gb, fb = _given_name_and_father(b)
    return ga == gb and (fa is None or fb is None or fa == fb)


# ---- route choice ------------------------------------------------------------
class MuslimRoutes:
    """rows: Muslim rows in book order as [(hadith_id, text, abd_al_baqi_number or None)]."""

    def __init__(self, rows, reader: ChainReader):
        self.reader = reader
        self.ids = [r[0] for r in rows]
        self.text = [r[1] or "" for r in rows]
        self.pos = {r[0]: i for i, r in enumerate(rows)}
        self.number = [r[2] for r in rows]
        self.narr = [[canonical(n) for n in reader.narrators(t)] for t in self.text]
        self.raw = [[canonical(n) for n in raw_narrators(t)] for t in self.text]

    def block(self, i: int) -> list[int]:
        """The contiguous run of rows (gaps <= 3) with row i's Abd al-Baqi number."""
        n = self.number[i]
        members = [i]
        for step in (-1, 1):
            k = i
            while True:
                nearby = [j for j in range(k + step, k + 4 * step, step) if 0 <= j < len(self.number) and self.number[j] == n]
                if not nearby:
                    break
                k = nearby[0]
                members.append(k)
        return sorted(members)

    def route_only(self, i: int) -> bool:
        return bool(_ROUTE_ONLY_RE.search(_DIACRITICS_RE.sub("", self.text[i]).replace("إ", "ا")))

    def fragment(self, i: int) -> bool:
        return len(self.narr[i]) <= 1

    def chain_of(self, block: list[int], i: int) -> list[list[str]]:
        names = list(self.narr[i])
        if self.route_only(i) or self.fragment(i):
            for j in reversed([x for x in block if x < i]):
                names += self.narr[j]
                if not self.route_only(j) and not self.fragment(j):
                    break
        return names

    def choose(self, source_text: str, cited_id: int, hint: str) -> tuple[int, str]:
        """(hadith_id to link, reason)."""
        cur = self.pos[cited_id]
        block = self.block(cur)
        if len(block) == 1:
            return cited_id, "single_route"
        src = [canonical(n) for n in self.reader.narrators(source_text)]
        shared = {}
        for i in block:
            chain = self.chain_of(block, i)
            m = {j for j, a in enumerate(src) if any(same_person(a, b) for b in chain)}
            raw = self.raw[i]
            for q in range(1, len(raw)):  # "X عن أبيه": the citing chain's narrator after X
                if raw[q] and raw[q][0] in ("ابيه", "جده", "ابوه"):
                    m |= {j + 1 for j in range(len(src) - 1) if same_person(src[j], raw[q - 1])}
            shared[i] = m
        if shared[cur] and max(shared[cur]) >= len(src) - 2:
            companion = max(shared[cur])
            tabii = companion - 1 if companion >= 1 else None
        else:
            tabii = len(src) - 2 if len(src) >= 2 else None
        if tabii is None:
            return cited_id, "keep_no_anchor"
        if tabii in shared[cur] and not self.fragment(cur):
            return cited_id, "keep"
        cand = [i for i in block if tabii in shared[i] and not self.fragment(i)] or [i for i in block if tabii in shared[i]]
        if not cand:
            return cited_id, "keep_no_route_with_tabii"
        named = [canonical(n) for n in raw_narrators("حدثنا " + re.split(r"\s+عن\s+|،", hint)[0] + " عن النبي")] if hint.strip() else []
        by_hint = [i for i in cand if named and any(same_person(a, b) for a in named for b in self.chain_of(block, i))]
        if len(by_hint) == 1:
            top = by_hint
        else:
            pool = by_hint or cand
            most = max(len(shared[i]) for i in pool)
            top = [i for i in pool if len(shared[i]) == most]
        if len(top) > 1:
            words = set(_DIACRITICS_RE.sub("", source_text).split())
            sim = {i: len(words & set(_DIACRITICS_RE.sub("", self.text[i]).split())) for i in top}
            best = max(sim.values())
            if sum(1 for i in top if sim[i] == best) == 1:
                top = [i for i in top if sim[i] == best]
        if len(top) != 1:
            return cited_id, "keep_tied"
        new = top[0]
        if len(shared[new]) <= len(shared[cur]) and not self.fragment(cur):
            return cited_id, "keep_no_better_route"
        return self.ids[new], "fragment_to_full_route" if self.fragment(cur) else "route_with_citing_tabii"


def hint_for(citation_hint: str | None, span_text: str) -> str:
    h = re.sub(r"وحده|الإمام|الامام", "", citation_hint or "").strip()
    h = re.sub(r"^عن\s+", "", h)
    if not h:
        m = _HINT_IN_SPAN_RE.search(_DIACRITICS_RE.sub("", span_text))
        h = m.group(1) if m else ""
    return h


def refine(pairs: list[dict], citations: list[dict], routes: MuslimRoutes, source_text: dict[int, str]):
    """(refined pairs, changes)."""
    hints = {c["span_text"]: c.get("via_hint") for c in citations if c.get("collection") == MUSLIM}
    refined, changes = [], []
    for p in pairs:
        if p["cited_collection"] != MUSLIM or p["cited_hadith_id"] not in routes.pos:
            refined.append(p)
            continue
        new_id, reason = routes.choose(source_text.get(p["musnad_hadith_id"], ""), p["cited_hadith_id"],
                                       hint_for(hints.get(p["span_text"]), p["span_text"]))
        if new_id != p["cited_hadith_id"]:
            changes.append({"source_hadith_id": p["musnad_hadith_id"], "source_serial": p["musnad_serial"],
                            "old_cited_hadith_id": p["cited_hadith_id"], "new_cited_hadith_id": new_id,
                            "reason": reason, "span_text": p["span_text"]})
            p = {**p, "cited_hadith_id": new_id, "route_refined_from": p["cited_hadith_id"]}
        refined.append(p)
    return refined, changes


def load_muslim_rows(session) -> list[tuple[int, str, int | None]]:
    """Muslim rows in fawazahmed0's book order with their Abd al-Baqi number."""
    by_number = {
        n: (hid, t)
        for hid, n, t in session.execute(
            select(Hadith.id, Hadith.hadith_number, Hadith.text_ar)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .join(HadithCollection, HadithBook.collection_id == HadithCollection.id)
            .where(HadithCollection.name == MUSLIM)
        )
    }
    rows = []
    for h in fetch_edition("eng-muslim")["hadiths"]:
        hit = by_number.get(str(h["hadithnumber"]))
        if hit:
            number = int(float(h["arabicnumber"])) if h.get("arabicnumber") is not None else None
            rows.append((hit[0], hit[1], number))
    return rows


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    citation_path = {}
    for arg in sys.argv[1:]:
        path, _, cits = arg.partition("=")
        stem = path[: -len(".pairs.json")] if path.endswith(".pairs.json") else path
        citation_path[path] = cits or stem + ".json"
    all_pairs = {p: json.load(open(p, encoding="utf-8")) for p in citation_path}
    source_ids = {x["musnad_hadith_id"] for ps in all_pairs.values() for x in ps if x["cited_collection"] == MUSLIM}
    with hadith_db_session() as session:
        muslim_rows = load_muslim_rows(session)
        six_texts = session.scalars(
            select(Hadith.text_ar)
            .join(HadithBook, Hadith.book_id == HadithBook.id)
            .join(HadithCollection, HadithBook.collection_id == HadithCollection.id)
            .where(HadithCollection.name.in_(SIX_BOOKS))
        ).all()
        source_text = dict(session.execute(select(Hadith.id, Hadith.text_ar).where(Hadith.id.in_(source_ids))).all())
    routes = MuslimRoutes(muslim_rows, ChainReader(learn_chain_vocabulary(six_texts)))

    for path, pairs in all_pairs.items():
        stem = path[: -len(".pairs.json")] if path.endswith(".pairs.json") else path
        citations = json.load(open(citation_path[path], encoding="utf-8"))
        refined, changes = refine(pairs, citations, routes, source_text)
        with open(stem + ".routes.json", "w", encoding="utf-8") as f:
            json.dump(changes, f, ensure_ascii=False, indent=1)
        with open(stem + ".pairs.refined.json", "w", encoding="utf-8") as f:
            json.dump(refined, f, ensure_ascii=False, indent=1)
        muslim = sum(1 for p in pairs if p["cited_collection"] == MUSLIM)
        print(f"{path}: {muslim} Muslim citations, {len(changes)} re-routed {dict(Counter(c['reason'] for c in changes))}")


if __name__ == "__main__":
    main()

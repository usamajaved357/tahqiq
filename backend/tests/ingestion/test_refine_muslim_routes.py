"""Regressions for refine_muslim_routes.py — every case is a real one found
while building the rule (docs/HADITH_CROSS_REFERENCING.md section 4e).

The fixture holds the real Sahih Muslim rows of the blocks involved (in book
order, blocks separated by empty rows) and the citing hadith text, so no
database is needed. A failing case means a rule change would put a wrong
route back into the links.
"""
import json
from pathlib import Path

import pytest

from scripts.ingestion.refine_muslim_routes import (
    ChainReader,
    MuslimRoutes,
    _tokens,
    canonical,
    raw_narrators,
    same_person,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "muslim_routes.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def routes() -> tuple[MuslimRoutes, dict[str, int]]:
    rows, id_of = [], {}
    for i, r in enumerate(FIXTURE["muslim_rows"]):
        rows.append((i, r["text"], r["abd_al_baqi"]))
        if r["number"]:
            id_of[r["number"]] = i
    return MuslimRoutes(rows, ChainReader(set(FIXTURE["vocabulary"]))), id_of


def case(name: str) -> dict:
    return next(c for c in FIXTURE["cases"] if c["name"] == name)


def choose(routes, name):
    r, id_of = routes
    c = case(name)
    new_id, reason = r.choose(c["source_text"], id_of[c["cited"]], c["hint"])
    number = next(n for n, i in id_of.items() if i == new_id)
    return number, reason


def test_route_named_in_footnote_is_chosen(routes):
    # Musnad #24840 (Mansur -> Ibrahim -> al-Aswad -> 'A'isha), footnote "من
    # طريق جرير عن منصور": was linked to 5176 (Ishaq b. Suwayd -> Mu'adha)
    assert choose(routes, "exact_route_named_in_footnote") == ("5172", "route_with_citing_tabii")


def test_father_resolution_keeps_exact_route(routes):
    # Musnad #7563 Suhayl -> Abu Salih -> Abu Hurayra; Muslim 2292 reads
    # "سهيل بن أبي صالح عن أبيه". Without resolving "عن أبيه" to Abu Salih
    # the rule moved it to 2294 (Bukayr -> Dhakwan), a different route.
    assert choose(routes, "father_resolution_keeps_exact_route") == ("2292", "keep")


def test_fragment_moves_to_full_route(routes):
    # Musnad #21118 via "عبيد الله بن موسى عن إسرائيل"; 6165 is a mid-hadith
    # split ("حدثنا أبي بن كعب …"), 6166 is exactly that route
    assert choose(routes, "fragment_to_full_route") == ("6166", "fragment_to_full_route")


def test_no_sideways_move(routes):
    # Nasa'i #4336 (Ibn 'Umar, donkey meat) -> Muslim 5008: 5009 shares no
    # more of the citing chain, so the current route is kept
    assert choose(routes, "repeated_number_stays_in_block") == ("5008", "keep_no_better_route")


def test_repeated_number_blocks_are_separate(routes):
    # Abd al-Baqi 561 is garlic at Khaybar (1248-1249) AND donkey meat after
    # 1936 (5008-5009): a link may never move from one place to the other
    r, id_of = routes
    garlic = set(r.block(id_of["1248"]))
    donkey = set(r.block(id_of["5008"]))
    assert garlic == {id_of["1248"], id_of["1249"]}
    assert donkey == {id_of["5008"], id_of["5009"]}


def test_every_case_stays_in_its_block(routes):
    r, id_of = routes
    for c in FIXTURE["cases"]:
        new_id, _ = r.choose(c["source_text"], id_of[c["cited"]], c["hint"])
        assert new_id in r.block(id_of[c["cited"]]), c["name"]


def test_two_word_given_names_must_match_whole():
    ubaydallah_b_musa = canonical(_tokens("عبيد الله بن موسى"))
    ubaydallah_b_abdallah = canonical(_tokens("عبيد الله بن عبد الله"))
    assert not same_person(ubaydallah_b_musa, ubaydallah_b_abdallah)
    assert same_person(ubaydallah_b_musa, canonical(_tokens("عبيد الله")))


def test_fathers_must_agree_when_both_given():
    assert not same_person(canonical(_tokens("سعيد بن جبير")), canonical(_tokens("سعيد بن المسيب")))
    assert same_person(canonical(_tokens("سعيد بن جبير")), canonical(_tokens("سعيد")))


def test_ubayy_is_not_the_kunya_abu():
    # "أبي بن كعب" is the companion Ubayy, not "أبو …"
    assert _tokens("ابي بن كعب")[0] == "ابي"
    assert _tokens("ابي هريرة")[0] == "ابو"


def test_unambiguous_aliases():
    assert same_person(canonical(_tokens("الزهري")), canonical(_tokens("ابن شهاب")))
    assert same_person(canonical(_tokens("ذكوان")), canonical(_tokens("ابي صالح")))
    assert same_person(canonical(_tokens("شقيق")), canonical(_tokens("ابي وائل")))


def test_report_text_is_not_read_as_a_narrator():
    # "نوفا الشامي يزعم" (Musnad #21118) is the report, not a chain member
    reader = ChainReader(set(FIXTURE["vocabulary"]))
    text = case("fragment_to_full_route")["source_text"]
    names = [" ".join(n) for n in reader.narrators(text)]
    assert not any("يزعم" in n for n in names)
    assert any(n.startswith("عبيد الله بن موسي") or n.startswith("عبيد الله بن موسى") for n in names)


def test_isnad_stops_where_the_prophet_is_named():
    narrators = raw_narrators("حدثنا مالك عن نافع عن ابن عمر ان رسول الله صلى الله عليه وسلم قال لا يبع بعضكم")
    assert [" ".join(n) for n in narrators] == ["مالك", "نافع", "بن عمر"]

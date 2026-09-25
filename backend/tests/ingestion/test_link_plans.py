"""Safety rules of the link-replacement tooling (replace_links.py,
plan_route_changes.py): a protected or re-added pair is never removed, and
every change is made in both directions."""
import json

from scripts.ingestion.plan_route_changes import pair, review_pairs
from scripts.ingestion.replace_links import load_plan, wanted_changes


def test_protected_and_added_pairs_are_never_removed(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"remove": [[2, 1], [3, 4], [5, 6], [7, 7]], "add": [[4, 3], [8, 9]], "protect": [[6, 5]]}))
    remove, add = load_plan(str(plan))
    assert remove == {(1, 2)}
    assert add == {(3, 4), (8, 9)}


def test_changes_are_symmetric():
    per_row = wanted_changes({(1, 2)}, {(2, 3)})
    assert per_row == {1: ({2}, set()), 2: ({1}, {3}), 3: (set(), {2})}


def test_pair_is_unordered():
    assert pair(9, 4) == pair(4, 9) == (4, 9)


def test_review_file_pairs(tmp_path):
    # the protect list is read from the reviewed first-pass Tuhfat files
    review = tmp_path / "review.txt"
    review.write_text(
        "=== serial ٢٩ | companion: الطفيل بن أبي بن كعب الأنصاري عن أبيه ===\n"
        "tarf: إذا كان يوم القيامة…\n"
        "  [Sunan Ibn Majah #4314] ...نص...\n"
        "  [Jami At-Tirmidhi #3613] ...نص...\n"
        "\n"
        "=== serial ٨٦٨ | companion: سلمة بن وردان ===\n"
        "  [Sunan Ibn Majah #51] ...\n"
        "  [Jami At-Tirmidhi #1993] ...\n"
        "  [Sahih Muslim #10] ...\n",
        encoding="utf-8",
    )
    got = review_pairs([str(review)])
    assert (("Jami At-Tirmidhi", "3613"), ("Sunan Ibn Majah", "4314")) in got
    assert len(got) == 1 + 3  # never across two serial blocks
    assert not any({a, b} == {("Jami At-Tirmidhi", "3613"), ("Sunan Ibn Majah", "51")} for a, b in got)

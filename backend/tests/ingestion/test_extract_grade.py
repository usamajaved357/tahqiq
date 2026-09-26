"""Al-Arna'ut's verdict is read from the OPENING of the hadith's own notes."""
import pytest

from scripts.ingestion.ingest_takhrij_book import extract_grade


@pytest.mark.parametrize(
    "footnote, grade",
    [
        ("(١) إسناده صحيح على شرط الشيخين. سفيان: هو الثوري.", "sahih"),
        ("(١) في (م): أو صمته.(٢) حديث صحيح، وهذا إسناد حسن", "sahih"),
        # #19804: the verdict opens the note; "وإسناده ضعيف" further on is another route
        ("(٣) صحيح لغيره، وهذا إسناد حسن لأجل أبي الوازع. وأخرجه … عن أبي برزة. وإسناده ضعيف.", "sahih li-ghayrihi"),
        ("(٢) حسن لغيره، وهذا إسناد ضعيف، ميمون بن أبي شبيب…", "hasan li-ghayrihi"),
        ("(١) إسناده ضعيف لانقطاعه، شريح بن عبيد لم يدرك عمر.", "da'if"),
        ("(١) حديث حسن، مطر -وهو ابن طهمان الوراق- مختلف فيه", "hasan"),
        ("(١) إِسْنَادُهُ صَحِيحٌ", "sahih"),
        # a quoted verdict of another scholar is not his
        ("(١) رجاله ثقات. وقال الترمذي: هذا حديث حسن غريب.", None),
        ("(١) إسناده قوي على شرط مسلم", None),
        ("(١) في (ظ ١٤): في.", None),
    ],
)
def test_extract_grade(footnote, grade):
    assert extract_grade(footnote) == grade

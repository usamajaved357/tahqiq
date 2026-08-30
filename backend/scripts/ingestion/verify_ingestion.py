"""Post-ingestion sanity check. Automated first pass only — the docs' separate
"sample 200 hadith, manually compare against sunnah.com" verification
sub-task still needs to happen by hand before unverified data reaches real
users beyond internal testing.

Usage: python -m scripts.ingestion.verify_ingestion
"""
from sqlalchemy import func, select

from app.models.hadith import Hadith, HadithBook, HadithCollection, HadithTranslation
from app.models.quran import Ayah, Surah, Translation
from scripts.ingestion.common import db_session

EXPECTED_SURAHS = 114
EXPECTED_AYAHS = 6236


def check_counts(session) -> None:
    surah_count = session.scalar(select(func.count()).select_from(Surah))
    ayah_count = session.scalar(select(func.count()).select_from(Ayah))
    translation_count = session.scalar(
        select(func.count(func.distinct(Translation.language_code)))
    )
    print(f"surahs: {surah_count} (expected {EXPECTED_SURAHS})")
    print(f"ayahs: {ayah_count} (expected {EXPECTED_AYAHS})")
    print(f"translation languages present: {translation_count}")

    ayahs_without_en = session.scalar(
        select(func.count())
        .select_from(Ayah)
        .outerjoin(
            Translation,
            (Translation.ayah_id == Ayah.id) & (Translation.language_code == "en"),
        )
        .where(Translation.id.is_(None))
    )
    print(f"ayahs missing an 'en' translation: {ayahs_without_en}")

    print()
    for name, count in session.execute(
        select(HadithCollection.name, func.count(Hadith.id))
        .select_from(HadithCollection)
        .join(HadithBook, HadithBook.collection_id == HadithCollection.id)
        .join(Hadith, Hadith.book_id == HadithBook.id)
        .group_by(HadithCollection.name)
    ):
        print(f"hadith in {name}: {count}")

    hadith_without_en = session.scalar(
        select(func.count())
        .select_from(Hadith)
        .outerjoin(
            HadithTranslation,
            (HadithTranslation.hadith_id == Hadith.id)
            & (HadithTranslation.language_code == "en"),
        )
        .where(HadithTranslation.id.is_(None))
    )
    print(f"hadith missing an 'en' translation: {hadith_without_en}")


def spot_check(session) -> None:
    print("\n--- spot checks ---")
    ayah = session.execute(
        select(Ayah.text_ar)
        .join(Surah, Surah.id == Ayah.surah_id)
        .where(Surah.number == 1, Ayah.ayah_number == 1)
    ).scalar_one_or_none()
    print(f"Surah 1:1 (Al-Fatiha) text_ar: {ayah}")

    hadith = session.execute(
        select(Hadith.text_ar)
        .join(HadithBook, HadithBook.id == Hadith.book_id)
        .join(HadithCollection, HadithCollection.id == HadithBook.collection_id)
        .where(HadithCollection.name == "Sahih al-Bukhari", Hadith.hadith_number == "1")
    ).scalar_one_or_none()
    print(f"Bukhari hadith #1 text_ar: {hadith}")


def main() -> None:
    with db_session() as session:
        check_counts(session)
        spot_check(session)


if __name__ == "__main__":
    main()

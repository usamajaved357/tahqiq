"""Exhaustive field-by-field integrity check: re-fetches every source record
used by ingest_quran.py / ingest_hadith.py and compares it character-for-
character against what's actually stored in the database. Checks every row,
not a sample.

This verifies the ingestion pipeline didn't drop, duplicate, truncate, or
corrupt anything relative to the source APIs — it does NOT verify that the
source data itself is scholarly correct. That's the separate manual sample
comparison against sunnah.com / printed editions described in
docs/project-documentation.md Section 13, which needs a human, not a script.

Usage: python -u -m scripts.ingestion.verify_full_integrity
"""
import sys

import httpx
from sqlalchemy import select

from app.models.hadith import Hadith, HadithBook, HadithCollection, HadithGrading, HadithTranslation
from app.models.quran import Ayah, Surah, Tafsir, Translation
from scripts.ingestion.common import db_session
from scripts.ingestion.ingest_hadith import (
    COLLECTIONS,
    LANGUAGE_PREFIXES,
    SAHIHAYN,
    TRANSLATION_LANGUAGES,
    fetch_edition,
)
from scripts.ingestion.ingest_quran import ARABIC_EDITION, TRANSLATION_EDITIONS, fetch
from scripts.ingestion.ingest_tafsir import EDITIONS as TAFSIR_EDITIONS
from scripts.ingestion.ingest_tafsir import normalize as normalize_tafsir

mismatches = []
checked = 0


def record_mismatch(where: str, detail: str) -> None:
    mismatches.append(f"{where}: {detail}")


def verify_quran(session) -> None:
    global checked
    print("=== Quran ===")

    source_surahs = fetch("surah")
    db_surahs = {s.number: s for s in session.scalars(select(Surah))}
    if len(source_surahs) != len(db_surahs):
        record_mismatch("surahs", f"source has {len(source_surahs)}, db has {len(db_surahs)}")
    for s in source_surahs:
        db_s = db_surahs.get(s["number"])
        if db_s is None:
            record_mismatch("surahs", f"surah {s['number']} missing from db")
            continue
        checked += 1
        if db_s.name_ar != s["name"]:
            record_mismatch("surahs", f"surah {s['number']} name_ar mismatch")
        if db_s.name_en != s["englishName"]:
            record_mismatch("surahs", f"surah {s['number']} name_en mismatch")
        if db_s.revelation_type != s["revelationType"].lower():
            record_mismatch("surahs", f"surah {s['number']} revelation_type mismatch")
    print(f"  surahs checked: {len(source_surahs)}")

    ar_data = fetch(f"quran/{ARABIC_EDITION}")
    db_ayahs = {
        (a.surah_number, a.ayah_number): a
        for a in session.execute(
            select(Ayah.ayah_number, Ayah.text_ar, Ayah.juz, Ayah.page, Surah.number.label("surah_number"))
            .join(Surah, Surah.id == Ayah.surah_id)
        )
    }
    ayah_count = 0
    for surah in ar_data["surahs"]:
        for ayah in surah["ayahs"]:
            key = (surah["number"], ayah["numberInSurah"])
            db_a = db_ayahs.get(key)
            ayah_count += 1
            if db_a is None:
                record_mismatch("ayahs", f"{key} missing from db")
                continue
            if db_a.text_ar != ayah["text"]:
                record_mismatch("ayahs", f"{key} text_ar mismatch")
            if db_a.juz != ayah["juz"] or db_a.page != ayah["page"]:
                record_mismatch("ayahs", f"{key} juz/page mismatch")
    print(f"  ayahs checked: {ayah_count}")

    for lang, edition in TRANSLATION_EDITIONS.items():
        data = fetch(f"quran/{edition}")
        translator = data["edition"]["englishName"]
        db_trans = {
            (a_num, s_num): t
            for t, a_num, s_num in session.execute(
                select(Translation.text, Ayah.ayah_number, Surah.number)
                .join(Ayah, Ayah.id == Translation.ayah_id)
                .join(Surah, Surah.id == Ayah.surah_id)
                .where(Translation.language_code == lang, Translation.translator == translator)
            )
        }
        n = 0
        for surah in data["surahs"]:
            for ayah in surah["ayahs"]:
                key = (ayah["numberInSurah"], surah["number"])
                n += 1
                db_text = db_trans.get(key)
                if db_text is None:
                    record_mismatch(f"translations[{lang}]", f"{key} missing from db")
                elif db_text != ayah["text"]:
                    record_mismatch(f"translations[{lang}]", f"{key} text mismatch")
        print(f"  '{lang}' translations checked: {n}")


def verify_tafsir(session) -> None:
    global checked
    print("\n=== Tafsir ===")
    for lang, (edition_slug, source_name) in TAFSIR_EDITIONS.items():
        db_text = {
            (a_num, s_num): t
            for t, a_num, s_num in session.execute(
                select(Tafsir.text, Ayah.ayah_number, Surah.number)
                .join(Ayah, Ayah.id == Tafsir.ayah_id)
                .join(Surah, Surah.id == Ayah.surah_id)
                .where(Tafsir.language_code == lang, Tafsir.source_name == source_name)
            ).all()
        }
        n = 0
        for surah_number in range(1, 115):
            resp = httpx.get(
                f"https://cdn.jsdelivr.net/gh/spa5k/tafsir_api@main/tafsir/{edition_slug}/{surah_number}.json",
                timeout=60,
            )
            resp.raise_for_status()
            for entry in normalize_tafsir(resp.json()):
                key = (entry["ayah"], entry["surah"])
                n += 1
                text = db_text.get(key)
                if text is None:
                    record_mismatch(f"tafsir[{lang}]", f"{key} missing from db")
                elif text != entry["text"]:
                    record_mismatch(f"tafsir[{lang}]", f"{key} text mismatch")
        checked += n
        print(f"  '{lang}' tafsir checked: {n}")


def verify_hadith(session) -> None:
    global checked
    print("\n=== Hadith ===")
    for slug, (name, collection_slug) in COLLECTIONS.items():
        editions = {
            lang: fetch_edition(f"{prefix}-{collection_slug}") for lang, prefix in LANGUAGE_PREFIXES.items()
        }
        en_data = editions["en"]
        by_number_per_lang = {
            lang: {h["hadithnumber"]: h for h in data["hadiths"]} for lang, data in editions.items()
        }

        collection = session.scalar(select(HadithCollection).where(HadithCollection.name == name))
        if collection is None:
            record_mismatch(name, "collection missing from db entirely")
            continue

        db_hadith = {
            h_num: (h_id, text_ar)
            for h_id, h_num, text_ar in session.execute(
                select(Hadith.id, Hadith.hadith_number, Hadith.text_ar)
                .join(HadithBook, HadithBook.id == Hadith.book_id)
                .where(HadithBook.collection_id == collection.id)
            )
        }
        db_translations: dict[str, dict[int, str]] = {}
        for lang in TRANSLATION_LANGUAGES:
            db_translations[lang] = dict(
                session.execute(
                    select(Hadith.id, HadithTranslation.text)
                    .join(HadithTranslation, HadithTranslation.hadith_id == Hadith.id)
                    .join(HadithBook, HadithBook.id == Hadith.book_id)
                    .where(HadithBook.collection_id == collection.id, HadithTranslation.language_code == lang)
                ).all()
            )
        db_gradings: dict[int, set] = {}
        for h_id, grader, grade in session.execute(
            select(Hadith.id, HadithGrading.grader_name, HadithGrading.grade)
            .join(HadithGrading, HadithGrading.hadith_id == Hadith.id)
            .join(HadithBook, HadithBook.id == Hadith.book_id)
            .where(HadithBook.collection_id == collection.id)
        ):
            db_gradings.setdefault(h_id, set()).add((grader, grade))

        source_numbers = [h["hadithnumber"] for h in en_data["hadiths"]]
        if len(source_numbers) != len(set(source_numbers)):
            dupes = {x for x in source_numbers if source_numbers.count(x) > 1}
            record_mismatch(name, f"source has duplicate hadithnumber values: {dupes}")

        n = 0
        for h in en_data["hadiths"]:
            number = h["hadithnumber"]
            n += 1
            entry = db_hadith.get(str(number))
            if entry is None:
                record_mismatch(name, f"hadith #{number} missing from db")
                continue
            h_id, db_text_ar = entry

            ar_h = by_number_per_lang["ar"].get(number)
            expected_ar = ar_h["text"] if ar_h else None
            if db_text_ar != expected_ar:
                record_mismatch(name, f"hadith #{number} text_ar mismatch")

            for lang in TRANSLATION_LANGUAGES:
                lang_h = by_number_per_lang[lang].get(number)
                if lang_h is None:
                    continue
                if db_translations[lang].get(h_id) != lang_h["text"]:
                    record_mismatch(name, f"hadith #{number} {lang} translation mismatch")

            actual_gradings = db_gradings.get(h_id, set())
            if slug in SAHIHAYN:
                expected_gradings = {("Ijma (Sahihayn)", "sahih")}
            else:
                expected_gradings = {(g["name"], g["grade"]) for g in (h.get("grades") or [])}
            if actual_gradings != expected_gradings:
                record_mismatch(
                    name,
                    f"hadith #{number} gradings mismatch: db={actual_gradings} expected={expected_gradings}",
                )

        checked += n
        print(f"  {name}: {n} hadith checked")


def main() -> None:
    with db_session() as session:
        verify_quran(session)
        verify_tafsir(session)
        verify_hadith(session)

    print(f"\n{'=' * 50}")
    print(f"Total records checked: {checked}")
    if mismatches:
        print(f"MISMATCHES FOUND: {len(mismatches)}")
        for m in mismatches[:50]:
            print(f"  - {m}")
        if len(mismatches) > 50:
            print(f"  ... and {len(mismatches) - 50} more")
        sys.exit(1)
    else:
        print("No mismatches. Every field in the database matches the source API exactly.")


if __name__ == "__main__":
    main()

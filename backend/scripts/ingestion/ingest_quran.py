"""Phase 1 ingestion: Quran text + translations from the Al Quran Cloud API.

Populates `surahs`, `ayahs`, and `translations`. The Arabic text itself lives
in `ayahs.text_ar` — there is no separate 'ar' translation row, since the
source text already is Arabic.

Language set (en/ur/id/bn/tr) chosen by Muslim population by country (Pew
Research: Indonesia 242M, Pakistan/Urdu already covered, Bangladesh 150M,
Turkey 84M), constrained to languages also available in the hadith source
(fawazahmed0) so Quran and Hadith stay in sync — Hindi and Persian were
considered but aren't available there.

Usage: python -m scripts.ingestion.ingest_quran
"""
import httpx

from app.models.quran import Ayah, Surah, Translation
from scripts.ingestion.common import db_session, upsert_many, upsert_many_returning, upsert_one

API_BASE = "https://api.alquran.cloud/v1"
ARABIC_EDITION = "quran-uthmani"
TRANSLATION_EDITIONS = {
    "en": "en.sahih",
    "ur": "ur.jalandhry",
    "id": "id.indonesian",
    "bn": "bn.bengali",
    "tr": "tr.diyanet",
}


def fetch(path: str) -> dict:
    resp = httpx.get(f"{API_BASE}/{path}", timeout=60)
    resp.raise_for_status()
    return resp.json()["data"]


def ingest_surahs(session) -> dict[int, int]:
    surahs = fetch("surah")
    surah_id_by_number = {}
    for s in surahs:
        surah_id = upsert_one(
            session,
            Surah,
            {
                "number": s["number"],
                "name_ar": s["name"],
                "name_en": s["englishName"],
                "revelation_type": s["revelationType"].lower(),
            },
            index_elements=["number"],
        )
        surah_id_by_number[s["number"]] = surah_id
    session.commit()
    print(f"Ingested {len(surah_id_by_number)} surahs")
    return surah_id_by_number


def ingest_ayahs(session, surah_id_by_number: dict[int, int]) -> dict[tuple[int, int], int]:
    data = fetch(f"quran/{ARABIC_EDITION}")
    rows = []
    for surah in data["surahs"]:
        surah_id = surah_id_by_number[surah["number"]]
        for ayah in surah["ayahs"]:
            rows.append(
                {
                    "surah_id": surah_id,
                    "ayah_number": ayah["numberInSurah"],
                    "text_ar": ayah["text"],
                    "juz": ayah["juz"],
                    "page": ayah["page"],
                    "_surah_number": surah["number"],
                }
            )

    surah_number_by_key = {
        (r["surah_id"], r["ayah_number"]): r.pop("_surah_number") for r in rows
    }
    returned = upsert_many_returning(session, Ayah, rows, index_elements=["surah_id", "ayah_number"])
    ayah_id_by_key: dict[tuple[int, int], int] = {
        (surah_number_by_key[(surah_id, ayah_number)], ayah_number): ayah_id
        for ayah_id, surah_id, ayah_number in returned
    }
    session.commit()
    print(f"Ingested {len(ayah_id_by_key)} ayahs")
    return ayah_id_by_key


def ingest_translations(session, ayah_id_by_key: dict[tuple[int, int], int]) -> None:
    for language_code, edition in TRANSLATION_EDITIONS.items():
        data = fetch(f"quran/{edition}")
        translator = data["edition"]["englishName"]
        rows = []
        for surah in data["surahs"]:
            for ayah in surah["ayahs"]:
                ayah_id = ayah_id_by_key.get((surah["number"], ayah["numberInSurah"]))
                if ayah_id is None:
                    continue
                rows.append(
                    {
                        "ayah_id": ayah_id,
                        "translator": translator,
                        "language_code": language_code,
                        "text": ayah["text"],
                    }
                )
        upsert_many(session, Translation, rows, index_elements=["ayah_id", "translator"])
        session.commit()
        print(f"Ingested {len(rows)} '{language_code}' translations ({translator})")


def main() -> None:
    with db_session() as session:
        surah_id_by_number = ingest_surahs(session)
        ayah_id_by_key = ingest_ayahs(session, surah_id_by_number)
        ingest_translations(session, ayah_id_by_key)


if __name__ == "__main__":
    main()

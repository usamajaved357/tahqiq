"""Phase 1 ingestion: Quran tafsir (commentary) from spa5k/tafsir_api.

Populates `tafsirs`, keyed off the already-ingested `ayahs` table (run
ingest_quran.py first). Scoped to a single classical source — Tafsir Ibn
Kathir — across Arabic, English, and Urdu, matching the AR/EN/UR scope used
for Quran and Hadith. Fetched per-surah (114 requests per edition) rather
than per-ayah (6,236 requests) since the source supports it.

Some surahs are multi-megabyte per edition (Ibn Kathir's commentary is
lengthy, e.g. Al-Baqarah runs several MB), so the 342 surah/edition fetches
are done concurrently rather than one at a time.

Usage: python -u -m scripts.ingestion.ingest_tafsir
"""
import asyncio

import httpx
from sqlalchemy import select

from app.models.quran import Ayah, Surah, Tafsir
from scripts.ingestion.common import db_session, upsert_many

CDN_BASE = "https://cdn.jsdelivr.net/gh/spa5k/tafsir_api@main/tafsir"
CONCURRENCY = 10

# language_code -> (edition slug, display name)
EDITIONS = {
    "ar": ("ar-tafsir-ibn-kathir", "Tafsir Ibn Kathir"),
    "en": ("en-tafisr-ibn-kathir", "Tafsir Ibn Kathir"),
    "ur": ("ur-tafseer-ibn-e-kaseer", "Tafsir Ibn Kathir"),
}


def normalize(data) -> list[dict]:
    # Response shape varies by source: either a flat list of {text, ayah, surah},
    # or {"ayahs": [...]} wrapping the same per-ayah objects.
    return data["ayahs"] if isinstance(data, dict) else data


async def fetch_all(edition_slug: str) -> dict[int, list[dict]]:
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: dict[int, list[dict]] = {}

    async with httpx.AsyncClient(timeout=60, limits=limits) as client:

        async def fetch_one(surah_number: int) -> None:
            async with semaphore:
                resp = await client.get(f"{CDN_BASE}/{edition_slug}/{surah_number}.json")
                resp.raise_for_status()
                results[surah_number] = normalize(resp.json())

        await asyncio.gather(*(fetch_one(n) for n in range(1, 115)))

    return results


def ingest_edition(session, lang: str, edition_slug: str, source_name: str, ayah_id_by_key: dict) -> int:
    by_surah = asyncio.run(fetch_all(edition_slug))
    rows = []
    for surah_number in range(1, 115):
        for entry in by_surah[surah_number]:
            key = (entry["surah"], entry["ayah"])
            ayah_id = ayah_id_by_key.get(key)
            if ayah_id is None:
                continue
            rows.append(
                {
                    "ayah_id": ayah_id,
                    "source_name": source_name,
                    "language_code": lang,
                    "text": entry["text"],
                }
            )
    upsert_many(session, Tafsir, rows, index_elements=["ayah_id", "source_name", "language_code"])
    session.commit()
    return len(rows)


def main() -> None:
    with db_session() as session:
        ayah_id_by_key = {
            (surah_number, ayah_number): ayah_id
            for ayah_id, ayah_number, surah_number in session.execute(
                select(Ayah.id, Ayah.ayah_number, Surah.number).join(Surah, Surah.id == Ayah.surah_id)
            )
        }
        if not ayah_id_by_key:
            raise RuntimeError("ayahs table is empty — run ingest_quran.py first")

        for lang, (edition_slug, source_name) in EDITIONS.items():
            n = ingest_edition(session, lang, edition_slug, source_name, ayah_id_by_key)
            print(f"Ingested {n} '{lang}' tafsir entries ({source_name})")


if __name__ == "__main__":
    main()

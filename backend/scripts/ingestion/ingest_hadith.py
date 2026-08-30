"""Phase 1 ingestion: Hadith text + translations + gradings from the
fawazahmed0/hadith-api bootstrap source (per docs/project-documentation.md
Section 13). Every row is tagged source_dataset='fawazahmed0-hadith-api' and
verification_status='unverified' — this data has not yet been cross-checked
against sunnah.com or a printed edition (see the bootstrap-verification
sub-task in the docs; that manual sampling step is separate from this script).

Scoped to the six major collections for now: Bukhari, Muslim, Abu Dawud,
Tirmidhi, Nasa'i, Ibn Majah.

Usage: python -m scripts.ingestion.ingest_hadith
"""
import httpx

from app.models.hadith import (
    Hadith,
    HadithBook,
    HadithCollection,
    HadithGrading,
    HadithTranslation,
)
from scripts.ingestion.common import db_session, upsert_many, upsert_many_returning, upsert_one

CDN_BASE = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
SOURCE_DATASET = "fawazahmed0-hadith-api"

# collection slug -> (display name, arabic edition, english edition, urdu edition)
COLLECTIONS = {
    "bukhari": ("Sahih al-Bukhari", "ara-bukhari", "eng-bukhari", "urd-bukhari"),
    "muslim": ("Sahih Muslim", "ara-muslim", "eng-muslim", "urd-muslim"),
    "abudawud": ("Sunan Abu Dawud", "ara-abudawud", "eng-abudawud", "urd-abudawud"),
    "tirmidhi": ("Jami At-Tirmidhi", "ara-tirmidhi", "eng-tirmidhi", "urd-tirmidhi"),
    "nasai": ("Sunan an-Nasa'i", "ara-nasai", "eng-nasai", "urd-nasai"),
    "ibnmajah": ("Sunan Ibn Majah", "ara-ibnmajah", "eng-ibnmajah", "urd-ibnmajah"),
}

# Bukhari/Muslim (the Sahihayn) carry no per-hadith grades in the source —
# they're collectively authenticated by scholarly consensus rather than
# individually re-graded (docs Section 3).
SAHIHAYN = {"bukhari", "muslim"}


def fetch_edition(edition: str) -> dict:
    resp = httpx.get(f"{CDN_BASE}/{edition}.min.json", timeout=120)
    resp.raise_for_status()
    return resp.json()


def ingest_books(session, collection_id: int, sections: dict) -> dict[int, int]:
    book_id_by_number: dict[int, int] = {}
    for number_str, title in sections.items():
        number = int(number_str)
        book_id = upsert_one(
            session,
            HadithBook,
            {
                "collection_id": collection_id,
                "book_number": number,
                "name_en": title or None,
            },
            index_elements=["collection_id", "book_number"],
        )
        book_id_by_number[number] = book_id
    return book_id_by_number


def ingest_collection(session, slug: str, name: str, ar_ed: str, en_ed: str, ur_ed: str) -> None:
    ar_data = fetch_edition(ar_ed)
    en_data = fetch_edition(en_ed)
    ur_data = fetch_edition(ur_ed)

    collection_id = upsert_one(
        session,
        HadithCollection,
        {
            "name": name,
            "name_ar": ar_data["metadata"].get("name"),
            "total_hadith": len(en_data["hadiths"]),
        },
        index_elements=["name"],
    )
    session.commit()

    # Book (section) titles: prefer the English edition's titles, fall back
    # to Arabic-only naming if a book only appears there.
    book_id_by_number = ingest_books(session, collection_id, en_data["metadata"]["sections"])
    session.commit()

    ar_by_number = {h["hadithnumber"]: h for h in ar_data["hadiths"]}
    ur_by_number = {h["hadithnumber"]: h for h in ur_data["hadiths"]}

    hadith_rows = []
    for h in en_data["hadiths"]:
        book_id = book_id_by_number.get(h["reference"]["book"])
        if book_id is None:
            continue
        ar_h = ar_by_number.get(h["hadithnumber"])
        hadith_rows.append(
            {
                "book_id": book_id,
                "hadith_number": str(h["hadithnumber"]),
                "text_ar": ar_h["text"] if ar_h else None,
                "source_dataset": SOURCE_DATASET,
                "verification_status": "unverified",
                "_global_number": h["hadithnumber"],
            }
        )

    global_number_by_book_and_local = {
        (r["book_id"], r["hadith_number"]): r.pop("_global_number") for r in hadith_rows
    }
    returned = upsert_many_returning(
        session, Hadith, hadith_rows, index_elements=["book_id", "hadith_number"]
    )
    hadith_id_by_number: dict[int, int] = {
        global_number_by_book_and_local[(book_id, hadith_number)]: hadith_id
        for hadith_id, book_id, hadith_number in returned
    }
    session.commit()
    print(f"  {name}: {len(hadith_id_by_number)} hadith")

    translation_rows = []
    en_by_number = {h["hadithnumber"]: h for h in en_data["hadiths"]}
    for number, hadith_id in hadith_id_by_number.items():
        en_h = en_by_number.get(number)
        if en_h:
            translation_rows.append({"hadith_id": hadith_id, "language_code": "en", "text": en_h["text"]})
        ur_h = ur_by_number.get(number)
        if ur_h:
            translation_rows.append({"hadith_id": hadith_id, "language_code": "ur", "text": ur_h["text"]})
    upsert_many(session, HadithTranslation, translation_rows, index_elements=["hadith_id", "language_code"])
    session.commit()
    print(f"  {name}: {len(translation_rows)} translation rows")

    grading_rows = []
    if slug in SAHIHAYN:
        for hadith_id in hadith_id_by_number.values():
            grading_rows.append(
                {"hadith_id": hadith_id, "grader_name": "Ijma (Sahihayn)", "grade": "sahih"}
            )
    else:
        for number, hadith_id in hadith_id_by_number.items():
            en_h = en_by_number.get(number)
            for g in (en_h.get("grades") or []) if en_h else []:
                grading_rows.append(
                    {
                        "hadith_id": hadith_id,
                        "grader_name": g["name"],
                        "grade": g["grade"],
                    }
                )
    upsert_many(session, HadithGrading, grading_rows, index_elements=["hadith_id", "grader_name"])
    session.commit()
    print(f"  {name}: {len(grading_rows)} grading rows")


def main() -> None:
    with db_session() as session:
        for slug, (name, ar_ed, en_ed, ur_ed) in COLLECTIONS.items():
            print(f"Ingesting {name}...")
            ingest_collection(session, slug, name, ar_ed, en_ed, ur_ed)


if __name__ == "__main__":
    main()

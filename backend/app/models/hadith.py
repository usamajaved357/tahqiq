from sqlalchemy import ARRAY, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class HadithCollection(Base):
    __tablename__ = "hadith_collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_ar: Mapped[str | None] = mapped_column(Text)
    total_hadith: Mapped[int | None] = mapped_column(Integer)


class HadithBook(Base):
    __tablename__ = "hadith_books"
    __table_args__ = (UniqueConstraint("collection_id", "book_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("hadith_collections.id"))
    book_number: Mapped[int | None] = mapped_column(Integer)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_ar: Mapped[str | None] = mapped_column(Text)


class Hadith(Base):
    __tablename__ = "hadiths"
    __table_args__ = (UniqueConstraint("book_id", "hadith_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("hadith_books.id"))
    hadith_number: Mapped[str] = mapped_column(Text, nullable=False)
    text_ar: Mapped[str | None] = mapped_column(Text)
    narrator_chain: Mapped[str | None] = mapped_column(Text)
    topic_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    related_hadith_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    source_dataset: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(Text, default="unverified")


class HadithGrading(Base):
    __tablename__ = "hadith_gradings"
    __table_args__ = (UniqueConstraint("hadith_id", "grader_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hadith_id: Mapped[int] = mapped_column(ForeignKey("hadiths.id"))
    grader_name: Mapped[str] = mapped_column(Text, nullable=False)
    grade: Mapped[str] = mapped_column(Text, nullable=False)
    source_work: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class HadithTranslation(Base):
    __tablename__ = "hadith_translations"
    __table_args__ = (UniqueConstraint("hadith_id", "language_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hadith_id: Mapped[int] = mapped_column(ForeignKey("hadiths.id"))
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

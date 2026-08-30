from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Surah(Base):
    __tablename__ = "surahs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name_ar: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)
    revelation_type: Mapped[str | None] = mapped_column(Text)


class Ayah(Base):
    __tablename__ = "ayahs"
    __table_args__ = (UniqueConstraint("surah_id", "ayah_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    surah_id: Mapped[int] = mapped_column(ForeignKey("surahs.id"))
    ayah_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text_ar: Mapped[str] = mapped_column(Text, nullable=False)
    juz: Mapped[int | None] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)


class Translation(Base):
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("ayah_id", "translator"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ayah_id: Mapped[int] = mapped_column(ForeignKey("ayahs.id"))
    translator: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class Tafsir(Base):
    __tablename__ = "tafsirs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ayah_id: Mapped[int] = mapped_column(ForeignKey("ayahs.id"))
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

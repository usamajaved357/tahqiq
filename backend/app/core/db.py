from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Hadith domain lives on its own free-tier Postgres project (Neon), split out
# from the primary DB (Quran/tafsir/embeddings/users on Supabase) to fit both
# within free storage limits — see project notes on the two-database split.
# Same declarative Base/metadata is shared across both; only hadith_* tables
# are ever created/queried through HadithSessionLocal.
# pool_pre_ping: Neon closes idle connections server-side; without it, the
# first query after a long idle stretch fails with "SSL connection has been
# closed unexpectedly" on a stale pooled connection.
hadith_engine = create_engine(settings.HADITH_DATABASE_URL, pool_pre_ping=True)
HadithSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=hadith_engine)


class Base(DeclarativeBase):
    pass

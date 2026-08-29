from fastapi import FastAPI

app = FastAPI(title="Tahqiq API")


@app.get("/health")
def health():
    return {"status": "ok"}


# Route modules are registered here as they're built (Phase 4):
# from app.api.routes import search, quran, hadith, user, billing
# app.include_router(search.router, prefix="/api")

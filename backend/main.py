"""
LinguaForge backend — FastAPI + SQLite
Storage layout:
  vocabulary  — id, word, translation, language_from, language_to, source, created_at
  stats       — vocab_id, correct_count, wrong_count, ease_factor, interval_days, due_date, last_seen
  performance — id, vocab_id, correct, mode, answered_at
"""

from __future__ import annotations

import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("LINGUAFORGE_DB", "/data/linguaforge.db"))

# ── Database helpers ──────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vocabulary (
                id            TEXT PRIMARY KEY,
                word          TEXT NOT NULL,
                translation   TEXT NOT NULL DEFAULT '',
                language_from TEXT NOT NULL DEFAULT 'en',
                language_to   TEXT NOT NULL DEFAULT 'unknown',
                source        TEXT NOT NULL DEFAULT 'manual',
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats (
                vocab_id      TEXT PRIMARY KEY REFERENCES vocabulary(id),
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count   INTEGER NOT NULL DEFAULT 0,
                ease_factor   REAL    NOT NULL DEFAULT 2.5,
                interval_days INTEGER NOT NULL DEFAULT 1,
                due_date      TEXT    NOT NULL,
                last_seen     TEXT
            );
            CREATE TABLE IF NOT EXISTS performance (
                id          TEXT PRIMARY KEY,
                vocab_id    TEXT NOT NULL,
                correct     INTEGER NOT NULL,
                mode        TEXT NOT NULL DEFAULT 'flashcard',
                answered_at TEXT NOT NULL
            );
        """)

def _today() -> str:
    return date.today().isoformat()

def _due_date(interval_days: int) -> str:
    return (date.today() + timedelta(days=interval_days)).isoformat()

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Pydantic models ───────────────────────────────────────────────────────────

class VocabItem(BaseModel):
    word: str
    translation: str
    language_from: str = "en"
    language_to: str = "unknown"

class VocabBulk(BaseModel):
    items: list[VocabItem]
    source: str = "manual"

class VocabUpdate(BaseModel):
    word: str
    translation: str = ""
    language_from: str = "en"
    language_to: str = "unknown"

class AnswerPayload(BaseModel):
    vocab_id: str
    correct: bool
    mode: str = "flashcard"

class DuolingoSync(BaseModel):
    username: str
    password: Optional[str] = None
    jwt: Optional[str] = None

# ── Duolingo helpers ──────────────────────────────────────────────────────────

DUO_BASE = "https://www.duolingo.com"
_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

async def _duo_login(username: str, password: str) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.post(
            f"{DUO_BASE}/login",
            json={"login": username, "password": password, "locale": "en", "age": "20"},
            headers={**_HDR, "Content-Type": "application/json"},
        )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=401, detail="Duolingo login failed — check credentials.")
    data = r.json()
    return {
        "jwt": r.cookies.get("jwt_token") or data.get("jwt_token", ""),
        "user_id": data.get("user_id") or data.get("id"),
        "username": data.get("username", username),
    }

async def _duo_user_id(username: str, jwt: str) -> Optional[int]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"{DUO_BASE}/users?fields=id,username&username={username}",
            headers={**_HDR, "Authorization": f"Bearer {jwt}"},
            cookies={"jwt_token": jwt},
        )
    if r.status_code == 200:
        users = r.json().get("users", [])
        if users:
            return users[0].get("id")
    return None

async def _duo_fetch_vocab(user_id: int, jwt: str) -> list[dict]:
    headers = {**_HDR, "Authorization": f"Bearer {jwt}"}
    cookies = {"jwt_token": jwt}
    items: list[dict] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(f"{DUO_BASE}/vocabulary/overview", headers=headers, cookies=cookies)
        if r.status_code == 200:
            data = r.json()
            lang = data.get("learning_language", "unknown")
            for v in data.get("vocab_overview", []):
                word = (v.get("word_string") or "").strip()
                if word:
                    items.append({
                        "word": word,
                        "translation": v.get("normalized_string", word),
                        "language_to": lang,
                    })

        if not items:
            r2 = await c.get(
                f"{DUO_BASE}/api/1/users/{user_id}/",
                headers=headers, cookies=cookies,
                params={"fields": "language_data"},
            )
            if r2.status_code == 200:
                for lang_code, lang_data in r2.json().get("language_data", {}).items():
                    for skill in lang_data.get("skills", []):
                        for w in skill.get("words", []):
                            word = w if isinstance(w, str) else w.get("word_string", "")
                            if word:
                                items.append({"word": word, "translation": "", "language_to": lang_code})

    return items

# ── Shared upsert helper ──────────────────────────────────────────────────────

def _upsert_word(word: str, translation: str, language_from: str,
                 language_to: str, source: str) -> tuple[str, bool]:
    """Returns (doc_id, created). Skips duplicates."""
    with _get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM vocabulary WHERE word = ? AND language_to = ? LIMIT 1",
            (word, language_to)
        ).fetchone()
        if existing:
            return existing["id"], False

        doc_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO vocabulary (id, word, translation, language_from, language_to, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, word, translation, language_from, language_to, source, datetime.utcnow().isoformat())
        )
        conn.execute(
            "INSERT INTO stats (vocab_id, correct_count, wrong_count, ease_factor, interval_days, due_date, last_seen) "
            "VALUES (?, 0, 0, 2.5, 1, ?, NULL)",
            (doc_id, _today())
        )
        return doc_id, True

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/api/sync/duolingo")
async def sync_duolingo(payload: DuolingoSync):
    jwt_token = ""
    user_id = None
    display_user = payload.username

    if payload.jwt:
        jwt_token = payload.jwt.strip()
        user_id = await _duo_user_id(payload.username, jwt_token)
    elif payload.password:
        auth = await _duo_login(payload.username, payload.password)
        jwt_token, user_id, display_user = auth["jwt"], auth["user_id"], auth["username"]
    else:
        raise HTTPException(status_code=400, detail="Provide a password or JWT token.")

    if not user_id:
        raise HTTPException(status_code=404, detail=f"Could not resolve user '{payload.username}'.")

    try:
        vocab_items = await _duo_fetch_vocab(user_id, jwt_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch vocabulary: {e}")

    imported = skipped = 0
    for item in vocab_items:
        if not item.get("word"):
            continue
        _, created = _upsert_word(item["word"], item.get("translation", ""), "en",
                                  item["language_to"], "duolingo")
        imported += created
        skipped += not created

    return {
        "status": "ok", "imported": imported, "skipped": skipped, "user": display_user,
        "note": (f"Fetched {len(vocab_items)} words from Duolingo." if imported
                 else "All words already existed, or Duolingo returned none. Try paste-import."),
    }


@app.get("/api/vocab")
def list_vocab(language: Optional[str] = None):
    with _get_db() as conn:
        if language:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE v.language_to = ? ORDER BY lower(v.word)",
                (language,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "ORDER BY lower(v.word)"
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/vocab/languages")
def list_languages():
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT language_to, COUNT(*) as count FROM vocabulary GROUP BY language_to ORDER BY language_to"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/vocab/due")
def get_due_vocab(language: Optional[str] = None, limit: int = 50):
    today = _today()
    with _get_db() as conn:
        if language:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.interval_days, s.due_date, s.last_seen "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE v.language_to = ? AND (s.due_date IS NULL OR s.due_date <= ?) "
                "ORDER BY s.due_date LIMIT ?",
                (language, today, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.interval_days, s.due_date, s.last_seen "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE s.due_date IS NULL OR s.due_date <= ? "
                "ORDER BY s.due_date LIMIT ?",
                (today, limit)
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/vocab/weak")
def get_weak_vocab(language: Optional[str] = None, limit: int = 50):
    with _get_db() as conn:
        if language:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date, "
                "CAST(s.correct_count AS REAL) / (s.correct_count + s.wrong_count) AS accuracy "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE v.language_to = ? AND (s.correct_count + s.wrong_count) > 0 "
                "ORDER BY accuracy ASC, s.wrong_count DESC LIMIT ?",
                (language, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date, "
                "CAST(s.correct_count AS REAL) / (s.correct_count + s.wrong_count) AS accuracy "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE (s.correct_count + s.wrong_count) > 0 "
                "ORDER BY accuracy ASC, s.wrong_count DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/vocab/strong")
def get_strong_vocab(language: Optional[str] = None, limit: int = 50):
    with _get_db() as conn:
        if language:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date, "
                "CAST(s.correct_count AS REAL) / (s.correct_count + s.wrong_count) AS accuracy "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE v.language_to = ? AND (s.correct_count + s.wrong_count) >= 3 "
                "ORDER BY accuracy DESC, s.correct_count DESC LIMIT ?",
                (language, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT v.*, s.correct_count, s.wrong_count, s.ease_factor, s.due_date, "
                "CAST(s.correct_count AS REAL) / (s.correct_count + s.wrong_count) AS accuracy "
                "FROM vocabulary v LEFT JOIN stats s ON v.id = s.vocab_id "
                "WHERE (s.correct_count + s.wrong_count) >= 3 "
                "ORDER BY accuracy DESC, s.correct_count DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/vocab")
def add_vocab(item: VocabItem):
    doc_id, created = _upsert_word(
        item.word, item.translation, item.language_from, item.language_to, "manual"
    )
    return {"id": doc_id, "status": "created" if created else "exists"}


@app.post("/api/vocab/bulk")
def add_vocab_bulk(payload: VocabBulk):
    imported = skipped = 0
    for item in payload.items:
        _, created = _upsert_word(item.word, item.translation,
                                  item.language_from, item.language_to, payload.source)
        if created:
            imported += 1
        else:
            skipped += 1
    return {"imported": imported, "skipped": skipped}


@app.delete("/api/vocab/{vocab_id}")
def delete_vocab(vocab_id: str):
    with _get_db() as conn:
        conn.execute("DELETE FROM performance WHERE vocab_id = ?", (vocab_id,))
        conn.execute("DELETE FROM stats WHERE vocab_id = ?", (vocab_id,))
        conn.execute("DELETE FROM vocabulary WHERE id = ?", (vocab_id,))
    return {"status": "deleted"}


@app.delete("/api/vocab")
def clear_all_vocab():
    with _get_db() as conn:
        deleted = conn.execute("SELECT COUNT(*) FROM vocabulary").fetchone()[0]
        conn.execute("DELETE FROM performance")
        conn.execute("DELETE FROM stats")
        conn.execute("DELETE FROM vocabulary")
    return {"status": "cleared", "deleted": deleted}


@app.patch("/api/vocab/{vocab_id}")
def update_vocab(vocab_id: str, item: VocabUpdate):
    with _get_db() as conn:
        existing = conn.execute("SELECT id FROM vocabulary WHERE id = ?", (vocab_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Word not found")
        conn.execute(
            "UPDATE vocabulary SET word = ?, translation = ?, language_from = ?, language_to = ? WHERE id = ?",
            (item.word.strip(), (item.translation or "").strip(),
             (item.language_from or "en").strip() or "en",
             (item.language_to or "unknown").strip() or "unknown",
             vocab_id)
        )
    return {"status": "updated"}


@app.post("/api/answer")
def record_answer(payload: AnswerPayload):
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO performance (id, vocab_id, correct, mode, answered_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), payload.vocab_id, int(payload.correct), payload.mode,
             datetime.utcnow().isoformat())
        )

        row = conn.execute("SELECT * FROM stats WHERE vocab_id = ?", (payload.vocab_id,)).fetchone()
        s = dict(row) if row else {
            "correct_count": 0, "wrong_count": 0, "ease_factor": 2.5,
            "interval_days": 1, "due_date": _today(), "last_seen": None,
        }

        ef       = s.get("ease_factor", 2.5)
        interval = s.get("interval_days", 1)
        correct  = s.get("correct_count", 0)
        wrong    = s.get("wrong_count", 0)

        last_seen = s.get("last_seen")
        if last_seen:
            days_elapsed = (date.today() - date.fromisoformat(last_seen[:10])).days
        else:
            days_elapsed = interval

        if payload.correct:
            correct += 1
            ef = max(1.3, ef + 0.1 - 0 * (0.08))  # q=5 simplification
            base = max(interval, days_elapsed)
            new_interval = 1 if base == 1 else (6 if base <= 6 else round(base * ef))
        else:
            wrong += 1
            new_interval = 1
            ef = max(1.3, ef - 0.2)

        conn.execute(
            "INSERT INTO stats (vocab_id, correct_count, wrong_count, ease_factor, interval_days, due_date, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vocab_id) DO UPDATE SET "
            "correct_count = excluded.correct_count, wrong_count = excluded.wrong_count, "
            "ease_factor = excluded.ease_factor, interval_days = excluded.interval_days, "
            "due_date = excluded.due_date, last_seen = excluded.last_seen",
            (payload.vocab_id, correct, wrong, ef, new_interval,
             _due_date(new_interval), datetime.utcnow().isoformat())
        )

    return {"status": "recorded"}


@app.get("/api/stats/overview")
def stats_overview():
    with _get_db() as conn:
        vocab_count = conn.execute("SELECT COUNT(*) FROM vocabulary").fetchone()[0]
        mastered = conn.execute(
            "SELECT COUNT(*) FROM stats WHERE correct_count >= 5 AND ease_factor >= 2.5"
        ).fetchone()[0]
        struggling = conn.execute(
            "SELECT COUNT(*) FROM stats WHERE (correct_count + wrong_count) >= 3 AND wrong_count > correct_count"
        ).fetchone()[0]
    return {"total": vocab_count, "mastered": mastered, "struggling": struggling}


@app.get("/api/quiz/choices")
def get_choices(vocab_id: str, language: Optional[str] = None):
    with _get_db() as conn:
        doc = conn.execute(
            "SELECT translation FROM vocabulary WHERE id = ?", (vocab_id,)
        ).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Word not found")
        correct_trans = doc["translation"]

        if language:
            all_rows = conn.execute(
                "SELECT translation FROM vocabulary WHERE language_to = ? AND id != ? LIMIT 100",
                (language, vocab_id)
            ).fetchall()
        else:
            all_rows = conn.execute(
                "SELECT translation FROM vocabulary WHERE id != ? LIMIT 100",
                (vocab_id,)
            ).fetchall()

    translations = [r["translation"] for r in all_rows if r["translation"] and r["translation"] != correct_trans]
    random.shuffle(translations)
    distractors = list(dict.fromkeys(translations))[:3]

    choices = distractors + [correct_trans]
    random.shuffle(choices)
    return {"choices": choices, "correct": correct_trans}


# ── Static frontend ───────────────────────────────────────────────────────────

static_dir = Path(__file__).parent.parent / "frontend"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

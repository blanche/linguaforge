"""
LinguaForge backend — FastAPI + Google Cloud Firestore
Storage layout (all under collection "users/{user_id}/"):
  vocabulary/{doc_id}  — word, translation, language_from, language_to, source, created_at
  stats/{doc_id}       — correct_count, wrong_count, ease_factor, interval_days, due_date, last_seen
  performance/{auto}   — vocab_id, correct, mode, answered_at

USER_ID is read from the LINGUAFORGE_USER env var (default "default").
For a multi-user setup, replace this with real auth.
"""

from __future__ import annotations

import os
import random
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.cloud import firestore
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

GCP_PROJECT = os.environ.get("GCP_PROJECT")          # auto-detected on Cloud Run
USER_ID     = os.environ.get("LINGUAFORGE_USER", "default")

# ── Firestore client ──────────────────────────────────────────────────────────

db: firestore.Client = None   # initialised in lifespan

def _vocab_col():
    return db.collection("users").document(USER_ID).collection("vocabulary")

def _stats_col():
    return db.collection("users").document(USER_ID).collection("stats")

def _perf_col():
    return db.collection("users").document(USER_ID).collection("performance")

def _today() -> str:
    return date.today().isoformat()

def _due_date(interval_days: int) -> str:
    return (date.today() + timedelta(days=interval_days)).isoformat()

def _doc_to_dict(doc: firestore.DocumentSnapshot) -> dict:
    d = doc.to_dict() or {}
    d["id"] = doc.id
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = firestore.Client(project=GCP_PROJECT)
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
    vocab_id: str          # Firestore document id (string)
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
    existing = (
        _vocab_col()
        .where("word", "==", word)
        .where("language_to", "==", language_to)
        .limit(1)
        .get()
    )
    if existing:
        return existing[0].id, False

    doc_id = str(uuid.uuid4())
    _vocab_col().document(doc_id).set({
        "word": word,
        "translation": translation,
        "language_from": language_from,
        "language_to": language_to,
        "source": source,
        "created_at": datetime.utcnow().isoformat(),
    })
    _stats_col().document(doc_id).set({
        "correct_count": 0,
        "wrong_count": 0,
        "ease_factor": 2.5,
        "interval_days": 1,
        "due_date": _today(),
        "last_seen": None,
    })
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
    q = _vocab_col()
    if language:
        q = q.where("language_to", "==", language)
    vocab = {d.id: _doc_to_dict(d) for d in q.stream()}
    stats = {d.id: d.to_dict() for d in _stats_col().stream()}

    result = []
    for vid, v in vocab.items():
        s = stats.get(vid, {})
        result.append({**v, "correct_count": s.get("correct_count", 0),
                        "wrong_count": s.get("wrong_count", 0),
                        "ease_factor": s.get("ease_factor", 2.5),
                        "due_date": s.get("due_date")})
    result.sort(key=lambda x: x.get("word", "").lower())
    return result


@app.get("/api/vocab/languages")
def list_languages():
    counts: dict[str, int] = {}
    for d in _vocab_col().stream():
        lang = (d.to_dict() or {}).get("language_to", "unknown")
        counts[lang] = counts.get(lang, 0) + 1
    return [{"language_to": k, "count": v} for k, v in sorted(counts.items())]



@app.get("/api/vocab/due")
def get_due_vocab(language: Optional[str] = None, limit: int = 50):
    today = _today()
    q = _vocab_col()
    if language:
        q = q.where("language_to", "==", language)
    vocab = {d.id: _doc_to_dict(d) for d in q.stream()}
    stats = {d.id: d.to_dict() for d in _stats_col().stream()}

    due = []
    for vid, v in vocab.items():
        s = stats.get(vid, {})
        due_date = s.get("due_date") or today
        if due_date <= today:
            due.append({**v, **s, "id": vid})

    due.sort(key=lambda x: x.get("due_date") or today)
    return due[:limit]


@app.get("/api/vocab/weak")
def get_weak_vocab(language: Optional[str] = None, limit: int = 50):
    q = _vocab_col()
    if language:
        q = q.where("language_to", "==", language)
    vocab = {d.id: _doc_to_dict(d) for d in q.stream()}
    stats = {d.id: d.to_dict() for d in _stats_col().stream()}

    weak = []
    for vid, v in vocab.items():
        s = stats.get(vid, {})
        total = (s.get("correct_count") or 0) + (s.get("wrong_count") or 0)
        if total == 0:
            continue
        acc = (s.get("correct_count") or 0) / total
        weak.append({**v, **s, "id": vid, "accuracy": acc})

    weak.sort(key=lambda x: (x["accuracy"], -(x.get("wrong_count") or 0)))
    return weak[:limit]


@app.get("/api/vocab/strong")
def get_strong_vocab(language: Optional[str] = None, limit: int = 50):
    q = _vocab_col()
    if language:
        q = q.where("language_to", "==", language)
    vocab = {d.id: _doc_to_dict(d) for d in q.stream()}
    stats = {d.id: d.to_dict() for d in _stats_col().stream()}

    strong = []
    for vid, v in vocab.items():
        s = stats.get(vid, {})
        total = (s.get("correct_count") or 0) + (s.get("wrong_count") or 0)
        if total < 3:
            continue
        acc = (s.get("correct_count") or 0) / total
        strong.append({**v, **s, "id": vid, "accuracy": acc})

    strong.sort(key=lambda x: (-x["accuracy"], -(x.get("correct_count") or 0)))
    return strong[:limit]


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
    _vocab_col().document(vocab_id).delete()
    _stats_col().document(vocab_id).delete()
    for d in _perf_col().where("vocab_id", "==", vocab_id).stream():
        d.reference.delete()
    return {"status": "deleted"}


@app.delete("/api/vocab")
def clear_all_vocab():
    deleted = 0
    for d in _vocab_col().stream():
        d.reference.delete()
        deleted += 1
    for d in _stats_col().stream():
        d.reference.delete()
    for d in _perf_col().stream():
        d.reference.delete()
    return {"status": "cleared", "deleted": deleted}


@app.patch("/api/vocab/{vocab_id}")
def update_vocab(vocab_id: str, item: VocabUpdate):
    ref = _vocab_col().document(vocab_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Word not found")
    ref.update({
        "word": item.word.strip(),
        "translation": (item.translation or "").strip(),
        "language_from": (item.language_from or "en").strip() or "en",
        "language_to": (item.language_to or "unknown").strip() or "unknown",
    })
    return {"status": "updated"}


@app.post("/api/answer")
def record_answer(payload: AnswerPayload):
    _perf_col().add({
        "vocab_id": payload.vocab_id,
        "correct": payload.correct,
        "mode": payload.mode,
        "answered_at": datetime.utcnow().isoformat(),
    })

    stats_ref = _stats_col().document(payload.vocab_id)
    s = (stats_ref.get().to_dict() or {})

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

    stats_ref.set({
        "correct_count": correct,
        "wrong_count": wrong,
        "ease_factor": ef,
        "interval_days": new_interval,
        "due_date": _due_date(new_interval),
        "last_seen": datetime.utcnow().isoformat(),
    }, merge=True)

    return {"status": "recorded"}


@app.get("/api/stats/overview")
def stats_overview():
    vocab_count = sum(1 for _ in _vocab_col().stream())

    mastered = struggling = 0
    for d in _stats_col().stream():
        s = d.to_dict()
        if (s.get("correct_count") or 0) >= 5 and (s.get("ease_factor") or 0) >= 2.5:
            mastered += 1
        total = (s.get("correct_count") or 0) + (s.get("wrong_count") or 0)
        if total >= 3 and (s.get("wrong_count") or 0) > (s.get("correct_count") or 0):
            struggling += 1

    return {
        "total": vocab_count, "mastered": mastered, "struggling": struggling,
    }


@app.get("/api/quiz/choices")
def get_choices(vocab_id: str, language: Optional[str] = None):
    doc = _vocab_col().document(vocab_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Word not found")
    correct_trans = (doc.to_dict() or {}).get("translation", "")

    q = _vocab_col()
    if language:
        q = q.where("language_to", "==", language)
    all_docs = list(q.limit(100).stream())
    random.shuffle(all_docs)

    distractors: list[str] = []
    for d in all_docs:
        t = (d.to_dict() or {}).get("translation", "")
        if t and t != correct_trans and t not in distractors:
            distractors.append(t)
        if len(distractors) == 3:
            break

    choices = distractors + [correct_trans]
    random.shuffle(choices)
    return {"choices": choices, "correct": correct_trans}


# ── Static frontend ───────────────────────────────────────────────────────────

static_dir = Path(__file__).parent.parent / "frontend"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

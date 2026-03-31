# LinguaForge — Vocabulary Trainer

A vocabulary trainer that reads from your Duolingo account. Runs locally via Docker.

## Project Structure

```
linguaforge/
├── backend/
│   ├── main.py          # FastAPI backend (Duolingo API proxy + SQLite persistence)
│   └── requirements.txt
├── frontend/
│   └── index.html       # Single-page frontend (no build step needed)
├── Dockerfile
├── docker-compose.yml
└── .dockerignore
```

---

## Running locally

```bash
docker compose up -d --build
# Open http://localhost:8080
```

Data is persisted in a Docker volume (`linguaforge_data`) across restarts.

---

## Getting your Duolingo JWT

1. Open [duolingo.com](https://www.duolingo.com) and log in
2. Open DevTools (F12) → Console tab
3. Paste and run:
   ```javascript
   document.cookie.match(new RegExp('(^| )jwt_token=([^;]+)'))[0].slice(11)
   ```
4. Copy the returned string and paste it into the app

> **Security note:** Your JWT is only used to fetch vocabulary from Duolingo and is never written to disk.

---

## Features

| Feature | Description |
|---|---|
| **Vocabulary fetch** | Loads all words from your Duolingo account |
| **Flashcard trainer** | Flip cards with 3D animation |
| **Multiple choice** | Pick the correct translation |
| **Type it** | Type the translation from memory |
| **Spaced repetition** | Due-date scheduling based on correct/wrong answers |
| **Infinite sessions** | Trains through all due/weak words until exhausted or stopped |
| **Progress tracking** | Per-word correct/wrong counts stored in SQLite |
| **Search & filter** | Filter by weak/strong/due, search by word |
| **Multi-language** | Automatically lists all your active Duolingo courses |

---

## Updating

```bash
docker compose up -d --build
```

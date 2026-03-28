# Duo Trainer — Deployment Guide

A vocabulary trainer app that reads from your Duolingo account, built to run on **Google Cloud Run**.

## 🏗️ Project Structure

```
duolingo-trainer/
├── app.py              # Flask backend (Duolingo API proxy + translation)
├── static/
│   └── index.html      # Single-page frontend (no build step needed)
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

---

## 🚀 Deploy to Google Cloud Run

### Prerequisites
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) installed and authenticated
- A GCP project with billing enabled
- Cloud Run API enabled

### Step 1 — Set your project

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
```

### Step 2 — Build and push the container

```bash
cd duolingo-trainer

# Build using Cloud Build (no local Docker needed)
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/duo-trainer

# OR build locally and push
docker build -t gcr.io/YOUR_PROJECT_ID/duo-trainer .
docker push gcr.io/YOUR_PROJECT_ID/duo-trainer
```

### Step 3 — Deploy to Cloud Run

```bash
gcloud run deploy duo-trainer \
  --image gcr.io/YOUR_PROJECT_ID/duo-trainer \
  --platform managed \
  --region europe-west1 \
  --allow-unauthenticated \
  --port 8080 \
  --memory 256Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3
```

### Step 4 — Open the app

After deployment you'll get a URL like:
```
https://duo-trainer-xxxx-ew.a.run.app
```

Open it in your browser and follow the JWT instructions on screen.

---

## 🔑 Getting your Duolingo JWT

1. Open [duolingo.com](https://www.duolingo.com) and log in
2. Open DevTools (F12) → Console tab
3. Paste and run:
   ```javascript
   document.cookie.match(new RegExp('(^| )jwt_token=([^;]+)'))[0].slice(11)
   ```
4. Copy the returned string and paste it into the app

> **Security note:** Your JWT token grants access to your Duolingo account. It is sent only to this app's backend (which proxies requests to Duolingo) and is never stored server-side. Keep it private. The token does not expire unless you change your Duolingo password.

---

## 🛠️ Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python app.py
# Open http://localhost:8080
```

---

## 🧠 Features

| Feature | Description |
|---|---|
| **Vocabulary fetch** | Loads all words from your Duolingo account with strength scores |
| **Flashcard trainer** | Flip cards with animated 3D effect, auto-fetches translations |
| **Weak-first mode** | Prioritizes words you've gotten wrong or never practiced |
| **Progress tracking** | Stores correct/wrong counts in browser localStorage per language |
| **Distribution chart** | Visual histogram of your vocabulary strength |
| **Search & filter** | Filter by weak/strong, search by word or skill |
| **Session stats** | Score summary after each training session |
| **Multi-language** | Automatically lists all your active Duolingo courses |

---

## ⚙️ Environment Variables

None required. The app runs on `PORT` (default: 8080), which Cloud Run sets automatically.

---

## 💰 Cost

On Cloud Run with `--min-instances 0`, the app scales to zero when idle.
Expected cost for personal use: **< $1/month** (likely within free tier).

---

## 📦 Updating the app

```bash
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/duo-trainer
gcloud run deploy duo-trainer --image gcr.io/YOUR_PROJECT_ID/duo-trainer
```

#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# LinguaForge — Deploy to Google Cloud Run (Firestore backend)
# Usage: ./deploy.sh [PROJECT_ID] [REGION]
# Example: ./deploy.sh my-gcp-project europe-west1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project)}"
REGION="${2:-us-central1}"
SERVICE_NAME="linguaforge"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "🚀 LinguaForge — Deploy to Cloud Run"
echo "   Project : ${PROJECT_ID}"
echo "   Region  : ${REGION}"
echo "   Image   : ${IMAGE}"
echo ""

# 1. Enable required APIs
echo "🔧 Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  --project "${PROJECT_ID}"

# 2. Create Firestore database (Native mode) if it doesn't exist yet
echo "🗄️  Ensuring Firestore Native database exists..."
gcloud firestore databases create \
  --location="${REGION}" \
  --project "${PROJECT_ID}" \
  2>/dev/null || echo "   (Firestore database already exists — skipping)"

# 3. Build and push image via Cloud Build
echo ""
echo "📦 Building Docker image with Cloud Build..."
gcloud builds submit \
  --tag "${IMAGE}" \
  --project "${PROJECT_ID}" \
  .

# 4. Deploy to Cloud Run
echo ""
echo "☁️  Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},LINGUAFORGE_USER=default" \
  --project "${PROJECT_ID}"

echo ""
echo "✅ Deployment complete!"
echo ""
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "🌐 Your app is live at: ${SERVICE_URL}"
echo ""
echo "📋 Notes:"
echo "   • Firestore is in the GCP free tier: 1 GiB storage, 50K reads/day, 20K writes/day"
echo "   • Data persists across container restarts (unlike SQLite on Cloud Run)"
echo "   • LINGUAFORGE_USER=default means all data is in one user namespace."
echo "     Change this env var to support multiple users."

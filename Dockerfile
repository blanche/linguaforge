FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

EXPOSE 8080

# On Cloud Run, Application Default Credentials are available automatically.
# For local dev, set GOOGLE_APPLICATION_CREDENTIALS to a service account key file.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

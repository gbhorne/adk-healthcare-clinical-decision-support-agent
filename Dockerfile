# HC-CDSS Dockerfile
# Packages the ADK web server and all 6 pipeline agents for Cloud Run deployment.
#
# BUILD:
#   docker build -t hc-cdss .
#
# RUN LOCALLY:
#   docker run -p 8000:8000 \
#     -e GCP_PROJECT_ID=your-project-id \
#     -e GOOGLE_APPLICATION_CREDENTIALS=/app/sa-key.json \
#     -v /path/to/sa-key.json:/app/sa-key.json \
#     hc-cdss
#
# CLOUD RUN DEPLOYMENT:
#   gcloud run deploy hc-cdss \
#     --image gcr.io/YOUR_PROJECT_ID/hc-cdss \
#     --platform managed \
#     --region us-central1 \
#     --service-account cdss-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
#     --set-env-vars GCP_PROJECT_ID=YOUR_PROJECT_ID \
#     --no-allow-unauthenticated \
#     --port 8000
#
# NOTE: For Cloud Run, remove GOOGLE_APPLICATION_CREDENTIALS entirely.
#       The service account is attached at the Cloud Run service level using
#       --service-account. ADC resolves credentials automatically via the
#       metadata server. Run scripts/setup_secret_manager.py to migrate
#       remaining secrets before deploying.

FROM python:3.14-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY cdss_agent/ ./cdss_agent/
COPY agents/ ./agents/
COPY shared/ ./shared/
COPY data/ ./data/

# Copy .env.example as a reference — real env vars injected at runtime
COPY .env.example .env.example

# Expose ADK web server port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ADK requires GOOGLE_GENAI_USE_VERTEXAI for Vertex AI routing
ENV GOOGLE_GENAI_USE_VERTEXAI=true

# Run ADK web server
# ADK web binds to 0.0.0.0:8000 by default in containerized environments
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8000"]

# VIGIL — single CPU image. Spark/Kafka/Mongo are OPTIONAL: without them VIGIL runs its
# documented local fallbacks (pandas engine, replay bus, mongita) and says so in the UI.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 VIGIL_API_HOST=0.0.0.0 VIGIL_API_PORT=8000
WORKDIR /app

# JDK is only needed for the Spark engine; keep the image small but functional.
RUN apt-get update && apt-get install -y --no-install-recommends \
      default-jdk-headless build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1
CMD ["uvicorn", "vigil.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

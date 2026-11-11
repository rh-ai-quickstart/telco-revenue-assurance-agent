# Build context: repo root (telco-revenue-assurance-agent/)
# Both backend/ and agent/ must be in scope.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/   /app/agent/
COPY backend/ /app/backend/

# OpenShift: random UID in group 0 — g=u propagates permissions.
RUN chown -R 1001:0 /app && chmod -R g=u /app

USER 1001

EXPOSE 8000

ENTRYPOINT ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

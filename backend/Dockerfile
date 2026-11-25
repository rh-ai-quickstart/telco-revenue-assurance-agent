# Build context: repo root (telco-revenue-assurance-agent/)
# Both backend/ and agent/ must be in scope.
FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

COPY backend/requirements.txt /opt/app-root/src/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/   /opt/app-root/src/agent/
COPY backend/ /opt/app-root/src/backend/

EXPOSE 8000

ENTRYPOINT ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

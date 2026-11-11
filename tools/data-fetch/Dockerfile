FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py db.py models.py queries.py main.py /app/data_fetch_tool/

# OpenShift runs containers with a random UID in group 0 (root group).
# g=u propagates user permissions to group so the process can write temp files.
RUN mkdir -p /app/data_fetch_tool && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app

USER 1001

EXPOSE 8080

ENTRYPOINT ["uvicorn", "data_fetch_tool.main:app", "--host", "0.0.0.0", "--port", "8080"]

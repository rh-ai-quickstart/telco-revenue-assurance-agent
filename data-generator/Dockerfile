FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py generate.py /app/data_generator/

# OpenShift runs containers with a random UID in group 0 (root group).
# g=u propagates user permissions to group so the process can write temp files.
RUN mkdir -p /app/data_generator && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app

ENV HF_HOME=/tmp/huggingface \
    HF_DATASETS_CACHE=/tmp/huggingface/datasets

USER 1001

ENTRYPOINT ["python", "-m", "data_generator.generate"]

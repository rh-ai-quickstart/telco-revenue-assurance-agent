FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    rm requirements.txt

USER 1001

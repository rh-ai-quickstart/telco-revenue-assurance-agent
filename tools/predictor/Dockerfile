FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --progress-bar off -r /app/requirements.txt

COPY *.py /app/predictor/

# OpenShift may assign any UID in group 0. Source stays readable and writable
# temporary files go to /tmp; no database driver or credential is baked in.
RUN chown -R 1001:0 /app && chmod -R g=u /app

USER 1001:0
EXPOSE 8080

CMD ["uvicorn", "predictor.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

FROM registry.access.redhat.com/ubi9/python-312:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/app-root/src

COPY requirements.txt /opt/app-root/src/requirements.txt
RUN pip install --no-cache-dir --progress-bar off -r /opt/app-root/src/requirements.txt

COPY *.py /opt/app-root/src/predictor/

EXPOSE 8080

CMD ["uvicorn", "predictor.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

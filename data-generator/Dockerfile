FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

USER 0
RUN dnf install -y --nodocs postgresql-devel gcc && dnf clean all
USER 1001

COPY requirements.txt /opt/app-root/src/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py generate.py /opt/app-root/src/data_generator/

ENV HF_HOME=/tmp/huggingface \
    HF_DATASETS_CACHE=/tmp/huggingface/datasets

ENTRYPOINT ["python", "-m", "data_generator.generate"]

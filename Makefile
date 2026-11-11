.PHONY: build deploy undeploy test lint \
        venv upload-datasets \
        run-pipelines run-pipeline-churn run-pipeline-fraud run-pipeline-qoe

# ── Python virtual environment ─────────────────────────────────────────────────
VENV   := .venv
PYTHON := $(VENV)/bin/python3
PIP    := $(VENV)/bin/pip3

venv: $(VENV)

$(VENV):
	python3.12 -m venv $(VENV)

# ── Credentials and namespace — override at the command line ───────────────────
# Example: make deploy PGVECTOR_PASSWORD=secret MINIO_PASSWORD=minio123
NAMESPACE         ?= telco-revenue-assurance-agent
PGVECTOR_USER     ?= postgres
PGVECTOR_PASSWORD ?= change-me
PGVECTOR_DBNAME   ?= postgres
MINIO_HOST        ?= minio-service.showcase.svc
MINIO_PORT        ?= 9000
MINIO_SCHEME      ?= http
MINIO_BUCKET      ?= mybucket
MINIO_USER        ?= minio
MINIO_PASSWORD    ?= minio123

# ── KFP pipeline variables ─────────────────────────────────────────────────────
# KFP_ENDPOINT is auto-detected from the cluster route.
# BEARER_TOKEN is auto-detected from the current oc login session.
# Override either if needed: make run-pipelines KFP_ENDPOINT=https://...
KFP_ENDPOINT  ?= $(shell oc get route ds-pipeline-dspa -n $(NAMESPACE) -o jsonpath='https://{.spec.host}' 2>/dev/null)
BEARER_TOKEN  ?= $(shell oc whoami -t 2>/dev/null)

# External MinIO route — used by upload-datasets (runs locally, can't reach internal svc).
# Auto-detected from the cluster; override if your route differs.
MINIO_EXTERNAL_ENDPOINT ?= $(shell oc get route minio-api -n showcase -o jsonpath='https://{.spec.host}' 2>/dev/null)

# Common flags forwarded to deploy/Makefile for every target
DEPLOY_FLAGS = \
  NAMESPACE=$(NAMESPACE) \
  PGVECTOR_USER=$(PGVECTOR_USER) \
  PGVECTOR_PASSWORD=$(PGVECTOR_PASSWORD) \
  PGVECTOR_DBNAME=$(PGVECTOR_DBNAME) \
  MINIO_HOST=$(MINIO_HOST) \
  MINIO_PORT=$(MINIO_PORT) \
  MINIO_SCHEME=$(MINIO_SCHEME) \
  MINIO_BUCKET=$(MINIO_BUCKET) \
  MINIO_USER=$(MINIO_USER) \
  MINIO_PASSWORD=$(MINIO_PASSWORD)

build:
	$(MAKE) -C deploy build-all NAMESPACE=$(NAMESPACE)

deploy:
	$(MAKE) -C deploy deploy-all $(DEPLOY_FLAGS)

undeploy:
	$(MAKE) -C deploy undeploy-all NAMESPACE=$(NAMESPACE)

test:
	pytest

lint:
	cd frontend && npm run lint

# ── Dataset upload ─────────────────────────────────────────────────────────────
# Download datasets from Hugging Face and upload to the S3 bucket.
# Credentials are read directly from the my-data-connection secret in the namespace.
# Requires: pip install datasets boto3 pandas
upload-datasets: $(VENV)
	$(PIP) install -q datasets boto3 pandas
	AWS_S3_ENDPOINT=$(MINIO_EXTERNAL_ENDPOINT) \
	AWS_S3_BUCKET=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_S3_BUCKET}' 2>/dev/null | base64 -d) \
	AWS_ACCESS_KEY_ID=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' 2>/dev/null | base64 -d) \
	AWS_SECRET_ACCESS_KEY=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' 2>/dev/null | base64 -d) \
	  $(PYTHON) demo/scripts/upload_datasets.py

# ── KFP pipeline run targets ───────────────────────────────────────────────────
# Prerequisites: training datasets must exist in s3://$(MINIO_BUCKET)/data/
# Requires: pip install kfp kfp-kubernetes python-dotenv

run-pipeline-churn: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	  $(PYTHON) demo/pipelines/03-telco-churn-kfp-pipeline.py

run-pipeline-fraud: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	  $(PYTHON) demo/pipelines/01-telco-revass-randomforest-pipeline.py

run-pipeline-qoe: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	  $(PYTHON) demo/pipelines/starlink_qoe_pipeline.py

run-pipelines: run-pipeline-churn run-pipeline-fraud run-pipeline-qoe

# ── Individual component targets ───────────────────────────────────────────────
# Forward any deploy-* or undeploy-* or build-* target to deploy/Makefile.
# Examples:
#   make deploy-pipelines
#   make deploy-pgvector PGVECTOR_PASSWORD=secret
#   make undeploy-frontend
#   make build-pipeline-base
deploy-%:
	$(MAKE) -C deploy deploy-$* $(DEPLOY_FLAGS)

undeploy-%:
	$(MAKE) -C deploy undeploy-$* NAMESPACE=$(NAMESPACE)

build-%:
	$(MAKE) -C deploy build-$* NAMESPACE=$(NAMESPACE)

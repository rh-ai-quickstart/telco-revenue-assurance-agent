.SUFFIXES:

.PHONY: build deploy deploy-all deploy-infra deploy-storage deploy-app maybe-deploy-llm \
        undeploy undeploy-all undeploy-infra undeploy-app test lint \
        venv upload-datasets verify-run-pipelines \
        run-pipelines run-pipeline-churn run-pipeline-fraud run-pipeline-qoe \
        version build-all-images push-all-images helm-install-test

# ── Python virtual environment ─────────────────────────────────────────────────
VENV   := .venv
PYTHON := $(VENV)/bin/python3
PIP    := $(VENV)/bin/pip3

venv: $(VENV)

$(VENV):
	python3.12 -m venv $(VENV)

# ── Credentials and namespace — override at the command line ───────────────────
# Example: make deploy PGVECTOR_PASSWORD=secret S3_SECRET_KEY=s4secret
NAMESPACE         ?= telco-revenue-assurance-agent
PGVECTOR_USER     ?= postgres
PGVECTOR_PASSWORD ?= change-me
PGVECTOR_DBNAME   ?= postgres
PGVECTOR_IMAGE    ?= $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pgvector:$(IMAGE_TAG)
HF_TOKEN          ?=

# S3 / aws-compatible-storage connection details.
# S4_NAMESPACE defaults to $(NAMESPACE) so the full stack shares one project.
# Override to deploy S4 in a separate namespace:
#   make deploy S4_NAMESPACE=shared-storage
S4_NAMESPACE      ?= $(NAMESPACE)
STORAGE_CLASS ?=
S3_HOST           ?= aws-compatible-storage.$(S4_NAMESPACE).svc
S3_PORT           ?= 7480
S3_SCHEME         ?= http
S3_BUCKET         ?= mybucket
S3_ACCESS_KEY     ?= s4admin
S3_SECRET_KEY     ?= s4secret

# ── Container image registry ───────────────────────────────────────────────────
IMAGE_REGISTRY ?= quay.io/rh-ai-quickstart
override IMAGE_PREFIX := telco-revenue-assurance-agent

# ── LLM service ────────────────────────────────────────────────────────────────
# LLM_URL: vLLM-compatible base URL.  LLM_API_TOKEN: bearer token (optional).
# LLM_MODEL_NAME: model served at LLM_URL (default: Llama-3.1-8B-Instruct).
# Example: make deploy-ogx-ai LLM_URL=http://my-vllm.svc:80/v1 LLM_MODEL_NAME=mistralai/Mistral-7B-v0.3
LLM_URL        ?=
LLM_API_TOKEN  ?=
LLM_MODEL_NAME ?= meta-llama/Llama-3.1-8B-Instruct
LLM_MODEL_KEY  = $(shell echo "$(LLM_MODEL_NAME)" | sed 's|.*/||' | tr '[:upper:]' '[:lower:]' | sed 's/[._]/-/g')
OGX_MODEL_ID   ?= $(LLM_MODEL_KEY)/$(LLM_MODEL_NAME)

# ── KFP pipeline variables ─────────────────────────────────────────────────────
# KFP_ENDPOINT is auto-detected from the cluster route.
# BEARER_TOKEN is auto-detected from the current oc login session.
# Override either if needed: make run-pipelines KFP_ENDPOINT=https://...
KFP_ENDPOINT  ?= $(shell oc get route ds-pipeline-dspa -n $(NAMESPACE) -o jsonpath='https://{.spec.host}' 2>/dev/null)
BEARER_TOKEN  ?= $(shell oc whoami -t 2>/dev/null)

# External S3 route — used by upload-datasets (runs locally, can't reach internal svc).
# Auto-detected from the cluster; override if your route differs.
S3_EXTERNAL_ENDPOINT ?= $(shell oc get route aws-compatible-storage-api -n $(S4_NAMESPACE) -o jsonpath='https://{.spec.host}' 2>/dev/null)

# Common flags forwarded to deploy/Makefile for every target
DEPLOY_FLAGS = \
  NAMESPACE=$(NAMESPACE) \
  PGVECTOR_USER=$(PGVECTOR_USER) \
  PGVECTOR_PASSWORD=$(PGVECTOR_PASSWORD) \
  PGVECTOR_DBNAME=$(PGVECTOR_DBNAME) \
  PGVECTOR_IMAGE=$(PGVECTOR_IMAGE) \
  S4_NAMESPACE=$(S4_NAMESPACE) \
  STORAGE_CLASS=$(STORAGE_CLASS) \
  S3_HOST=$(S3_HOST) \
  S3_PORT=$(S3_PORT) \
  S3_SCHEME=$(S3_SCHEME) \
  S3_BUCKET=$(S3_BUCKET) \
  S3_ACCESS_KEY=$(S3_ACCESS_KEY) \
  S3_SECRET_KEY=$(S3_SECRET_KEY) \
  IMAGE_REGISTRY=$(IMAGE_REGISTRY) \
  IMAGE_TAG=$(IMAGE_TAG) \
  IMAGE_PREFIX=$(IMAGE_PREFIX) \
  LLM_URL=$(LLM_URL) \
  LLM_API_TOKEN=$(LLM_API_TOKEN) \
  LLM_MODEL_NAME=$(LLM_MODEL_NAME) \
  OGX_MODEL_ID=$(OGX_MODEL_ID) \
  HF_TOKEN=$(HF_TOKEN)

# ── Image version ──────────────────────────────────────────────────────────────
VERSION            ?= 0.1.0
CURRENT_GIT_BRANCH  = $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null)
IMAGE_TAG          ?= $(if $(filter main,$(CURRENT_GIT_BRANCH)),$(VERSION),$(VERSION)-dev)

version:
	@echo "$(VERSION)"

# ── CI image build/push targets (used by build-and-push workflow) ──────────────
# Builds all application images with docker. Distinct from the OpenShift
# build target which requires a cluster (oc start-build).
build-all-images:
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-backend:$(VERSION)         -f backend/Dockerfile .
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-data-fetch:$(VERSION)      tools/data-fetch/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-data-generator:$(VERSION)  data-generator/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-frontend:$(VERSION)        frontend/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pipeline-base:$(VERSION)   demo/pipeline-base/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-predictor:$(VERSION)       tools/predictor/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-scoring:$(VERSION)         scoring/
	docker build -t $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pgvector:$(VERSION)        deploy/build/pgvector-seeded/

push-all-images:
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-backend:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-data-fetch:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-data-generator:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-frontend:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pipeline-base:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-predictor:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-scoring:$(VERSION)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pgvector:$(VERSION)

# ── CI Helm install (used by Kind E2E / nightly) ──────────────────────────────
# Installs application Helm charts using IMAGE_REGISTRY and IMAGE_TAG.
# Example: make helm-install-test IMAGE_REGISTRY=localhost:5001 IMAGE_TAG=0.1.0 NAMESPACE=test
helm-install-test:
	$(MAKE) -C deploy deploy-app $(DEPLOY_FLAGS)

# ── OpenShift in-cluster build (requires oc login + BuildConfig) ───────────────
build:
	$(MAKE) -C deploy build-all NAMESPACE=$(NAMESPACE)

# Phase 1a: storage layer only (S4 + pgvector + data-gen). Verified before proceeding.
deploy-storage:
	@test -n "$(STORAGE_CLASS)" || (echo "Error: STORAGE_CLASS is required. Example: make deploy-all STORAGE_CLASS=gp3-csi" && exit 1)
	$(MAKE) -C deploy deploy-storage $(DEPLOY_FLAGS)

# Phase 1: storage → upload-datasets → DSPA. Full infra sequence.
deploy-infra:
	$(MAKE) deploy-storage
	$(MAKE) upload-datasets
	$(MAKE) -C deploy deploy-pipelines verify-pipelines $(DEPLOY_FLAGS)

# Phase 3: application components. Run after deploy-infra + run-pipelines.
deploy-app:
	$(MAKE) -C deploy deploy-app $(DEPLOY_FLAGS)

# Conditionally deploys vLLM in-cluster when LLM_URL is not set (local LLM mode).
# When LLM_URL is provided, this is a no-op (remote LLM mode).
# Waits up to 10 min for the InferenceService to be Ready before returning,
# so the OGX-AI wait-for-models init container starts with vLLM already serving.
maybe-deploy-llm:
	$(if $(LLM_URL),,$(MAKE) -C deploy deploy-llm-service $(DEPLOY_FLAGS))
	$(if $(LLM_URL),,oc wait inferenceservice/$(LLM_MODEL_KEY) -n $(NAMESPACE) --for=condition=Ready=True --timeout=600s)

# Full deploy: infra → pipelines → verify pipelines → (optional local LLM) → app.
# Local LLM:  omit LLM_URL, set HF_TOKEN=<token>
# Remote LLM: set LLM_URL=<url>, LLM_API_TOKEN=<token> (optional)
deploy-all:
	$(MAKE) deploy-infra
	$(MAKE) run-pipelines
	$(MAKE) verify-run-pipelines
	$(MAKE) maybe-deploy-llm
	$(MAKE) deploy-app

deploy: deploy-all

# Full teardown: all Helm releases + oc resources + PVCs.
# Does NOT delete the namespace — run `make undeploy-namespace` explicitly.
undeploy-all:
	$(MAKE) -C deploy undeploy-all NAMESPACE=$(NAMESPACE)
	$(MAKE) -C deploy undeploy-llm-service NAMESPACE=$(NAMESPACE)
	oc delete pvc --all -n $(NAMESPACE) || true

undeploy: undeploy-all

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
	AWS_S3_ENDPOINT=$(S3_EXTERNAL_ENDPOINT) \
	AWS_S3_BUCKET=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_S3_BUCKET}' 2>/dev/null | base64 -d) \
	AWS_ACCESS_KEY_ID=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' 2>/dev/null | base64 -d) \
	AWS_SECRET_ACCESS_KEY=$(shell oc get secret my-data-connection -n $(NAMESPACE) \
	  -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' 2>/dev/null | base64 -d) \
	  $(PYTHON) demo/scripts/upload_datasets.py

# ── KFP pipeline run targets ───────────────────────────────────────────────────
# Prerequisites: training datasets must exist in s3://$(S3_BUCKET)/data/
# Requires: pip install kfp kfp-kubernetes python-dotenv

run-pipeline-churn: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	S3_BUCKET=$(S3_BUCKET) \
	$(if $(IMAGE_REGISTRY),BASE_IMAGE=$(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pipeline-base:$(IMAGE_TAG)) \
	  $(PYTHON) demo/pipelines/03-telco-churn-kfp-pipeline.py

run-pipeline-fraud: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	S3_BUCKET=$(S3_BUCKET) \
	$(if $(IMAGE_REGISTRY),BASE_IMAGE=$(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pipeline-base:$(IMAGE_TAG)) \
	  $(PYTHON) demo/pipelines/01-telco-revass-randomforest-pipeline.py

run-pipeline-qoe: $(VENV)
	$(PIP) install -q kfp kfp-kubernetes python-dotenv
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	S3_SCHEME=$(S3_SCHEME) S3_HOST=$(S3_HOST) S3_PORT=$(S3_PORT) S3_BUCKET=$(S3_BUCKET) \
	$(if $(IMAGE_REGISTRY),BASE_IMAGE=$(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pipeline-base:$(IMAGE_TAG)) \
	  $(PYTHON) demo/pipelines/starlink_qoe_pipeline.py

run-pipelines: run-pipeline-churn run-pipeline-fraud run-pipeline-qoe

# Poll KFP until all three experiments (churn, revass, starlink-qoe) have a SUCCEEDED run.
# Usable standalone: make verify-run-pipelines
verify-run-pipelines: $(VENV)
	$(PIP) install -q kfp
	KUBEFLOW_ENDPOINT=$(KFP_ENDPOINT) BEARER_TOKEN=$(BEARER_TOKEN) \
	  $(PYTHON) demo/scripts/verify_pipelines.py

# ── Top-level undeploy targets ─────────────────────────────────────────────────
undeploy-infra:
	$(MAKE) -C deploy undeploy-infra NAMESPACE=$(NAMESPACE)

undeploy-app:
	$(MAKE) -C deploy undeploy-app NAMESPACE=$(NAMESPACE)

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

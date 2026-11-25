# Clear all built-in implicit rules (C/C++ compilation etc.) — this Makefile
# only orchestrates Helm and oc commands, never compiles source files.
.SUFFIXES:

# ── Target namespace ───────────────────────────────────────────────────────────
NAMESPACE ?= telco-revenue-assurance-agent

# ── PostgreSQL credentials ─────────────────────────────────────────────────────
PGVECTOR_USER     ?= postgres
PGVECTOR_PASSWORD ?= change-me
PGVECTOR_DBNAME   ?= postgres
DB_SECRET         ?= database-secret
# PGVECTOR_IMAGE: pre-seeded PostgreSQL image (data already generated).
# When set, deploy-pgvector uses this image and deploy-all skips deploy-data-gen.
# Build with: make build-pgvector-seeded (see docs)
# Example: make deploy PGVECTOR_IMAGE=quay.io/rh-ai-quickstart/telco-revenue-assurance-agent-pgvector:latest
PGVECTOR_IMAGE    ?= $(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-pgvector:$(IMAGE_TAG)
# Split repo and tag so the chart doesn't double-append the tag.
# Works for quay.io/org/name:tag and quay.io/org/name (defaults tag to latest).
_pgvector_repo     = $(firstword $(subst :, ,$(PGVECTOR_IMAGE)))
_pgvector_tag      = $(or $(word 2,$(subst :, ,$(PGVECTOR_IMAGE))),latest)

# ── S3 / aws-compatible-storage (used by DSPA and pipeline data connections) ───
# S4_NAMESPACE: namespace where aws-compatible-storage is deployed.
# Defaults to $(NAMESPACE) so the full stack shares one project.
# Override with a different namespace if running a shared S4 instance:
#   make deploy-aws-compatible-storage S4_NAMESPACE=shared-storage
S4_NAMESPACE      ?= $(NAMESPACE)
STORAGE_CLASS ?=

# S3 connection details consumed by DSPA, secrets, and predictor.
# S3_HOST defaults to the in-cluster service name in S4_NAMESPACE.
# Override with an external route if needed:
#   make deploy-pipelines S3_HOST=aws-compatible-storage-api-<ns>.apps.<cluster> S3_SCHEME=https S3_PORT=443
S3_HOST           ?= aws-compatible-storage.$(S4_NAMESPACE).svc
S3_PORT           ?= 7480
S3_SCHEME         ?= http
S3_BUCKET         ?= mybucket
S3_ACCESS_KEY     ?= s4admin
S3_SECRET_KEY     ?= s4secret

# ── Container image registry ───────────────────────────────────────────────────
IMAGE_REGISTRY     ?= quay.io/rh-ai-quickstart
CURRENT_GIT_BRANCH  = $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null)
IMAGE_TAG          ?= $(if $(filter main,$(CURRENT_GIT_BRANCH)),$(VERSION),$(VERSION)-dev)
override IMAGE_PREFIX := telco-revenue-assurance-agent

# Compute full image repository for a given suffix (empty when IMAGE_REGISTRY unset)
img = $(if $(IMAGE_REGISTRY),$(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-$(1))

# Default scoring image: external registry when set, internal otherwise.
SCORING_IMAGE ?= $(if $(IMAGE_REGISTRY),$(IMAGE_REGISTRY)/$(IMAGE_PREFIX)-scoring:$(IMAGE_TAG),image-registry.openshift-image-registry.svc:5000/$(NAMESPACE)/scoring:latest)

# ── LLM service ────────────────────────────────────────────────────────────────
# LLM_URL: vLLM-compatible base URL for OGX-AI.  Can be in-cluster or external.
#   make deploy-ogx-ai LLM_URL=http://llama-3-1-8b-instruct-vllm.showcase.svc:80/v1
# LLM_API_TOKEN: bearer token for endpoints that require authentication (optional).
# LLM_MODEL_NAME: model identifier served at LLM_URL (HuggingFace-style, e.g. meta-llama/Llama-3.1-8B-Instruct).
#   The model key is derived automatically as LLM_MODEL_KEY.
#   Override: make deploy-backend LLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.3
LLM_NAMESPACE  ?= $(NAMESPACE)
HF_TOKEN       ?=
LLM_URL        ?=
LLM_API_TOKEN  ?=
LLM_MODEL_NAME ?= meta-llama/Llama-3.1-8B-Instruct
# LLM_MODEL_KEY: Helm values key derived from LLM_MODEL_NAME (strip namespace prefix,
# lowercase, dots/underscores → dashes).  Matches the named entry in ogx-ai values.yaml
# (e.g. llama-3-1-8b-instruct).  Used to enable the model and set its URL in OGX-AI.
LLM_MODEL_KEY  = $(shell echo "$(LLM_MODEL_NAME)" | sed 's|.*/||' | tr '[:upper:]' '[:lower:]' | sed 's/[._]/-/g')
# _ogx_llm_url: vLLM base URL passed to OGX-AI.  When LLM_URL is set (remote LLM),
# use it directly.  Otherwise fall back to the in-cluster llm-service URL built from
# the model key (requires deploy-llm-service to have created <key>-vllm Service).
_ogx_llm_url   = $(or $(LLM_URL),http://$(LLM_MODEL_KEY)-vllm.$(LLM_NAMESPACE).svc.cluster.local/v1)
# OGX_MODEL_ID: model ID the backend sends to OGX-AI when making inference calls.
# Matches models.<LLM_MODEL_KEY>.id in the ogx-ai Helm values (e.g. meta-llama/Llama-3.1-8B-Instruct).
OGX_MODEL_ID   ?= $(LLM_MODEL_KEY)/$(LLM_MODEL_NAME)

# ── Phony targets ──────────────────────────────────────────────────────────────
.PHONY: build-all deploy-all deploy-storage deploy-app undeploy-all \
        build-backend build-data-fetch build-data-generator \
        build-frontend build-pipeline-base build-predictor build-scoring \
        deploy-namespace deploy-secrets deploy-data-connection deploy-pgvector \
        deploy-serving deploy-serving-rbac deploy-serving-churn deploy-serving-fraud deploy-serving-starlink-qoe \
        deploy-data-gen deploy-data-fetch deploy-predictor \
        deploy-aws-compatible-storage deploy-ogx-ai deploy-pipelines deploy-backend \
        deploy-scoring deploy-frontend deploy-llm-service \
        verify-aws-compatible-storage verify-pgvector verify-pipelines verify-serving \
        verify-data-gen verify-data-fetch verify-predictor verify-ogx-ai verify-backend \
        verify-scoring verify-frontend \
        upload-datasets run-pipeline-churn run-pipeline-fraud run-pipeline-qoe \
        undeploy-infra undeploy-app \
        undeploy-namespace undeploy-aws-compatible-storage undeploy-llm-service \
        undeploy-secrets undeploy-pgvector \
        undeploy-serving undeploy-serving-rbac undeploy-serving-churn undeploy-serving-fraud undeploy-serving-starlink-qoe \
        undeploy-data-gen undeploy-data-fetch undeploy-predictor \
        undeploy-ogx-ai undeploy-pipelines undeploy-backend \
        undeploy-scoring undeploy-frontend \
        undeploy-data-connection

# ── Build targets ───────────────────────────────────────────────────────────────
# Apply ImageStream + BuildConfig manifests then start binary builds.
# Run from the repo root.

build-all: build-backend build-data-fetch build-data-generator build-frontend build-pipeline-base build-predictor build-scoring

build-backend:
	oc apply -f build/backend/build.yaml -n $(NAMESPACE)
	oc start-build telco-backend --from-dir=.. --follow -n $(NAMESPACE)

build-data-fetch:
	oc apply -f build/tools/data-fetch/build.yaml -n $(NAMESPACE)
	oc start-build data-fetch-tool --from-dir=../tools/data-fetch/ --follow -n $(NAMESPACE)

build-data-generator:
	oc apply -f build/data-generator/build.yaml -n $(NAMESPACE)
	oc start-build data-generator --from-dir=../data-generator/ --follow -n $(NAMESPACE)

build-frontend:
	oc apply -f build/frontend/build.yaml -n $(NAMESPACE)
	oc start-build telco-frontend --from-dir=../frontend/ --follow -n $(NAMESPACE)

build-predictor:
	oc apply -f build/tools/predictor/build.yaml -n $(NAMESPACE)
	oc start-build predictor --from-dir=../tools/predictor/ --follow -n $(NAMESPACE)

build-pipeline-base:
	oc apply -f build/pipeline-base/build.yaml -n $(NAMESPACE)
	oc start-build pipeline-base --from-dir=../demo/pipeline-base/ --follow -n $(NAMESPACE)

build-scoring:
	oc apply -f build/scoring/build.yaml -n $(NAMESPACE)
	oc start-build scoring --from-dir=../scoring/ --follow -n $(NAMESPACE)

# ── Deploy targets (individual) ─────────────────────────────────────────────────
# Each target can be run standalone. deploy-all chains them in dependency order.

deploy-aws-compatible-storage:
	helm upgrade --install aws-compatible-storage helm/aws-compatible-storage \
	  -n $(S4_NAMESPACE) \
	  --set aws-compatible-storage.storage.data.storageClass=$(STORAGE_CLASS) \
	  --set aws-compatible-storage.s3.accessKeyId=$(S3_ACCESS_KEY) \
	  --set aws-compatible-storage.s3.secretAccessKey=$(S3_SECRET_KEY) \
	  --wait

undeploy-aws-compatible-storage:
	helm uninstall aws-compatible-storage -n $(S4_NAMESPACE) || true

deploy-namespace:
	# Create the OpenShift project (no-op if it already exists)
	oc new-project $(NAMESPACE) 2>/dev/null || true
	# Label it as an OpenShift AI Data Science Project
	oc label namespace $(NAMESPACE) \
	  opendatahub.io/dashboard=true \
	  modelmesh-enabled=false \
	  --overwrite

deploy-secrets: deploy-data-connection
	oc create secret generic $(DB_SECRET) \
	  --from-literal=DATABASE_URL="postgresql://$(PGVECTOR_USER):$(PGVECTOR_PASSWORD)@postgres.$(NAMESPACE).svc.cluster.local:5432/$(PGVECTOR_DBNAME)" \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	oc create secret generic dashboard-dspa-secret \
	  --from-literal=AWS_ACCESS_KEY_ID=$(S3_ACCESS_KEY) \
	  --from-literal=AWS_SECRET_ACCESS_KEY=$(S3_SECRET_KEY) \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	oc create serviceaccount my-data-connection-sa \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -

# Creates a properly labelled RHOAI data connection secret.
# KServe and DSPA both require opendatahub.io/managed=true and
# opendatahub.io/dashboard=true to recognise the secret as a data connection
# and populate the agent's models.json.
deploy-data-connection:
	oc create secret generic my-data-connection \
	  --from-literal=AWS_ACCESS_KEY_ID=$(S3_ACCESS_KEY) \
	  --from-literal=AWS_SECRET_ACCESS_KEY=$(S3_SECRET_KEY) \
	  --from-literal=AWS_S3_BUCKET=$(S3_BUCKET) \
	  --from-literal=AWS_S3_ENDPOINT=$(S3_SCHEME)://$(S3_HOST):$(S3_PORT) \
	  --from-literal=AWS_DEFAULT_REGION=us-east-1 \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	oc label secret my-data-connection \
	  opendatahub.io/managed=true \
	  opendatahub.io/dashboard=true \
	  -n $(NAMESPACE) --overwrite
	oc annotate secret my-data-connection \
	  opendatahub.io/connection-type=s3 \
	  opendatahub.io/connection-type-protocol=s3 \
	  opendatahub.io/connection-type-ref=s3 \
	  "openshift.io/display-name=My Data Connection" \
	  "openshift.io/description=data connection object storage" \
	  -n $(NAMESPACE) --overwrite

deploy-pgvector:
	helm upgrade --install pgvector helm/pgvector \
	  -n $(NAMESPACE) \
	  -f values/postgres/values-showcase.yaml \
	  --set secret.user=$(PGVECTOR_USER) \
	  --set secret.password=$(PGVECTOR_PASSWORD) \
	  --set secret.dbname=$(PGVECTOR_DBNAME) \
	  --set vectordb=false \
	  $(if $(PGVECTOR_IMAGE),--set image.repository=$(_pgvector_repo) --set image.tag=$(_pgvector_tag))

deploy-serving: deploy-serving-rbac deploy-serving-churn deploy-serving-fraud deploy-serving-starlink-qoe

deploy-serving-rbac:
	oc apply -f serving/rbac.yaml -n $(NAMESPACE)

deploy-serving-churn:
	oc apply -f serving/churn-servingruntime.yaml -n $(NAMESPACE)
	oc apply -f serving/churn-inferenceservice.yaml -n $(NAMESPACE)

deploy-serving-fraud:
	oc apply -f serving/rafm-servingruntime.yaml -n $(NAMESPACE)
	oc apply -f serving/rafm-inferenceservice.yaml -n $(NAMESPACE)

deploy-serving-starlink-qoe:
	oc apply -f serving/starlink-qoe-servingruntime.yaml -n $(NAMESPACE)
	oc apply -f serving/starlink-qoe-inferenceservice.yaml -n $(NAMESPACE)

deploy-data-gen:
	helm upgrade --install data-gen helm/data-generator \
	  -n $(NAMESPACE) \
	  --set database.existingSecret=$(DB_SECRET) \
	  --set generator.nCustomers=10000 \
	  --set generator.nRegions=4 \
	  --set generator.coupling=0.5 \
	  --set generator.nTransactions=10000 \
	  $(if $(call img,data-generator),--set image.repository=$(call img,data-generator) --set image.tag=$(IMAGE_TAG))
	oc wait --for=condition=complete job -l app.kubernetes.io/instance=data-gen \
	  -n $(NAMESPACE) --timeout=600s

deploy-data-fetch:
	helm upgrade --install data-fetch helm/tools/data-fetch \
	  -n $(NAMESPACE) \
	  --set database.existingSecret=$(DB_SECRET) \
	  --set database.existingSecretKey=DATABASE_URL \
	  $(if $(call img,data-fetch),--set image.repository=$(call img,data-fetch) --set image.tag=$(IMAGE_TAG))

deploy-predictor:
	helm upgrade --install predictor helm/tools/predictor \
	  -n $(NAMESPACE) \
	  -f helm/tools/predictor/values-showcase.yaml \
	  --set dataFetch.url=http://data-fetch.$(NAMESPACE).svc:8080 \
	  --set models.churn.url=https://churn-prediction-predictor.$(NAMESPACE).svc:8443/v2/models/churn-prediction/infer \
	  --set models.fraud.url=https://fraud-prediction-predictor.$(NAMESPACE).svc:8443/v2/models/fraud-prediction/infer \
	  --set models.qoe.url=https://starlink-qoe-predictor.$(NAMESPACE).svc:8443/v2/models/starlink-qoe/infer \
	  --set qoe.artifacts.endpoint=$(S3_SCHEME)://$(S3_HOST):$(S3_PORT) \
	  $(if $(call img,predictor),--set image.repository=$(call img,predictor) --set image.tag=$(IMAGE_TAG))

deploy-llm-service:
	oc new-project $(LLM_NAMESPACE) 2>/dev/null || true
	helm upgrade --install llm-service helm/llm-service \
	  --set device=gpu \
	  --set models.$(LLM_MODEL_KEY).enabled=true \
	  --set secret.hf_token=$(HF_TOKEN) \
	  $(if $(LLM_API_TOKEN),--set "models.$(LLM_MODEL_KEY).args[0]=--api-key" --set "models.$(LLM_MODEL_KEY).args[1]=$(LLM_API_TOKEN)") \
	  -n $(LLM_NAMESPACE)

undeploy-llm-service:
	helm uninstall llm-service -n $(LLM_NAMESPACE) || true

deploy-ogx-ai:
	helm upgrade --install ogx-ai helm/ogx-ai \
	  -n $(NAMESPACE) \
	  --set models.$(LLM_MODEL_KEY).enabled=true \
	  --set models.$(LLM_MODEL_KEY).url=$(_ogx_llm_url) \
	  $(if $(LLM_API_TOKEN),--set models.$(LLM_MODEL_KEY).apiToken=$(LLM_API_TOKEN))

deploy-pipelines:
	S3_HOST_FQDN=aws-compatible-storage.$(S4_NAMESPACE).svc.cluster.local \
	S3_HOST=$(S3_HOST) S3_PORT=$(S3_PORT) \
	S3_SCHEME=$(S3_SCHEME) S3_BUCKET=$(S3_BUCKET) \
	STORAGE_CLASS=$(STORAGE_CLASS) \
	  envsubst < values/dspa/dspa.yaml | oc apply -f - -n $(NAMESPACE)

deploy-backend:
	helm upgrade --install backend helm/backend \
	  -n $(NAMESPACE) \
	  --set database.existingSecret=$(DB_SECRET) \
	  --set database.existingSecretKey=DATABASE_URL \
	  --set ogx.modelId="$(OGX_MODEL_ID)" \
	  $(if $(call img,backend),--set image.repository=$(call img,backend) --set image.tag=$(IMAGE_TAG))

deploy-scoring:
	SCORING_IMAGE=$(SCORING_IMAGE) NAMESPACE=$(NAMESPACE) \
	  envsubst '$$SCORING_IMAGE $$NAMESPACE' < build/scoring/job.yaml \
	  | oc apply -f - -n $(NAMESPACE)

deploy-frontend:
	helm upgrade --install frontend helm/frontend \
	  -n $(NAMESPACE) \
	  --set fullnameOverride=frontend \
	  --set backendUrl=http://backend.$(NAMESPACE).svc.cluster.local:8000 \
	  $(if $(call img,frontend),--set image.repository=$(call img,frontend) --set image.tag=$(IMAGE_TAG))

# ── Verify targets ─────────────────────────────────────────────────────────────
# Each verify-* checks the matching component is healthy before continuing.
# Usable standalone: make verify-backend

verify-aws-compatible-storage:
	oc rollout status deployment/aws-compatible-storage -n $(S4_NAMESPACE) --timeout=120s

verify-pgvector:
	oc wait pod/postgres-0 -n $(NAMESPACE) --for=condition=Ready --timeout=120s

verify-pipelines:
	@echo "Waiting for DSPA operator to create pipeline deployments..."
	@until oc get deployment/ds-pipeline-dspa -n $(NAMESPACE) &>/dev/null; do sleep 10; done
	@until oc get deployment/ds-pipeline-scheduledworkflow-dspa -n $(NAMESPACE) &>/dev/null; do sleep 10; done
	@until oc get deployment/ds-pipeline-persistenceagent-dspa -n $(NAMESPACE) &>/dev/null; do sleep 10; done
	oc wait deployment/ds-pipeline-dspa \
	           deployment/ds-pipeline-scheduledworkflow-dspa \
	           deployment/ds-pipeline-persistenceagent-dspa \
	  -n $(NAMESPACE) --for=condition=Available --timeout=300s
	@echo "Waiting for KFP API route to become ready..."
	@KFP_HOST=$$(oc get route ds-pipeline-dspa -n $(NAMESPACE) -o jsonpath='{.spec.host}' 2>/dev/null); \
	 TOKEN=$$(oc whoami -t 2>/dev/null); \
	 for i in $$(seq 1 30); do \
	   STATUS=$$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $$TOKEN" "https://$$KFP_HOST/apis/v2beta1/pipelines" 2>/dev/null); \
	   if [ "$$STATUS" = "200" ] || [ "$$STATUS" = "401" ]; then \
	     echo "KFP API is ready (HTTP $$STATUS)."; exit 0; \
	   fi; \
	   echo "  KFP route not ready yet (HTTP $$STATUS), retrying in 10s... ($$i/30)"; \
	   sleep 10; \
	 done; \
	 echo "Warning: KFP route did not become ready after 300s — proceeding anyway."; true

# InferenceServices won't be Ready until models exist in S4 (run-pipelines).
# This verify confirms the resources were created; readiness follows after pipelines.
verify-serving:
	oc get inferenceservice churn-prediction fraud-prediction starlink-qoe \
	  -n $(NAMESPACE)
	@echo "ServingRuntimes and InferenceServices created."
	@echo "Run 'make run-pipelines' to train models — InferenceServices become Ready once models land in S4."

verify-data-fetch:
	oc rollout status deployment/data-fetch -n $(NAMESPACE) --timeout=180s

verify-predictor:
	oc rollout status deployment/predictor -n $(NAMESPACE) --timeout=180s

verify-ogx-ai:
	oc rollout status deployment/ogx-ai -n $(NAMESPACE) --timeout=600s


verify-backend:
	oc rollout status deployment/backend -n $(NAMESPACE) --timeout=180s

verify-scoring:
	oc wait job/scoring -n $(NAMESPACE) --for=condition=complete --timeout=600s

verify-frontend:
	oc rollout status deployment/frontend -n $(NAMESPACE) --timeout=180s

# ── Phase 1a: deploy-storage ───────────────────────────────────────────────────
# Deploys and verifies storage layer: namespace, secrets, S4, pgvector, data-gen.
# Runs before upload-datasets so S4 is available for dataset upload.
verify-data-gen:
	oc wait job/data-gen-data-generator -n $(NAMESPACE) \
	  --for=condition=complete --timeout=600s

deploy-storage: deploy-namespace deploy-secrets \
                deploy-aws-compatible-storage verify-aws-compatible-storage \
                deploy-pgvector verify-pgvector \
                $(if $(PGVECTOR_IMAGE),,deploy-data-gen verify-data-gen)

# ── Phase 1: deploy-infra ───────────────────────────────────────────────────────
# Full infra phase: storage → (upload-datasets via root Makefile) → DSPA.
# When called from deploy/Makefile directly, upload-datasets is skipped.
# Use root Makefile `make deploy-infra` to include the upload-datasets step.
# deploy-infra is intentionally not defined here.
# Use `make deploy-infra` from the repo root — it inserts upload-datasets between
# deploy-storage and deploy-pipelines. Running it from this directory skips that step.

# ── Phase 2: run-pipelines (root Makefile) ──────────────────────────────────────
# Train ML models and store artifacts in S4.
# Run from the repo root: make run-pipelines
# KServe InferenceServices become Ready once models appear in S4.

# ── Phase 3: deploy-app ─────────────────────────────────────────────────────────
# Deploys and verifies all application components.
# Requires: deploy-infra complete + models in S4 (run-pipelines done).
deploy-app: deploy-serving verify-serving \
            deploy-data-fetch verify-data-fetch \
            deploy-predictor verify-predictor \
            deploy-ogx-ai verify-ogx-ai \
            deploy-backend verify-backend \
            deploy-scoring verify-scoring \
            deploy-frontend verify-frontend

# ── deploy-all — chains all three phases ───────────────────────────────────────
# For a fresh cluster without pre-trained models, run-pipelines must be called
# between deploy-infra and deploy-app (see root Makefile `deploy` target).
# deploy-all skips run-pipelines to stay cluster-agnostic (no KFP SDK locally).
deploy-all: deploy-infra deploy-app

# ── Undeploy targets (individual) ──────────────────────────────────────────────

undeploy-frontend:
	helm uninstall frontend -n $(NAMESPACE) || true

undeploy-backend:
	helm uninstall backend -n $(NAMESPACE) || true

undeploy-scoring:
	oc delete job scoring -n $(NAMESPACE) || true

undeploy-pipelines:
	oc delete datasciencepipelinesapplication dspa -n $(NAMESPACE) || true

undeploy-ogx-ai:
	helm uninstall ogx-ai -n $(NAMESPACE) || true

undeploy-predictor:
	helm uninstall predictor -n $(NAMESPACE) || true

undeploy-data-fetch:
	helm uninstall data-fetch -n $(NAMESPACE) || true

undeploy-data-gen:
	helm uninstall data-gen -n $(NAMESPACE) || true

undeploy-serving: undeploy-serving-churn undeploy-serving-fraud undeploy-serving-starlink-qoe undeploy-serving-rbac
	@:

undeploy-serving-rbac:
	oc delete -f serving/rbac.yaml -n $(NAMESPACE) || true

undeploy-serving-churn:
	oc delete -f serving/churn-inferenceservice.yaml -n $(NAMESPACE) || true
	oc delete -f serving/churn-servingruntime.yaml -n $(NAMESPACE) || true

undeploy-serving-fraud:
	oc delete -f serving/rafm-inferenceservice.yaml -n $(NAMESPACE) || true
	oc delete -f serving/rafm-servingruntime.yaml -n $(NAMESPACE) || true

undeploy-serving-starlink-qoe:
	oc delete -f serving/starlink-qoe-inferenceservice.yaml -n $(NAMESPACE) || true
	oc delete -f serving/starlink-qoe-servingruntime.yaml -n $(NAMESPACE) || true

undeploy-pgvector:
	helm uninstall pgvector -n $(NAMESPACE) || true

undeploy-secrets:
	oc delete secret $(DB_SECRET) dashboard-dspa-secret \
	  -n $(NAMESPACE) || true
	oc delete serviceaccount my-data-connection-sa \
	  -n $(NAMESPACE) || true

undeploy-data-connection:
	oc delete secret my-data-connection -n $(NAMESPACE) || true

undeploy-namespace:
	oc delete project $(NAMESPACE) || true

# ── undeploy-all — full stack in reverse dependency order ───────────────────────
# Note: undeploy-namespace is intentionally excluded — delete the project explicitly
#       with `make undeploy-namespace` to avoid accidental data loss.
undeploy-infra: undeploy-pipelines undeploy-pgvector undeploy-aws-compatible-storage undeploy-secrets undeploy-data-connection
	@:

undeploy-app: undeploy-frontend undeploy-scoring undeploy-backend undeploy-ogx-ai \
              undeploy-predictor undeploy-data-fetch undeploy-serving
	@:

undeploy-all: undeploy-app undeploy-data-gen undeploy-infra
	@:

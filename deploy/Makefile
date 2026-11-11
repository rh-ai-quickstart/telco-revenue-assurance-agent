# ── Target namespace ───────────────────────────────────────────────────────────
NAMESPACE ?= telco-revenue-assurance-agent

# ── PostgreSQL credentials ─────────────────────────────────────────────────────
PGVECTOR_USER     ?= postgres
PGVECTOR_PASSWORD ?= change-me
PGVECTOR_DBNAME   ?= postgres
DB_SECRET         ?= database-secret

# ── MinIO / object storage (used by DSPA and pipeline data connections) ────────
# MINIO_HOST defaults to the internal service created by the ogx-ai chart.
# Override with an external route if needed:
#   make deploy-pipelines MINIO_HOST=minio-api-<ns>.apps.<cluster> MINIO_SCHEME=https MINIO_PORT=443
MINIO_HOST          ?= minio-service.showcase.svc
MINIO_PORT          ?= 9000
MINIO_SCHEME        ?= http
MINIO_BUCKET        ?= mybucket
MINIO_USER          ?= minio
MINIO_PASSWORD      ?= minio123
MINIO_NAMESPACE     ?= showcase
MINIO_STORAGE_CLASS ?= lvms-vg1

# ── LLM service (vLLM) ─────────────────────────────────────────────────────────
# Deployed into LLM_NAMESPACE alongside MinIO.
# Override VLLM_URL to point OGX at a different endpoint (internal or external):
#   make deploy-ogx-ai VLLM_URL=http://llama-3-1-8b-instruct-vllm.showcase.svc:80/v1
LLM_NAMESPACE ?= $(NAMESPACE)
HF_TOKEN      ?=
VLLM_URL      ?=

# ── Phony targets ──────────────────────────────────────────────────────────────
.PHONY: build-all deploy-all undeploy-all \
        build-backend build-data-fetch build-data-generator \
        build-frontend build-pipeline-base build-predictor build-scoring \
        deploy-namespace deploy-secrets deploy-data-connection deploy-pgvector \
        deploy-serving deploy-serving-rbac deploy-serving-churn deploy-serving-fraud deploy-serving-starlink-qoe \
        deploy-data-gen deploy-data-fetch deploy-predictor \
        deploy-ogx-ai deploy-pipelines deploy-backend \
        deploy-scoring deploy-frontend deploy-minio deploy-llm-service \
        upload-datasets run-pipeline-churn run-pipeline-fraud run-pipeline-qoe \
        undeploy-namespace undeploy-minio undeploy-llm-service \
        undeploy-secrets undeploy-pgvector \
        undeploy-serving undeploy-serving-rbac undeploy-serving-churn undeploy-serving-fraud undeploy-serving-starlink-qoe \
        undeploy-data-gen undeploy-data-fetch undeploy-predictor \
        undeploy-ogx-ai undeploy-pipelines undeploy-backend \
        undeploy-scoring undeploy-frontend

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

deploy-minio:
	oc new-project $(MINIO_NAMESPACE) 2>/dev/null || true
	oc apply -f minio/secret.yaml -n $(MINIO_NAMESPACE)
	sed 's|storageClassName: lvms-vg1|storageClassName: $(MINIO_STORAGE_CLASS)|g' minio/pvc.yaml \
	  | oc apply -f - -n $(MINIO_NAMESPACE)
	oc apply -f minio/deployment.yaml -n $(MINIO_NAMESPACE)
	oc apply -f minio/service.yaml -n $(MINIO_NAMESPACE)
	oc apply -f minio/route.yaml -n $(MINIO_NAMESPACE)

undeploy-minio:
	oc delete -f minio/route.yaml -n $(MINIO_NAMESPACE) || true
	oc delete -f minio/service.yaml -n $(MINIO_NAMESPACE) || true
	oc delete -f minio/deployment.yaml -n $(MINIO_NAMESPACE) || true
	oc delete -f minio/pvc.yaml -n $(MINIO_NAMESPACE) || true
	oc delete -f minio/secret.yaml -n $(MINIO_NAMESPACE) || true

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
	  --from-literal=AWS_ACCESS_KEY_ID=$(MINIO_USER) \
	  --from-literal=AWS_SECRET_ACCESS_KEY=$(MINIO_PASSWORD) \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	oc create serviceaccount my-data-connection-sa \
	  -n $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -

# Creates a properly labelled RHOAI data connection secret.
# KServe and DSPA both require opendatahub.io/managed=true and
# opendatahub.io/dashboard=true to recognise the secret as a data connection
# and populate the agent's models.json.
deploy-data-connection:
	oc create secret generic my-data-connection \
	  --from-literal=AWS_ACCESS_KEY_ID=$(MINIO_USER) \
	  --from-literal=AWS_SECRET_ACCESS_KEY=$(MINIO_PASSWORD) \
	  --from-literal=AWS_S3_BUCKET=$(MINIO_BUCKET) \
	  --from-literal=AWS_S3_ENDPOINT=$(MINIO_SCHEME)://$(MINIO_HOST):$(MINIO_PORT) \
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
	  --set vectordb=false

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
	  --set generator.nTransactions=10000
	oc wait --for=condition=complete job -l app.kubernetes.io/instance=data-gen \
	  -n $(NAMESPACE) --timeout=600s

deploy-data-fetch:
	helm upgrade --install data-fetch helm/tools/data-fetch \
	  -n $(NAMESPACE) \
	  --set database.existingSecret=$(DB_SECRET) \
	  --set database.existingSecretKey=DATABASE_URL

deploy-predictor:
	helm upgrade --install predictor helm/tools/predictor \
	  -n $(NAMESPACE) \
	  -f helm/tools/predictor/values-showcase.yaml

deploy-llm-service:
	oc new-project $(LLM_NAMESPACE) 2>/dev/null || true
	helm upgrade --install llm-service helm/llm-service \
	  --set device=gpu \
	  --set models.llama-3-1-8b-instruct.enabled=true \
	  --set secret.hf_token=$(HF_TOKEN) \
	  -n $(LLM_NAMESPACE)

undeploy-llm-service:
	helm uninstall llm-service -n $(LLM_NAMESPACE) || true

deploy-ogx-ai:
	helm upgrade --install ogx-ai helm/ogx-ai \
	  -n $(NAMESPACE) \
	  $(if $(VLLM_URL),--set models.remote-llm.url=$(VLLM_URL))

deploy-pipelines:
	MINIO_HOST=$(MINIO_HOST) MINIO_PORT=$(MINIO_PORT) \
	MINIO_SCHEME=$(MINIO_SCHEME) MINIO_BUCKET=$(MINIO_BUCKET) \
	  envsubst < values/dspa/dspa.yaml | oc apply -f - -n $(NAMESPACE)

deploy-backend:
	helm upgrade --install backend helm/backend \
	  -n $(NAMESPACE) \
	  --set database.existingSecret=$(DB_SECRET) \
	  --set database.existingSecretKey=DATABASE_URL

deploy-scoring:
	sed 's|telco-revenue-assurance-agent|$(NAMESPACE)|g' build/scoring/job.yaml \
	  | oc apply -f - -n $(NAMESPACE)

deploy-frontend:
	helm upgrade --install frontend helm/frontend \
	  -n $(NAMESPACE) \
	  --set fullnameOverride=frontend \
	  --set backendUrl=http://backend.$(NAMESPACE).svc.cluster.local:8000

# ── deploy-all — full stack in dependency order ─────────────────────────────────
# namespace → pgvector → pipelines (DSPA) → serving (KServe) →
# data-gen → data-fetch → predictor → ogx-ai → backend → scoring → frontend
deploy-all: deploy-namespace deploy-secrets deploy-pgvector deploy-pipelines deploy-serving \
            deploy-data-gen deploy-data-fetch deploy-predictor deploy-ogx-ai \
            deploy-backend deploy-scoring deploy-frontend

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
undeploy-all: undeploy-frontend undeploy-scoring undeploy-backend undeploy-ogx-ai \
              undeploy-predictor undeploy-data-fetch undeploy-data-gen undeploy-serving \
              undeploy-pipelines undeploy-pgvector undeploy-secrets

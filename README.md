# Detect Telco Revenue Losses with an AI Agent

Detect churn risk, billing fraud, and network QoE issues using a conversational AI agent backed by three ML models on OpenShift® AI.

## Requirements

### Hardware

| Node Type | Qty | vCPU | Memory |
|---|---|---|---|
| Control Plane | 3 | 8 | 16 GB |
| Worker | 2 | 16 | 32 GB |
| GPU Worker (optional) | 1 | 16 | 64 GB + 1× NVIDIA A10 / A100 / L40S |

> **GPU note:** A GPU node is required to run the LLM service on-cluster (Step 2). Without one, skip Step 2 and point `ogx-ai` at a remote OpenAI-compatible vLLM endpoint: `make deploy-ogx-ai VLLM_URL=<your-vllm-url>`.

### Software

| Software | Tested version |
|---|---|
| Red Hat OpenShift Container Platform | 4.22 (stable-4.22) |
| Red Hat OpenShift AI | 3.4.2 |
| Kubeflow Pipelines | 2.16.0 |
| KServe | 0.17.0 |
| Helm | 3.14+ |
| `oc` CLI | 4.22 |

### Permissions

| Resource group | Required role |
|---|---|
| Helm charts (`pgvector`, `ogx-ai`, tools, frontend) | Namespace admin |
| KServe manifests (`deploy/serving/`) | Namespace admin + OpenShift AI project member |

### Prerequisites

Before deploying, ensure the following are in place:

- **OCP CLI login** — you are authenticated against the target cluster (`oc whoami` returns your username).
- **OCP internal registry enabled** — the cluster internal registry is running and accessible; required by the `make build-*` targets that push images via `BuildConfig` and `ImageStream`.
- **Object storage** — an S3-compatible bucket (MinIO or equivalent) must be available; used by the Data Science Pipeline Application (DSPA) and the data connection for model artifacts. If not already deployed, use `make deploy-minio` (see Step 1 below).

## Deploy

### 0. Set environment variables

Export these once before running any step:

```bash
export NAMESPACE=telco-revenue-assurance-agent
export PGVECTOR_PASSWORD=<your-password>
export MINIO_PASSWORD=<your-minio-password>
export HF_TOKEN=<your-huggingface-token>
```

> `NAMESPACE` defaults to `telco-revenue-assurance-agent` and is inherited by all `make` targets automatically — no need to repeat it on each command. To use a different namespace, change the export above. Override any other value inline: `make deploy-X VAR=value`.

Clone the repo and change into the repo root:

```bash
git clone https://github.com/rh-ai-quickstart/telco-revenue-assurance-agent.git
cd telco-revenue-assurance-agent
```

---

### Step 1 — MinIO (object storage)

Deploys MinIO into a dedicated namespace (`showcase` by default). MinIO is used to store training datasets, KFP pipeline artifacts, and trained model files consumed by KServe. Skip this step if you already have an S3-compatible object store available.

```bash
make deploy-minio
```

Override the namespace or storage class if needed:

```bash
make deploy-minio MINIO_NAMESPACE=showcase MINIO_STORAGE_CLASS=gp3-csi
```

**Verify:**
```bash
oc get pods -n showcase -l app=minio
oc get route minio-api minio-ui -n showcase
```
Expected: pod in `Running` state and two routes with assigned hostnames.

---

### Step 2 — Namespace

Creates the OpenShift project and labels it as an OpenShift AI Data Science Project. The default namespace is `telco-revenue-assurance-agent`.

```bash
make deploy-namespace
```

**Verify:**
```bash
oc get project $NAMESPACE
```
Expected: project in `Active` state.

---

### Step 3 — LLM Service (vLLM)

Deploys the vLLM serving runtime with Llama 3.1 8B Instruct into the project namespace. Requires a GPU node.

> **Demo note:** This quickstart was validated using a remote vLLM endpoint on a separate GPU cluster. If no GPU node is available, skip this step and pass `VLLM_URL` pointing to your remote endpoint when deploying OGX in Step 12.

**HuggingFace prerequisites:**
- Create an access token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
- Request access to the model at [huggingface.co/meta-llama/Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct)

```bash
make deploy-llm-service HF_TOKEN=$HF_TOKEN
```

Once deployed, the internal vLLM service URL is `http://llama-3-1-8b-instruct-vllm.$NAMESPACE.svc:80/v1`. Pass it when deploying OGX in Step 12:

```bash
make deploy-ogx-ai VLLM_URL=http://llama-3-1-8b-instruct-vllm.$NAMESPACE.svc:80/v1
```

**Verify:**
```bash
oc get inferenceservice llama-3-1-8b-instruct -n $NAMESPACE
```
Expected: `InferenceService` in `Ready` state.

---

### Step 4 — Secrets

Creates three secrets in the project namespace (`telco-revenue-assurance-agent` by default): the database URL, the MinIO data connection for DSPA, and the S3 data connection for KServe model serving.

```bash
make deploy-secrets \
  PGVECTOR_PASSWORD=$PGVECTOR_PASSWORD \
  MINIO_PASSWORD=$MINIO_PASSWORD
```

**Verify:**
```bash
oc get secret database-secret dashboard-dspa-secret my-data-connection \
  -n $NAMESPACE
```
Expected: all three secrets show `Opaque` type.

---

### Step 5 — PostgreSQL

Deploys a PostgreSQL instance. This is the primary data store for customer, transaction, and scoring data.

```bash
make deploy-pgvector \
  PGVECTOR_PASSWORD=$PGVECTOR_PASSWORD
```

**Verify:**
```bash
oc get pods -n $NAMESPACE -l app.kubernetes.io/instance=pgvector
```
Expected: pod in `Running` state. Confirm connectivity:
```bash
oc exec postgres-0 -n $NAMESPACE -- psql -U postgres -c '\l'
```

---

### Step 6 — Data Science Pipeline Application (DSPA)

Deploys the Kubeflow Pipelines (KFP) server backed by MinIO object storage. This is used to run the ML training pipelines for churn, fraud, and QoE models.

```bash
make deploy-pipelines
```

> **MinIO host:** the default is `minio-service.showcase.svc:9000`. If your MinIO is external, override: `make deploy-pipelines MINIO_HOST=minio-api-<ns>.apps.<cluster> MINIO_SCHEME=https MINIO_PORT=443`.

**Verify:**
```bash
oc get datasciencepipelinesapplication dspa -n $NAMESPACE
```
Expected: `READY` column is `True`. For full status:
```bash
oc get datasciencepipelinesapplication dspa -n $NAMESPACE \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}{"\n"}'
```

---

### Step 7 — ML Training Pipelines

Builds the pipeline base image, uploads training datasets to MinIO, then runs the three KFP training pipelines (churn, fraud/RAFM, QoE). Each pipeline trains a model and writes the artifact to S3 at a path that KServe will serve from.

```bash
# Build the pipeline base image (once — skip if already done)
make build-pipeline-base

# Upload training datasets to MinIO (once — skip if already done)
make upload-datasets

# Run the three training pipelines
make run-pipeline-churn
make run-pipeline-fraud
make run-pipeline-qoe
```

Each `run-pipeline-*` command auto-detects the KFP endpoint and bearer token from your active OCP session and submits the pipeline run programmatically.

| Make target | Model trained | S3 output path |
|---|---|---|
| `run-pipeline-churn` | Churn (LightGBM) | `models/churn/1/` |
| `run-pipeline-fraud` | Fraud/RAFM (ONNX) | `models/fraud/1/` |
| `run-pipeline-qoe` | QoE (ONNX) | `models/starlink-qoe/1/` |

**Verify:**

```bash
oc get route ds-pipeline-dspa -n $NAMESPACE \
  -o jsonpath='{.spec.host}{"\n"}'
```

Open the printed URL in a browser → **Experiments** → confirm each pipeline run completes successfully. Model files should appear in MinIO at the S3 output paths listed above.

---

### Step 8 — Model Serving (KServe InferenceServices)

Deploys three `ServingRuntime` and three `InferenceService` resources for the churn (LightGBM), fraud/RAFM (ONNX), and QoE (ONNX) models.

```bash
make deploy-serving
```

**Verify:**
```bash
oc get servingruntime,inferenceservice -n $NAMESPACE
```
Expected: 3 `ServingRuntime` and 3 `InferenceService` resources. Models remain `Unknown/Loading` until the pipeline runs in Step 5 have written model files to S3 — that is expected at this stage.

---

### Step 9 — Data Generator

Builds and runs a one-off Kubernetes Job that populates PostgreSQL with 10,000 synthetic but statistically realistic telco customer and transaction records.

```bash
make build-data-generator
make deploy-data-gen \
  PGVECTOR_PASSWORD=$PGVECTOR_PASSWORD
```

The Makefile waits for the Job to complete (up to 10 minutes). To follow logs live:
```bash
oc logs -f -l app.kubernetes.io/instance=data-gen -n $NAMESPACE
```

**Verify:**
```bash
oc get job -l app.kubernetes.io/instance=data-gen -n $NAMESPACE
oc exec postgres-0 -n $NAMESPACE -- psql -U postgres -d postgres \
  -c "SELECT COUNT(*) FROM customers;"
```
Expected: Job in `Complete` state, query returns `10000`.

---

### Step 10 — Data Fetch Tool

Builds and deploys a FastAPI microservice that exposes structured SQL queries over HTTP. The agent and predictor use it to look up customer and transaction records, as well as pre-computed inference results (churn, fraud, and QoE scores written by the scoring job), without direct database access.

```bash
make build-data-fetch
make deploy-data-fetch
```

**Verify:**
```bash
oc get pods -n $NAMESPACE -l app.kubernetes.io/instance=data-fetch
oc get svc data-fetch -n $NAMESPACE
```
Expected: pod `Running`, service `data-fetch` on port `8080`.

---

### Step 11 — Predictor Tool

Builds and deploys the predictor service that wraps the three KServe InferenceService endpoints. The agent calls this tool to score individual customers for churn risk, fraud, and QoE.

```bash
make build-predictor
make deploy-predictor
```

**Verify:**
```bash
oc get pods -n $NAMESPACE -l app.kubernetes.io/instance=predictor
oc get svc predictor -n $NAMESPACE
```
Expected: pod `Running`, service `predictor` on port `8080`.

---

### Step 12 — ogx-ai / LlamaStack Agent

Deploys the LlamaStack agent server (ogx-ai). This is the ReAct loop that receives analyst questions, decides which tools to call, and streams responses back to the backend.

If you deployed the LLM service in Step 2, pass the internal vLLM URL:

```bash
make deploy-ogx-ai VLLM_URL=http://llama-3-1-8b-instruct-vllm.$NAMESPACE.svc:80/v1
```

Otherwise, pass your remote vLLM endpoint:

```bash
make deploy-ogx-ai VLLM_URL=<your-vllm-endpoint>/v1
```

**Verify:**
```bash
oc get pods -n $NAMESPACE -l app.kubernetes.io/instance=ogx-ai
oc exec -n $NAMESPACE deploy/ogx-ai -- \
  curl -s http://localhost:8321/v1/models | python3 -m json.tool
```
Expected: pod `Running`, models list includes `remote-llm/meta-llama/Llama-3.1-8B-Instruct`.

---

### Step 13 — Backend

Builds and deploys the FastAPI backend that orchestrates dashboard metrics queries, agent chat sessions, and streaming responses to the frontend.

```bash
make build-backend
make deploy-backend
```

**Verify:**
```bash
oc get pods -n $NAMESPACE -l app.kubernetes.io/instance=backend
curl -sk https://$(oc get route backend -n $NAMESPACE \
  -o jsonpath='{.spec.host}')/health
```
Expected: `{"status":"ok"}`.

---

### Step 14 — Scoring Job

Builds and runs a one-off Job that calls all three prediction models for every customer and writes the scores back to PostgreSQL. The dashboard and agent rely on these pre-computed scores.

```bash
make build-scoring
make deploy-scoring
```

**Verify:**
```bash
oc get job scoring -n $NAMESPACE
oc logs job/scoring -n $NAMESPACE
```
Expected: Job completes, logs show churn/fraud/QoE scores written for all 10,000 customers.

---

### Step 15 — Frontend

Builds and deploys the React dashboard. It includes a regional risk metrics view and the floating AI chat panel.

```bash
make build-frontend
make deploy-frontend
```

**Verify:**
```bash
oc get route -n $NAMESPACE
```
Open the `frontend` route URL in a browser. The dashboard should display regional risk metrics and the AI chat panel should respond to analyst questions in the bottom-right corner.

## Delete

```bash
make undeploy-all
```

This uninstalls all Helm releases and removes the KServe resources. Persistent volumes are retained by default. To delete them:

```bash
oc delete pvc --all -n $NAMESPACE
```

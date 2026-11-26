# Detect Telco Revenue Losses with an AI Agent

Detect churn risk, billing fraud, and network QoE issues using a conversational AI agent backed by three ML models on OpenShift® AI.

## Table of Contents

- [Description](#description)
- [Architecture](#architecture)
  - [AI/ML workflow](#aiml-workflow)
- [Usage](#usage)
- [Example Queries](#example-queries)
- [Requirements](#requirements)
- [Deploy](#deploy)
- [Optional components](#optional-components)
- [Delete](#delete)
- [Troubleshooting](#troubleshooting)
- [Tags](#tags)

## Description

This AI quickstart shows how a telecom revenue assurance team can use a conversational AI agent to investigate customer churn risk, billing fraud, and network Quality of Experience (QoE) across customer segments in real time. Rather than running manual SQL queries or switching between dashboards, an analyst asks the agent in plain language and receives model-backed insights in seconds — with the agent deciding autonomously which tools and models to call.

The agent runs on Red Hat® OpenShift AI using OGX with a Llama 3.1 8B Instruct model served by vLLM. It uses a ReAct loop to decide when to query the customer database, when to invoke the churn, fraud or QoE prediction models, and when to retrieve customer and transaction data — combining SQL data retrieval with live ML inference to produce actionable analyst guidance. All three prediction models (churn, fraud/RAFM, and QoE) are served as KServe InferenceServices on Red Hat OpenShift AI, using LightGBM and ONNX runtimes.

A React dashboard gives analysts a high-level regional risk summary and surfaces the same AI agent through a floating chat panel. A FastAPI backend orchestrates communication between the UI, the OGX agent, and a PostgreSQL database pre-seeded with synthetic telco customer, transaction, and network data.

At deployment, a scoring job pre-computes churn, fraud, and QoE predictions for all customers and writes them to the database. The dashboard reads these stored scores to display summary metrics — churn rate, fraud count, and average QoE by region — without hitting the models at runtime. The agent operates on-demand, filtering, slicing, and cross-referencing the same model APIs to answer analyst follow-up questions in real time.

## Architecture

```mermaid
flowchart TB
    User -->|HTTP| Route[OpenShift Route]
    Route --> Frontend[React Dashboard]
    Frontend -->|REST 🔒| Backend[FastAPI API]
    Backend -->|SQL 🔒| DB{{PostgreSQL}}
    Backend --> OGX{{ogx-ai / OGX}}
    OGX -->|OpenAI API| LLM{{llm-service / vLLM}}
    OGX -->|Python SDK| PredTools[Prediction Tools]
    OGX -->|Python SDK| DataFetch[Data Fetch Tool]
    PredTools -->|Customer IDs| DataFetch
    DataFetch -->|SQL 🔒| DB
    PredTools -->|KServe v2 🔒| ChurnModel[KServe: Churn Model]
    PredTools -->|KServe v2 🔒| FraudModel[KServe: Fraud Model]
    PredTools -->|KServe v2 🔒| QoEModel[KServe: QoE Model]
    DataGen[Data Generator] -.->|SQL optional| DB
    Scoring[Scoring Job] -->|HTTP batch predict| PredTools
    Scoring -->|SQL write scores 🔒| DB
```

| Component | Role |
|---|---|
| **React Dashboard** | Regional risk metrics + floating AI chat interface |
| **FastAPI Backend** | REST API — routes chat and metrics requests |
| **ogx-ai / OGX** | Agent orchestration and ReAct tool loop |
| **vLLM** | Llama 3.1 8B Instruct on-cluster inference |
| **Data Fetch Tool** | Queries PostgreSQL for customer and transaction data |
| **Prediction Tools** | Wraps KServe Churn, Fraud, and QoE model endpoints |
| **PostgreSQL** | Customer, transaction, and regional QoE data store |
| **Data Generator** | Optional one-off job to regenerate the synthetic dataset — not required when using the pre-seeded pgvector image |
| **Scoring Job** | One-off batch job that runs churn, fraud, and QoE predictions for all customers and writes scores back to PostgreSQL |
| **KServe Models** | LightGBM (churn), ONNX (fraud/RAFM), ONNX (QoE) |

**Agent tools** called by the ReAct loop:

| Tool | Category | Purpose |
|---|---|---|
| `query_db` | Data | Grouped customer query with churn/fraud/region filters |
| `count_customers` | Data | Count customers matching a single condition |
| `cross_metric_count` | Data | Count customers matching two cross-metric conditions |
| `list_customers` | Data | List individual customers with filters |
| `fetch_customer_data` | Data | Full profile for a specific customer |
| `fetch_transaction_data` | Data | Transaction records for a specific customer |
| `predict_churn` | Prediction | Churn risk score via KServe |
| `predict_fraud` | Prediction | Fraud flag via KServe |
| `predict_qoe` | Prediction | QoE score via KServe |

### AI/ML workflow

Datasets are pulled from Hugging Face and uploaded to S4 (`aws-compatible-storage`), the in-cluster S3-compatible store. Three Kubeflow Pipelines v2 runs (churn, fraud/RAFM, QoE) read from S4, train their models, and write the artifacts back under `models/churn/1/`, `models/fraud/1/`, and `models/starlink-qoe/1/`. KServe loads each artifact at startup; the predictor tool and scoring job call the resulting inference endpoints over HTTP. S4 also supplies the DSPA pipeline server and KServe with credentials through `dashboard-dspa-secret` and `my-data-connection`.

Customer records, transactions, and prediction scores remain in PostgreSQL. S4 holds the datasets and artifacts used to train and serve the models.

## Usage

After completing the [deployment steps](#deploy), find the dashboard endpoint with the OpenShift CLI:

```bash
NAMESPACE="${NAMESPACE:-$(oc project -q)}"
oc get routes -n "$NAMESPACE"
```

Print the dashboard URL:

```bash
oc get route frontend -n "$NAMESPACE" -o jsonpath='https://{.spec.host}{"\n"}'
```

Open the printed URL in your browser to explore the app.

The screenshots below show the deployed app with synthetic data. Metrics and the highest-risk region depend on the dataset and stored predictions; the figures in [Example Queries](#example-queries) are illustrative.

### 1. Review the revenue assurance overview

Start with the summary cards to understand the current exposure:

- **Revenue at risk** shows customer billing exposed to churn.
- **Churn rate** shows the percentage and count of customers predicted to churn.
- **Fraud cases** shows the flagged count and percentage of transactions.
- **Avg QoE score** summarizes quality of experience on a 0–100 scale, where higher scores indicate better service quality.

The **Quality of experience** panel breaks customer sessions into Good, Fair, and Poor groups. The **Highest churn region** card highlights the region to investigate, including its churn rate, customers at risk, average QoE, and season.

![Dashboard overview showing the regional risk summary, revenue and risk metrics, QoE distribution, and North as the highest-churn region](docs/images/dashboard-overview.png)

*Dashboard overview with pre-computed predictions for 10,000 synthetic customers.*

### 2. Compare regional risks

Use the buttons above **Regional risk summary** to change the table's sort order:

- **Churn** places the highest churn rate first (the default view).
- **Fraud** places the highest fraud rate first.
- **QoE** places the lowest quality score first, surfacing regions with the weakest service quality.

Compare customer counts, churn exposure, fraud rates, and QoE across the same rows to choose a follow-up question. In the captured snapshot, North has the highest churn rate and lowest QoE, while East has the highest fraud rate. This gives the analyst separate starting points for network-quality and billing investigations.

### 3. Investigate with the AI assistant

1. Select **Ask AI** in the bottom-right corner to open the **Revenue assistant** panel.
2. Choose a suggested question, such as **Which region needs attention first?**, to fill the question field, or type your own question.
3. Select **Send**. The assistant displays **Thinking…** while it investigates and shows tool activity, such as **Called query_db**, alongside the conversation.
4. Read the response, then ask follow-up questions in the same panel to narrow the investigation. Select **Close assistant** to return to the unobstructed dashboard.

![Dashboard with the Revenue assistant showing database tool calls and a response prioritizing North based on churn, revenue at risk, and QoE](docs/images/dashboard-assistant.png)

*A completed response to "Which region needs attention first?" using the deployed dataset.*

Try follow-ups such as:

- "Why is churn high in North?" — substitute the region highlighted in your dashboard.
- "Are fraud-flagged customers also churning?"
- "Which customers in North are at risk of churning?"

Use the findings to decide which coverage, retention, or billing investigation to pursue. The agent recommends actions; it does not apply retention offers or block accounts. Dashboard metrics come from stored scoring results, while the assistant can query the data and invoke prediction tools on demand.

## Example Queries

The agent reasons across all three models in a single conversation. Below are representative examples using the synthetic dataset included in this quickstart.

### "Why is churn so high in the West?"

The agent queries the database to segment customers by region, runs churn predictions per segment, and finds Region West at 35% churn versus 12% average elsewhere — 870 customers at risk. It then checks QoE for that region (34/100 vs 72 average) and fraud rate (0.5%, same as other regions). Response:

> *"Churn is concentrated in Region West — 35% of customers there are predicted to churn vs 12% average elsewhere. Starlink QoE in that region is 34/100, significantly below the 72 average. Poor satellite coverage correlates with the high churn rate. Fraud rate is normal (~0.5%), so billing issues are not a factor. 870 customers are at risk — consider investigating network coverage or targeting them with retention offers."*

### "Are fraud-flagged customers also churning?"

The agent fetches the fraud-flagged customer list, runs churn predictions on each, and cross-references the results:

> *"66% of fraud-flagged customers are also at churn risk. These may be billing disputes rather than actual fraud — review before blocking accounts."*

### "Which region needs attention first?"

The agent scores QoE across all regions, finds the worst performer, then checks churn and fraud for those customers to build a revenue-at-risk ranking:

> *"Region West is the clear priority: QoE of 34/100 and 35% churn (870 of 2,500 customers), versus 12% churn elsewhere. Only 8 of those 870 also have fraud-flagged transactions, so billing anomalies are not compounding the problem. Poor satellite coverage is the root cause."*

## Requirements

### Hardware

| Node Type | Qty | vCPU | Memory |
|---|---|---|---|
| Control Plane | 3 | 8 | 16 GB |
| Worker | 2 | 16 | 32 GB |
| GPU Worker (optional) | 1 | 16 | 64 GB + 1× NVIDIA A10 / A100 / L40S |

> **GPU note:** A GPU node is required only for local LLM deployment. Without one, use Option A (remote LLM) and point the stack at an existing OpenAI-compatible vLLM endpoint.

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

Before running `deploy-all`, ensure the following are in place:

- **CLI tools and login** — `make`, `helm`, `oc`, `envsubst`, `curl`, and Python 3.12 with virtual-environment support are installed, and `oc whoami` returns your cluster username. The deployment creates `.venv` and installs the dataset-upload and KFP dependencies there.
- **Container images** — pre-built images are published to `quay.io/rh-ai-quickstart`; no extra configuration needed.
- **Storage class** — know the PVC storage class name available on your cluster (e.g. `gp3-csi`); required by the DSPA pipeline server and object storage (`oc get storageclass` lists them).
- **LLM access** — either configure a running endpoint with `LLM_URL`, or provide a GPU node and a HuggingFace token with access to the [Llama model](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) for local deployment. The default `LLM_MODEL_NAME` is `meta-llama/Llama-3.1-8B-Instruct`; set it to the model served by your endpoint when using another supported model.

## Deploy

Clone the repo and change into the repo root:

```bash
git clone https://github.com/rh-ai-quickstart/telco-revenue-assurance-agent.git
cd telco-revenue-assurance-agent
```

The full stack deploys with a single command. Choose the option that matches your LLM setup.

### Required parameters

| Parameter | Required for | Description |
|-----------|-------------|-------------|
| `STORAGE_CLASS` | Always | PVC storage class for DSPA and object storage (e.g. `gp3-csi`) |
| `LLM_URL` | Remote LLM | Base URL of an OpenAI-compatible vLLM endpoint (e.g. `https://my-vllm.apps.cluster/v1`) |
| `LLM_API_TOKEN` | Remote LLM with auth | Bearer token for the remote endpoint — omit if the endpoint has no auth |
| `LLM_MODEL_NAME` | Local LLM | HuggingFace model identifier to serve in-cluster (e.g. `meta-llama/Llama-3.1-8B-Instruct`) |
| `HF_TOKEN` | Local LLM | HuggingFace access token — required to pull gated models |

### Option A — Remote LLM

Use this when you have an existing OpenAI-compatible vLLM endpoint (no GPU node required).

```bash
make deploy-all \
  NAMESPACE=telco-revenue-assurance-agent \
  STORAGE_CLASS=gp3-csi \
  LLM_URL=http://llama-3-1-8b-instruct-vllm-showcase.apps.xxx/v1 \
  LLM_API_TOKEN=<token>
```

> `LLM_API_TOKEN` is optional — omit it if the endpoint requires no authentication.

### Option B — Local LLM (GPU required)

Use this to deploy vLLM in-cluster. A GPU node with at least 1× NVIDIA A10 / A100 / L40S is required.

**HuggingFace prerequisites:**
- Create an access token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
- Request access to the model at [huggingface.co/meta-llama/Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct)

```bash
make deploy-all \
  NAMESPACE=telco-local-llm \
  STORAGE_CLASS=gp3-csi \
  LLM_MODEL_NAME=meta-llama/Llama-3.1-8B-Instruct \
  HF_TOKEN=<token>
```

> `deploy-all` automatically deploys vLLM in-cluster when `LLM_URL` is not set, waits for the `InferenceService` to be `Ready`, then proceeds with the rest of the stack.

### What deploy-all does

The command runs the following phases in order:

| Phase | What happens |
|-------|-------------|
| `deploy-infra` | Creates namespace → deploys object storage (S4) + PostgreSQL → uploads datasets → deploys and verifies the KFP pipeline server (DSPA) |
| `run-pipelines` | Submits three KFP training pipelines (churn, fraud, QoE) and waits for all to succeed |
| `maybe-deploy-llm` | Local LLM only: deploys vLLM via `llm-service` Helm chart and waits for `InferenceService` Ready |
| `deploy-app` | Deploys model serving (KServe), scoring job, data-fetch, predictor, OGX-AI agent, backend, and frontend |

Run serially, with no parallel `-j` setting in the command or `MAKEFLAGS`, so the prerequisite targets execute in order.

### Optional parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `NAMESPACE` | `telco-revenue-assurance-agent` | Target OpenShift project |
| `PGVECTOR_PASSWORD` | `change-me` | Password for the PostgreSQL instance |
| `S3_ACCESS_KEY` | `s4admin` | S3 access key for aws-compatible-storage |
| `S3_SECRET_KEY` | `s4secret` | S3 secret key for aws-compatible-storage |

### Verify and open the dashboard

Check that services and models become ready and the scoring job completes:

```bash
oc get pods,inferenceservices,jobs -n "$NAMESPACE"
oc logs job/scoring -n "$NAMESPACE"
```

Print the dashboard URL:

```bash
oc get route frontend -n "$NAMESPACE" -o jsonpath='https://{.spec.host}{"\n"}'
```

Open the URL and follow the [usage walkthrough](#usage). If scoring has completed but metrics are missing, see [Scoring job completes but dashboard shows no metrics](#scoring-job-completes-but-dashboard-shows-no-metrics).

## Optional components

### Data Generator

The `data-generator/` directory contains a standalone Kubernetes Job that generates synthetic telco customer and transaction records and writes them directly into PostgreSQL. It is **not needed for a standard deployment** — the pre-seeded pgvector image already includes a representative dataset — but it is useful if you want to:

- Regenerate the dataset with a different size or distribution.
- Experiment with custom data schemas or fraud/churn ratios.
- Understand how the synthetic data is produced (Faker-based Python scripts with configurable customer profiles, billing records, and network QoE events).

**How it works:** the Job connects to the PostgreSQL instance, drops and recreates the schema, then inserts a configurable number of customers (`NUM_CUSTOMERS`, default 10,000) along with associated CDRs, invoices, and QoE measurements. Each customer is assigned a randomised risk profile that drives the churn, fraud, and QoE scores the ML pipelines are trained against.

## Delete

The root `undeploy-all` target removes the application, S4, and the local LLM, and deletes **all PVCs in `$NAMESPACE`**. This removes persisted database and S4 data in the default configuration; the namespace itself is retained.

```bash
make undeploy-all NAMESPACE=<namespace>
```

## Troubleshooting

### OGX / OGX agent returns "Internal server error"

The model ID configured in the backend does not match what OGX registered. Verify the available models:

```bash
oc exec -n $NAMESPACE deploy/ogx-ai -- \
  curl -s http://localhost:8321/v1/models | python3 -m json.tool
```

The model ID must match the backend chart's `ogx.modelId`, which deployment sets from `OGX_MODEL_ID`. For the default model, it is `llama-3-1-8b-instruct/meta-llama/Llama-3.1-8B-Instruct`. Check that `LLM_MODEL_NAME` and `LLM_URL` match the model served by your endpoint.

---

### KServe InferenceServices stuck in `Unknown` or `Loading`

This is expected until the ML training pipelines have written model files to S4. Check the model-serving status:

```bash
oc get inferenceservice -n $NAMESPACE
```

If pipelines have completed and models are still not ready, check that the `my-data-connection` secret points to the correct S4 S3 API endpoint and bucket.

---

### S4 connection or bucket errors from DSPA or pipelines

Check the S4 deployment, its S3 service, and the bucket-bootstrap Job in the storage namespace:

```bash
S4_NAMESPACE="${S4_NAMESPACE:-$NAMESPACE}"
oc rollout status deployment/aws-compatible-storage -n "$S4_NAMESPACE" --timeout=120s
oc get service aws-compatible-storage -n "$S4_NAMESPACE"
oc logs job/aws-compatible-storage-bucket-bootstrap -n "$S4_NAMESPACE"
```

The service should expose the S3 API on port 7480, and the bootstrap logs should confirm `Verified: mybucket` (or your configured bucket). If the volume is pending, check the PVC and `STORAGE_CLASS`. If storage is ready but a client fails, check its endpoint and bucket configuration and ensure `dashboard-dspa-secret` and `my-data-connection` use the S4 credentials configured at deployment.

For local dataset-upload failures, retrieve the S3 API route:

```bash
oc get route aws-compatible-storage-api -n "$S4_NAMESPACE" -o jsonpath='https://{.spec.host}{"\n"}'
```

The uploader must be able to reach this URL from your workstation. Use `S3_EXTERNAL_ENDPOINT` to override route discovery; the web UI route is not an S3 API endpoint.

---

### Scoring job completes but dashboard shows no metrics

The backend caches metrics at startup. Restart the backend pod after the scoring job completes:

```bash
oc rollout restart deployment/backend -n $NAMESPACE
```

## Tags

- **Industry:** Telecommunications
- **Product:** Red Hat OpenShift AI
- **Use case:** Revenue assurance, churn prediction, fraud detection, network QoE monitoring

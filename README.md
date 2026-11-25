# Detect Telco Revenue Losses with an AI Agent

Detect churn risk, billing fraud, and network QoE issues using a conversational AI agent backed by three ML models on OpenShift® AI.

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

Before deploying, ensure the following are in place:

- **OCP CLI login** — you are authenticated against the target cluster (`oc whoami` returns your username).
- **Storage class** — know the PVC storage class name available on your cluster (e.g. `gp3-csi`); required by the DSPA pipeline server and object storage.
- **HuggingFace token** _(local LLM only)_ — create an access token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) and request access to [meta-llama/Llama-3.1-8B-Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct).

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

### Optional parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `NAMESPACE` | `telco-revenue-assurance-agent` | Target OpenShift project |
| `PGVECTOR_PASSWORD` | `change-me` | Password for the PostgreSQL instance |
| `S3_ACCESS_KEY` | `s4admin` | S3 access key for aws-compatible-storage |
| `S3_SECRET_KEY` | `s4secret` | S3 secret key for aws-compatible-storage |

## Optional components

### Data Generator

The `data-generator/` directory contains a standalone Kubernetes Job that generates synthetic telco customer and transaction records and writes them directly into PostgreSQL. It is **not needed for a standard deployment** — the pre-seeded pgvector image already includes a representative dataset — but it is useful if you want to:

- Regenerate the dataset with a different size or distribution.
- Experiment with custom data schemas or fraud/churn ratios.
- Understand how the synthetic data is produced (Faker-based Python scripts with configurable customer profiles, billing records, and network QoE events).

**How it works:** the Job connects to the PostgreSQL instance, drops and recreates the schema, then inserts a configurable number of customers (`NUM_CUSTOMERS`, default 10,000) along with associated CDRs, invoices, and QoE measurements. Each customer is assigned a randomised risk profile that drives the churn, fraud, and QoE scores the ML pipelines are trained against.

## Undeploy

```bash
make undeploy-all NAMESPACE=<namespace>
```

This uninstalls all Helm releases, removes KServe resources, and deletes PVCs.

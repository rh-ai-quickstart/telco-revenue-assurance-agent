# Deployment Guide

## Prerequisites

- OpenShift cluster with OpenShift AI
- `helm` 3.x and `oc` CLI authenticated to your cluster
- GPU node optional (vLLM can use a remote endpoint instead)

## Directory Layout

```
deploy/
├── helm/           # Helm charts for every component
│   ├── pgvector/           # PostgreSQL + pgvector
│   ├── ogx-ai/             # LlamaStack OGX orchestration layer
│   ├── frontend/           # React dashboard
│   ├── data-generator/     # Synthetic data generation job
│   ├── tools/
│   │   ├── data-fetch/     # Data Fetch Tool (agent tool service)
│   │   └── predictor/      # Prediction Tools (agent tool service)
├── build/          # OpenShift image build configs (BuildConfig / ImageStream)
├── serving/        # KServe InferenceService + ServingRuntime manifests
└── values/         # Environment-specific value overrides
    ├── ogx/
    └── postgres/
```

## Quick Deploy

```bash
# 1. Deploy infrastructure (PostgreSQL, OGX, tools)
helm install pgvector   ./helm/pgvector   -n <namespace>
helm install ogx-ai     ./helm/ogx-ai     -n <namespace>
helm install data-fetch ./helm/tools/data-fetch -n <namespace>
helm install predictor  ./helm/tools/predictor  -n <namespace>

# 2. Apply KServe models
oc apply -f serving/ -n <namespace>

# 3. Run data generation job
helm install data-gen   ./helm/data-generator  -n <namespace>

# 4. Deploy frontend
helm install frontend   ./helm/frontend   -n <namespace>
```

## Environment Overrides

Showcase-environment values live in `values/`:

```bash
helm install pgvector ./helm/pgvector -f values/postgres/values-showcase.yaml -n <namespace>
helm install ogx-ai   ./helm/ogx-ai   -f values/ogx/values-showcase.yaml      -n <namespace>
```

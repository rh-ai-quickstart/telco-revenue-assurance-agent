# Backend & Agent — Deploy on OpenShift AI

The **backend** is a FastAPI service that exposes the chat API (SSE streaming) and dashboard metrics.  
The **agent** module (imported by the backend) drives the OGX agentic loop and dispatches the 4 prediction tools.

---

## Architecture

```
Frontend → Backend (FastAPI :8000)
                ├─ GET  /api/metrics/dashboard  → PostgreSQL (direct SQL)
                ├─ POST /api/chat/sessions       → creates OGX session
                └─ POST /api/chat/sessions/{id}/turns  → SSE stream
                         └─ AgentRunner
                              ├─ LlamaStack inference API → OGX → vLLM
                              ├─ fetch_customer_data  → data-fetch-tool :8080
                              └─ predict_churn/fraud/qoe → preprocessing :8080
```

---

## Prerequisites

The following must already be running in the `telco-revenue-assurance-agent` namespace before deploying:

| Component | How it was deployed |
|---|---|
| PostgreSQL | `deploy/postgres/values-telco-revenue-assurance-agent.yaml` |
| data-generator | data seeded (one-shot Job) |
| data-fetch-tool | `data_fetch_tool/deploy/telco-revenue-assurance-agent.yaml` |
| preprocessing | `preprocessing/deploy/telco-revenue-assurance-agent.yaml` |
| OGX (`llamastack`) | `helm install ogx ai-architecture-charts/ogx-ai -f deploy/ogx/values-telco-revenue-assurance-agent.yaml` |
| vLLM | `helm install llm ai-architecture-charts/llm-service` |

---

## 1 — Run unit tests locally

No cluster or database needed — all external calls are mocked.

```bash
# From repo root
pip install -r requirements-test.txt

# Run all tests
pytest agent/tests/ backend/tests/ -v

# Run a specific suite
pytest agent/tests/test_tools.py -v
pytest agent/tests/test_runner.py -v
pytest backend/tests/test_metrics.py -v
pytest backend/tests/test_chat.py -v
```

Expected output:

```
agent/tests/test_tools.py::TestFetchCustomerData::test_returns_api_response_on_success PASSED
agent/tests/test_tools.py::TestFetchCustomerData::test_forwards_fields_parameter     PASSED
...
backend/tests/test_chat.py::test_create_session_returns_session_id                   PASSED
backend/tests/test_chat.py::test_chat_turn_streams_sse_events                        PASSED
...
```

### What the tests cover

| Suite | What is tested |
|---|---|
| `test_tools.py` | Tool HTTP calls, correct endpoints, error handling (HTTP + connection errors) |
| `test_runner.py` | Session create/delete, unknown-session guard, text streaming, tool dispatch loop |
| `test_metrics.py` | Dashboard response structure, float rounding, NULL aggregate handling |
| `test_chat.py` | Session creation, 404 on unknown session, SSE event format, session deletion |

---

## 2 — Deploy OGX

OGX must be running before the backend starts (the backend connects to it at startup).

```bash
# Add the ai-architecture-charts Helm repo if not already done
helm repo add ai-architecture-charts https://redhat-ai-services.github.io/ai-architecture-charts/
helm repo update

# Deploy OGX in the telco-revenue-assurance-agent namespace
helm upgrade --install ogx ai-architecture-charts/ogx-ai \
  -f deploy/ogx/values-telco-revenue-assurance-agent.yaml \
  -n telco-revenue-assurance-agent

# Confirm OGX pod is running
oc get pods -n telco-revenue-assurance-agent -l app.kubernetes.io/name=ogx-ai
```

The OGX service will be reachable at `http://llamastack:8321` inside the cluster.  
Adjust `OGX_URL` in `backend/deploy/telco-revenue-assurance-agent.yaml` if the service name differs.

---

## 3 — Build the backend image

The build context is the **repo root** because the backend Dockerfile needs both `agent/` and `backend/`.

```bash
# Create the ImageStream and BuildConfig (one-time)
oc apply -f backend/deploy/build.yaml -n telco-revenue-assurance-agent

# From repo root — send the entire repo as the build context
oc start-build telco-backend --from-dir=. --follow -n telco-revenue-assurance-agent
```

Watch the build logs until you see `Push successful`.

---

## 4 — Deploy the backend

```bash
oc apply -f backend/deploy/telco-revenue-assurance-agent.yaml -n telco-revenue-assurance-agent

# Verify the pod comes up
oc rollout status deployment/backend -n telco-revenue-assurance-agent
```

### Adjust secrets before applying

In `backend/deploy/telco-revenue-assurance-agent.yaml`, the `DATABASE_URL` is read from a secret:

```yaml
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: my-data-connection   # ← replace with your PostgreSQL secret name
      key: DATABASE_URL
```

Check your secret name with:

```bash
oc get secrets -n telco-revenue-assurance-agent | grep -i postgres
```

---

## 5 — Verify deployment

```bash
# Get the Route URL
BACKEND_URL=$(oc get route backend -n telco-revenue-assurance-agent -o jsonpath='{.spec.host}')

# Health check
curl -s https://$BACKEND_URL/health
# → {"status":"ok"}

# Dashboard metrics
curl -s https://$BACKEND_URL/api/metrics/dashboard | python3 -m json.tool

# Create a chat session
SESSION=$(curl -s -X POST https://$BACKEND_URL/api/chat/sessions | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session: $SESSION"

# Send a message and stream the response
curl -N -s -X POST https://$BACKEND_URL/api/chat/sessions/$SESSION/turns \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the overall churn rate and which region has the worst QoE?"}'
```

---

## 6 — Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | (required) | PostgreSQL connection URL |
| `OGX_URL` | `http://llamastack:8321` | LlamaStack/OGX service URL |
| `OGX_MODEL_ID` | `vllm-inference/llama-3.1-8b-instruct` | Model ID registered in OGX |
| `PREPROCESSING_URL` | `http://preprocessing:8080` | Prediction tools service |
| `DATA_FETCH_URL` | `http://preprocessing-data-fetch:8080` | Data fetch tool service |
| `TOOL_TIMEOUT` | `30` | HTTP timeout (seconds) for tool calls |

---

## Troubleshooting

**Backend pod CrashLoopBackOff**
```bash
oc logs deployment/backend -n telco-revenue-assurance-agent
```
Most likely cause: `DATABASE_URL` secret missing or OGX unreachable at startup.

**Tool calls return `{"error": "..."}`**
```bash
# Check preprocessing is healthy
oc exec deployment/backend -n telco-revenue-assurance-agent -- curl -s http://preprocessing:8080/health

# Check data-fetch-tool is healthy
oc exec deployment/backend -n telco-revenue-assurance-agent -- curl -s http://preprocessing-data-fetch:8080/health
```

**OGX connection refused**
```bash
oc get svc -n telco-revenue-assurance-agent | grep llama
# Confirm the service name matches OGX_URL in the ConfigMap
```

# Predictor service and Python tools

This package exposes customer-ID prediction tools and an HTTP service. It fetches
current source records through the data fetch service, preprocesses them, and
calls KServe v2 model endpoints. PostgreSQL access stays in `data_fetch_tool/`;
this package does not import that service or require database drivers.

The package, container image, Deployment, and Service are named `predictor`.
Python callers import from `predictor`; in-cluster HTTP callers use
`http://predictor.telco-revenue-assurance-agent.svc:8080` in the telco-revenue-assurance-agent namespace.

## Run the prediction service

From the repository root, install the dependencies and provide the environment
variables described below before starting the service:

```sh
python -m pip install -r predictor/requirements.txt
uvicorn predictor.main:app --host 0.0.0.0 --port 8080 --workers 1
```

Use `predictor/.env.example` as a configuration template. Environment files
are not loaded automatically by the Python package.

The service provides:

- `POST /predict/{model}` with `{"customer_id": "CUST00001"}`.
- `POST /predict/{model}/batch` with `{"customer_ids": ["CUST00001", "CUST00002"]}`.
- `GET /health` for process liveness.
- `GET /stats` for aggregate cache and inference statistics.
- `GET /docs` for the generated API reference.

`model` must be `churn`, `fraud`, or `qoe`. IDs must be nonblank strings; numeric
IDs are rejected. Batch requests accept 1–1,000 IDs and return one result per
input in the same order, including duplicates. Unknown request fields are
rejected. A failed batch returns an error for the request rather than partial
results.

```sh
curl --fail-with-body http://localhost:8080/predict/churn \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"CUST00001"}'

curl --fail-with-body http://localhost:8080/predict/fraud/batch \
  -H 'Content-Type: application/json' \
  -d '{"customer_ids":["CUST00001","CUST00002","CUST00001"]}'

curl --fail-with-body http://localhost:8080/health
curl --fail-with-body http://localhost:8080/stats
```

A churn response has the following shape:

```json
{"label":1,"verdict":"Will Churn","churn_probability":0.8}
```

A fraud batch response has the following shape:

```json
{
  "model": "fraud",
  "predictions": [
    {"customer_id": "CUST00001", "label": 0, "verdict": "Non-Fraud"},
    {"customer_id": "CUST00002", "label": 1, "verdict": "Fraud"},
    {"customer_id": "CUST00001", "label": 0, "verdict": "Non-Fraud"}
  ],
  "count": 3
}
```

Single fraud results contain `label` and `verdict`; QoE results contain
`qoe_class` and `verdict`. Default QoE classes are `0/Poor` below 40, `1/Fair`
from 40 to below 70, and `2/Good` at 70 or above.

Errors use FastAPI's `{"detail": ...}` format:

- `404`: required customer or transaction data was not found.
- `422`: invalid request/model or missing/invalid preprocessing features.
- `502`: data fetch or inference dependency failed or returned an invalid response.
- `500`: other configuration or infrastructure failures.

Preprocessing errors name the missing or invalid fields. Data fetch, inference,
and infrastructure exception text is not returned to callers. `/health` returns
`{"status":"ok"}` without checking model readiness, credentials, source data,
or S3 artifacts. `/stats` reports process-local aggregate counters and cache
sizes; it excludes customer IDs, feature values, cached predictions, endpoint
URLs, and credentials. Its counters reset when the process restarts. Neither
route requires the upstream services to be available.

## Configuration

- `DATA_FETCH_URL`: data fetch service base URL, including any gateway path
  prefix. The client appends `/customers` or `/transactions`.
- `DATA_FETCH_TIMEOUT`: positive seconds per data fetch request, default `30`.
- `DATA_FETCH_TOKEN`: optional gateway bearer token, separate from model-serving
  credentials.
- `CHURN_INFER_URL`, `FRAUD_INFER_URL`, `QOE_INFER_URL`: complete KServe v2
  inference URLs for the models in use. No endpoint URLs are built into the code.
- `INFER_TOKEN`: optional model-serving bearer token.
- `INFER_TOKEN_FILE`: optional mounted bearer-token file. When configured, its
  current contents take precedence over `INFER_TOKEN`, allowing a projected
  service-account token to rotate without restarting the process.
- `INFER_TIMEOUT`: positive seconds per inference request, default `30`.
- `SSL_CERT_FILE`: optional path to a trusted PEM CA bundle for HTTPS calls to
  data fetch and inference services. Mount the bundle at runtime; TLS verification
  remains enabled.
- `CACHE_TTL`: cached-result lifetime in seconds, default `300`; `0` disables
  reuse of completed predictions.
- `CACHE_MAXSIZE`: maximum cached feature vectors per model, default `512`.
- `BATCH_MAX_SIZE`: maximum model rows per inference batch, from 1 to 1,000,
  default `32`.
- `BATCH_WAIT_MS`: short collection window for combining pending model work,
  from 0 to 1,000 milliseconds, default `10`.
- `QOE_POOR_THRESHOLD` and `QOE_GOOD_THRESHOLD`: score boundaries, defaults
  `40` and `70`; require `0 <= poor < good <= 100`.

QoE also requires the fitted training artifacts in S3: set `AWS_S3_ENDPOINT`,
`S3_BUCKET`, and `S3_MODEL_PATH`. The loader reads
`<prefix>/artifacts/scaler.joblib` and
`<prefix>/artifacts/label_encoders.joblib` through the standard AWS credential
chain. If S3 uses a private certificate authority, set `AWS_CA_BUNDLE` to its
trusted PEM bundle. Use artifacts and scikit-learn versions compatible with the
deployed model. No credentials or artifacts are included in the container image.

The prediction service itself does not authenticate inbound HTTP requests.
Expose it through the platform's chosen internal networking or authenticated
gateway. The optional bearer-token settings authenticate its outbound calls.

## Batching and caching

Single and batch HTTP requests use the same batching implementation as the
Python tools. Bulk requests fetch source data together and send uncached feature
vectors as model tensors with a batch dimension. Concurrent calls can share
model batches during `BATCH_WAIT_MS`; each batch is capped by `BATCH_MAX_SIZE`.
All callers receive results matched back to their original input order.

Churn, fraud, and QoE have separate TTL caches. Cache keys are SHA-256 hashes of
the JSON-serialized processed features, so customers with the same model inputs
can reuse a prediction. Every request fetches current source records before
checking the result cache; a fetch failure does not serve a previous result.

Caches, pending batches, and counters belong to one Python process. Replicas
have independent caches and batching queues. The Docker image runs one Uvicorn
worker so each replica has one set of queues; additional workers would create
separate caches within the same replica. Shutdown drains/stops prediction
workers through the application's lifespan hook.

Each actual downstream KServe request writes `kserve_call_started` and
`kserve_call_completed` or `kserve_call_failed` entries to the container log.
The entries identify the model, upstream host, batch row count, outcome/status,
and duration, without customer IDs, input features, tokens, or response data.
Cache hits do not make an inference request and therefore do not create these
entries. In OpenShift, view them with
`oc logs -n telco-revenue-assurance-agent -f deployment/predictor`.

## Python callers

The single-customer tools retain their callable interface:

```python
from predictor import predict_churn, predict_fraud, predict_qoe
from predictor.predictors import predict_batch

result = predict_churn("CUST00001")
results = predict_batch("fraud", ["CUST00001", "CUST00002", "CUST00001"])
```

`predict_batch` returns a list of domain result dictionaries in input order.
The HTTP batch route adds the customer IDs and response envelope.

Other callers can use the synchronous data client directly:

```python
from predictor.client import fetch_customer_data, fetch_latest_transaction_data

customers = fetch_customer_data(
    ["CUST00001", "CUST00002"], fields=["total_bill_amount", "payment_status"]
)
transactions = fetch_latest_transaction_data(
    ["CUST00001", "CUST00002"], fields=["cost", "plan_type"]
)
```

The data client deduplicates IDs, splits lists into requests of at most 1,000,
and includes `customer_id` in explicit field selections. Missing IDs are omitted
from raw data-client results; the prediction API reports missing data as an
error. Latest-transaction fetching also includes `transaction_id` and selects
the highest ID locally. This requires fetching all transactions because the
existing service's `limit_per_customer` selects the lowest transaction IDs.
The generic `fetch_transaction_data` retains that service behavior.

`predictor/customer_data.py` and its custom-fetcher API have been removed;
prediction callers now use `predictor/client.py`.

## Container image

The build context is the `predictor/` directory:

```sh
podman build -f predictor/Dockerfile -t predictor:local predictor
podman run --rm --env-file predictor/.env -p 8080:8080 predictor:local
```

The Python 3.12 slim image listens on port 8080, runs as a non-root user, and
supports an OpenShift-assigned UID in group 0. The build context allowlist
includes Python source and requirements only; environment files, credentials,
artifacts, and bytecode are excluded. Supply tokens and CA bundles through
runtime environment variables or mounted files.

The separate data fetch service can be started with
`uvicorn data_fetch_tool.main:app --host 0.0.0.0 --port 8081` after installing
`data_fetch_tool/requirements.txt` and configuring its `DATABASE_URL`. Its
Dockerfile and Helm chart remain under `data_fetch_tool/`.

`predictor/deploy/telco-revenue-assurance-agent.yaml` connects to the active Helm-deployed data
fetch service at `http://data-fetch-tool-data-fetch-tool.telco-revenue-assurance-agent.svc:8080`.
The registry-free validation renderer overrides this URL to use its separate
`preprocessing-data-fetch` fixture service and isolated validation database.

The [predictor Helm chart](helm/predictor/README.md) is the deployment path for
the prediction service. Its [values.yaml](helm/predictor/values.yaml) labels all
environment variables, and [values-telco-revenue-assurance-agent.yaml](helm/predictor/values-telco-revenue-assurance-agent.yaml)
configures the active data-fetch and model endpoints, authentication, and CA
trust. The chart guide includes installation and adoption of the existing
manually deployed resources.

Build an image and install a new release in telco-revenue-assurance-agent:

```sh
oc apply -f predictor/deploy/build.yaml
oc start-build predictor -n telco-revenue-assurance-agent --from-dir=predictor --follow --wait
helm upgrade --install predictor predictor/helm/predictor -n telco-revenue-assurance-agent \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml --wait --timeout 5m
oc rollout status -n telco-revenue-assurance-agent deployment/predictor --timeout=300s
```

For the existing manual deployment, follow the chart's adoption instructions
before the first Helm installation. The raw `deploy/telco-revenue-assurance-agent.yaml` remains the
input for the isolated validation renderer; do not apply it over a Helm-managed
deployment.

## Current source-data constraints

The customer table is a snapshot; the data service does not select billing
cycles from a history table. Its producer must supply the intended current
cycle for latest-cycle churn predictions. The demo generator currently samples
cycles.

The current data service allowlist and generated schema omit
`visible_satellites`, `serving_satellites`, and `signal_loss_db`. QoE returns a
preprocessing error before loading artifacts or calling the model when these
fields are missing. Supplying them requires a service/schema update. Deploying
this prediction service does not fill that source-data gap.

## Verification

Install the preprocessing requirements and the HTTP test client, then run:

```sh
python -m pip install httpx
python -m unittest discover -s tests/predictor -v
```

Tests use mock HTTP services and fitted or mocked artifacts. They require no
database drivers, live database, inference service, or S3 access.

For live checks on the current cluster, use the
[registry-free validation guide](../tests/predictor/live/README.md).
It deploys the checked-out services on `python:3.12-slim` with mounted source
and an isolated fixture database. The packaged-image manifests under `deploy/`
require an enabled image registry. The guide distinguishes real HTTP/source
coverage from the explicit QoE verification fixtures needed for missing fields.

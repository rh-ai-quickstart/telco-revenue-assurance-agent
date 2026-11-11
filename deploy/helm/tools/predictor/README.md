# Predictor Helm chart

Deploys the `predictor` HTTP API, its Service and configuration, and optionally
a ServiceAccount, model-access RoleBindings, and an OpenShift service CA bundle.
The image, data-fetch service, KServe model servers, S3 artifacts, and credential
Secrets must already exist. This chart does not create those dependencies.

Start with [values.yaml](values.yaml): every application setting is labelled
with its environment variable and whether it is required or optional.
[values-telco-revenue-assurance-agent.yaml](values-telco-revenue-assurance-agent.yaml) supplies the verified telco-revenue-assurance-agent
endpoints, service-account authentication, CA trust, and Secret references.
The generic defaults intentionally leave deployment-specific URLs and the image
unset; supply them before linting or installing.

## Install in telco-revenue-assurance-agent

Run from the repository root on the jumphost. Select the working cluster
explicitly; its default kubeconfig may point at a different cluster:

```sh
export KUBECONFIG=/root/kubeconfigs/ptnr2/kubeconfig
oc get namespace telco-revenue-assurance-agent

# If you need a new image, build it before the Helm install/upgrade.
oc apply -f predictor/deploy/build.yaml
oc start-build predictor -n telco-revenue-assurance-agent --from-dir=predictor --follow --wait

helm lint predictor/helm/predictor \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml --strict
helm template predictor predictor/helm/predictor -n telco-revenue-assurance-agent \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml > /tmp/predictor-rendered.yaml
oc apply -n telco-revenue-assurance-agent --dry-run=server -f /tmp/predictor-rendered.yaml
```

For a **new installation** with no existing predictor resources:

```sh
helm upgrade --install predictor predictor/helm/predictor -n telco-revenue-assurance-agent \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml --wait --timeout 5m
```

For the **existing manually deployed telco-revenue-assurance-agent predictor**, the first Helm
installation must adopt its resources. The jumphost's Helm 3.22.1 supports
`--take-ownership`; use that flag only for this initial migration:

```sh
# Keep a private snapshot of the resources being adopted.
umask 077
oc get -n telco-revenue-assurance-agent deployment/predictor service/predictor \
  configmap/predictor-config configmap/predictor-service-ca \
  serviceaccount/predictor rolebinding/predictor-churn \
  rolebinding/predictor-fraud -o yaml > /tmp/predictor-before-helm.yaml

helm upgrade --install predictor predictor/helm/predictor -n telco-revenue-assurance-agent \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml \
  --take-ownership --wait --timeout 5m

oc rollout status -n telco-revenue-assurance-agent deployment/predictor --timeout=300s
```

The telco-revenue-assurance-agent override preserves the resource names, `app: predictor` selector,
Service port, and caller URL `http://predictor.telco-revenue-assurance-agent.svc:8080`. It manages
only the prediction service and its supporting resources; the separate
`preprocessing-data-fetch` validation deployment is outside the chart.
After adoption, update the service with Helm, rather than applying
`predictor/deploy/telco-revenue-assurance-agent.yaml` alongside it. Uninstalling the Helm release
also removes its adopted resources.

For later updates, use the same upgrade command without `--take-ownership`.
Set `image.digest` or use a unique `image.tag` for each image update; rebuilding
the same `latest` tag alone does not change the Deployment and trigger a rollout.
Changes to chart-managed configuration trigger a rollout through its checksum.
External Secret changes require `oc rollout restart -n telco-revenue-assurance-agent deployment/predictor`
to refresh environment variables. Mounted service-account tokens rotate without
a restart and are read for each prediction.

## KServe call logs

The API container writes Uvicorn and application logs to stderr, which OpenShift
collects as pod logs. Each actual outbound KServe request emits a
`kserve_call_started` line and a `kserve_call_completed` or
`kserve_call_failed` line. Entries include the model, upstream host, tensor row
count, outcome or HTTP status, and request duration. They exclude customer IDs,
features, bearer tokens, and response bodies. Cache hits and preprocessing or
data-fetch failures do not call KServe and therefore produce no KServe-call log.

Follow the live log stream with:

```sh
oc logs -n telco-revenue-assurance-agent -f deployment/predictor --since=5m
```

Filter to inference calls with:

```sh
oc logs -n telco-revenue-assurance-agent deployment/predictor --since=30m \
  | grep 'kserve_call_'
```

## Required environment-specific values

- `image.repository`: image built from `predictor/Dockerfile`. Set `image.tag`
  (default `latest`) or `image.digest` (takes precedence). `imagePullSecrets`
  accepts existing registry Secret names as `[{name: registry-credentials}]`.
- `dataFetch.url` → `DATA_FETCH_URL`: the data-fetch base URL. The Python
  client appends `/customers` or `/transactions` to it. In telco-revenue-assurance-agent this is
  `http://data-fetch-tool-data-fetch-tool.telco-revenue-assurance-agent.svc:8080`.
- `models.churn.url` → `CHURN_INFER_URL`, `models.fraud.url` → `FRAUD_INFER_URL`,
  and `models.qoe.url` → `QOE_INFER_URL`: complete KServe v2 inference URLs,
  including the served model name, port, and `/infer`. Configure at least one.
  An empty URL leaves that model unconfigured; its HTTP route still exists.

For a minimal churn-only deployment on another cluster, create a values file:

```yaml
image:
  repository: registry.example.com/team/predictor
  tag: "1.0.0"
dataFetch:
  url: http://data-fetch:8080
models:
  churn:
    url: http://churn-predictor:8080/v2/models/churn/infer
```

Then install with `helm upgrade --install predictor predictor/helm/predictor
-n YOUR_NAMESPACE -f your-values.yaml --wait --timeout 5m`.

## Authentication and TLS

All referenced Secrets, ConfigMaps, and Roles must exist in the release's
namespace. Credentials are referenced with `secretKeyRef`; do not store token
or access-key values in Helm values files.

- `dataFetch.auth.existingSecret` and `.key` → `DATA_FETCH_TOKEN`: optional
  bearer token for a data-fetch gateway. The default key is `token`.
- `inference.auth.mode: none` sends no model-serving token (default).
- `inference.auth.mode: serviceAccount` → `INFER_TOKEN_FILE`: uses the rotating
  token at `/var/run/secrets/kubernetes.io/serviceaccount/token`. Set
  `serviceAccount.automountServiceAccountToken: true`. For the telco-revenue-assurance-agent model
  proxies, `rbac.modelRoles.churn` and `.fraud` name the existing model view
  Roles; the chart binds them to the predictor ServiceAccount. `.qoe` is
  available for a protected QoE proxy. The configured token is shared across
  all model endpoints; use endpoints you trust.
- `inference.auth.mode: secret` → `INFER_TOKEN`: set
  `inference.auth.existingSecret` and `.key` (default `token`). Use this when
  the model gateway needs a separate bearer token.
- `serviceAccount.create: false` uses an existing account; set
  `serviceAccount.name`. For a newly created account, `.name` defaults to the
  chart's resource name, and `.annotations` can configure workload identity.
- `tls.http.openShiftServiceCA: true` → `SSL_CERT_FILE`: creates an
  OpenShift-injected CA ConfigMap and mounts it at
  `/etc/predictor-ca/service-ca.crt`. This enables certificate verification
  for the telco-revenue-assurance-agent churn/fraud HTTPS proxies on port 8443.
- Alternatively, `tls.http.existingConfigMap` and `.key` (default
  `service-ca.crt`) mount your own PEM trust bundle at that same path. Choose
  only one HTTP CA source. The bundle is used by both data-fetch and inference
  HTTPS calls; include every CA they require.
- `tls.s3.existingConfigMap` and `.key` (default `ca-bundle.pem`) →
  `AWS_CA_BUNDLE`: optional separate S3 CA bundle, mounted at
  `/etc/predictor-s3-ca/ca-bundle.pem`.

## QoE artifact configuration

When `models.qoe.url` is configured, also provide:

- `qoe.artifacts.endpoint` → `AWS_S3_ENDPOINT`: S3/MinIO endpoint URL.
- `qoe.artifacts.prefix` → `S3_MODEL_PATH`: training model prefix, e.g.
  `models/starlink-qoe`. It must contain `artifacts/scaler.joblib` and
  `artifacts/label_encoders.joblib`, compatible with the deployed model.
- `qoe.artifacts.bucket` → `S3_BUCKET`, **or**
  `qoe.artifacts.bucketSecret.name` and `.key` (default `AWS_S3_BUCKET`).
- `qoe.artifacts.credentialsSecret.name`: existing Secret used to populate
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Their Secret keys are
  configurable through `.accessKeyIdKey` and `.secretAccessKeyKey`; defaults
  match the environment names. Set `.sessionTokenKey` for temporary credentials
  (`AWS_SESSION_TOKEN`). Leave the name blank to use the AWS credential chain,
  such as workload identity, when available.
- `qoe.artifacts.region` → `AWS_DEFAULT_REGION`: optional AWS region.

Showcase uses `my-data-connection` for the bucket and access keys; its
`AWS_S3_ENDPOINT` is deliberately not imported. The endpoint comes from
`qoe.artifacts.endpoint`, pointing to the reachable MinIO Service.

## Runtime settings

- `dataFetch.timeoutSeconds` → `DATA_FETCH_TIMEOUT`: default `30`, positive seconds.
- `inference.timeoutSeconds` → `INFER_TIMEOUT`: default `30`, positive seconds.
- `cache.ttlSeconds` → `CACHE_TTL`: default `300` seconds; `0` disables completed-result caching.
- `cache.maxSize` → `CACHE_MAXSIZE`: default `512`, positive entries per model.
- `batching.maxSize` → `BATCH_MAX_SIZE`: default `32`, integer from `1` to `1000`.
- `batching.waitMs` → `BATCH_WAIT_MS`: default `10`, from `0` to `1000` milliseconds.
- `qoe.poorThreshold` → `QOE_POOR_THRESHOLD`: default `40`.
- `qoe.goodThreshold` → `QOE_GOOD_THRESHOLD`: default `70`; requires `0 <= poor < good <= 100`.

`replicaCount` defaults to `1`. Each pod runs one Uvicorn worker and owns its
cache, batching queues, and statistics. `service.port` defaults to `8080`;
the container always listens on `8080`. `service.type` defaults to `ClusterIP`.
Resource requests/limits, container `securityContext`, `nodeSelector`,
`tolerations`, and `affinity` are configurable in values. The default security
context allows an OpenShift-assigned UID.

The JSON schema and template checks reject missing required settings, misspelled
value keys, invalid numeric ranges, inconsistent token/CA settings, and
incomplete QoE artifact configuration. They cannot verify live URLs, Secret
contents, permissions, or source-data completeness.

## Check the running service

```sh
oc -n telco-revenue-assurance-agent get deployment/predictor service/predictor
oc -n telco-revenue-assurance-agent logs deployment/predictor --tail=100
oc -n telco-revenue-assurance-agent port-forward service/predictor 18080:8080
```

In a second terminal, use a customer ID present in the active data service:

```sh
curl --fail-with-body http://127.0.0.1:18080/health
curl --fail-with-body http://127.0.0.1:18080/stats
curl --fail-with-body http://127.0.0.1:18080/predict/churn \
  -H 'Content-Type: application/json' -d '{"customer_id":"YOUR_CUSTOMER_ID"}'
curl --fail-with-body http://127.0.0.1:18080/predict/fraud \
  -H 'Content-Type: application/json' -d '{"customer_id":"YOUR_CUSTOMER_ID"}'
curl --fail-with-body http://127.0.0.1:18080/stats
```

`/health` verifies process liveness only. A successful uncached prediction and
an increase in that pod's inference counters exercise the downstream model
path. Cache hits need not increase inference counters. A `502` still requires
checking the relevant upstream URL, CA, token/RoleBinding, and upstream response.

The active data service currently omits `visible_satellites`,
`serving_satellites`, and `signal_loss_db`; QoE requests using those records
return `422` before inference. The Helm chart does not repair missing source
fields. See the [service README](../../README.md#current-source-data-constraints)
and [live validation guide](../../../tests/predictor/live/README.md) for details.

## Chart checks

```sh
helm lint predictor/helm/predictor \
  -f predictor/helm/predictor/values-telco-revenue-assurance-agent.yaml --strict
python -m pip install pyyaml
python -m unittest discover -s tests/predictor -p test_helm_chart.py -v
```

The chart tests need Helm on `PATH` (or `HELM_BIN=/path/to/helm`) and PyYAML.
They validate the telco-revenue-assurance-agent migration contract, optional credentials/CA mounts,
configuration-triggered rollouts, and rejection of invalid configurations.

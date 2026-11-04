# OpenShift model serving

The application chart serves the three trained models through authenticated KServe
services in the release namespace. Training infrastructure is described in
[`../training/README.md`](../training/README.md). Successful training and populated
feature artifacts are prerequisites; rendering this chart does not create either.

## Artifact connections

The default data-connection mode references the connection Secret and model prefix
through `opendatahub.io/connections` and `opendatahub.io/connection-path`.
The chart also supplies `storage.key` and `storage.path`, which select an entry in
the operator-managed `storage-config` Secret. OpenShift AI 3.4's connection webhook
must recognize the connection to retain that location on updates. Each referenced
connection Secret needs
labels `opendatahub.io/managed: "true"` and `opendatahub.io/dashboard: "true"`,
annotations `opendatahub.io/connection-type-protocol: s3` and
`opendatahub.io/connection-type: s3`, and these data keys:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_S3_ENDPOINT`
- `AWS_S3_BUCKET`
- `AWS_DEFAULT_REGION`

Use separate credentials with verified read-only storage access for serving. Test
allowed list/read and denied write/delete on a disposable object before using the
credentials. Training must retain write access. Connection creation and permission
verification are deployment prerequisites; the application chart references these
Secrets and does not generate their credential values.

| Model | Connection Secret | Prefix within the bucket |
| --- | --- | --- |
| Churn | `telco-churn-model-storage` | `models/churn/1` |
| Fraud | `telco-fraud-model-storage` | `models/fraud` |
| QoE | `telco-qoe-model-storage` | `models/starlink-qoe` |

Submit the unchanged fraud pipeline with
`s3_model_key=models/fraud/1/model.onnx`. Its isolated prefix prevents MLServer from
discovering churn's `model-settings.json`. Both ONNX services explicitly load
`/mnt/models/1/model.onnx`; churn retains its generated model settings and native
`model.bst` file.

The protocol annotation is required by the OpenShift AI connections API. The
legacy annotation also supports the model controller's aggregate `storage-config`
Secret. Keeping only the legacy annotation is insufficient: the connection
webhook can remove the model storage fields on update without injecting a new
location. Verify that a server-side dry-run response retains the expected storage
location, not merely that the request succeeds. See the
[OpenShift AI 3.4 connections API](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.4/html/working_on_projects/using-connections_projects).

The chart omits `serving.kserve.io/storageSecretName` in data-connection mode so
KServe can read the aggregate Secret. An explicit `modelServing.MODEL.uri` switches
to direct-secret mode and emits that annotation. Direct-secret mode additionally
requires KServe's S3 endpoint/region/HTTPS annotations or `S3_*` keys; the
data-connection `AWS_S3_ENDPOINT` field alone does not configure this mode.

The `telco-model-feature-artifacts` PVC must contain verified `churn.json`,
`fraud.json`, `qoe.json`, `scaler.joblib`, and `label_encoders.joblib`. Derive these
from the matching trained artifacts and observed model response metadata. Keep
the dataset revision, pipeline run ID, hashes, and feature order with the
deployment evidence. Never infer a model version solely from an S3 directory.
The optional [artifact loader chart](artifacts/README.md) verifies these files and
publishes them together through the PVC's `current` directory. When using it, set
`predictionTools.modelFeatureManifestDir` to `/etc/telco-models/current` and keep
`predictionTools.featureArtifactsMountPath` at `/etc/telco-models`.

Set each `modelServing.MODEL.modelVersion` in deployment values to the immutable
identity assigned to that trained artifact, such as its recorded pipeline run ID.
The chart passes this value as `MLSERVER_MODEL_VERSION`; it does not edit the
model binary or imported pipeline. The default is empty because the chart cannot
know which training run supplies the artifacts. Verify the actual inference
response's `model_version` before writing the matching feature manifest. Explicit
versions inside a supplied `model-settings.json` can take precedence over an
environment default. MLServer's metadata `versions` list describes dependencies
and is not sufficient proof of the inference response version.
[MLServer version setting](https://github.com/opendatahub-io/MLServer/blob/f8b8d3eb6a130105122c53cf03cdd41cae26e6ea/mlserver/settings.py)
and [ONNX response construction](https://github.com/opendatahub-io/MLServer/blob/f8b8d3eb6a130105122c53cf03cdd41cae26e6ea/runtimes/onnx/mlserver_onnx/onnx.py)
define this behavior; verify it against the deployed runtime image as well.

## Inference authentication and TLS

Default URLs are generated from the release namespace:
`https://MODEL-predictor.NAMESPACE.svc:8443/v2/models/MODEL/infer`.
The `.svc` DNS name matches the OpenShift service certificate. Prediction-tools
mounts `openshift-service-ca.crt` and sets `SSL_CERT_FILE` to that certificate
bundle; TLS verification stays enabled. The ConfigMap must already contain
`service-ca.crt`. Overrides can name another ConfigMap/key, for example a combined
trust bundle when explicit model URLs use another certificate authority.

The chart grants the prediction-tools ServiceAccount only `get` on the three named
InferenceServices in the release namespace. The model ingress policies allow
that caller on port 8443. Runtime readiness probes still use the original runtime
port; they do not establish authorization or successful inference.

Standard KServe predictors reject a nonempty `spec.predictor.model.name`, so the
operational template omits it. KServe's MLServer reconciliation injects the model
name from the InferenceService name; churn's generated model settings also carry
its name. The imported source snapshots remain unchanged.

By default, `predictionTools.modelAuth.create: true` declares three
`kubernetes.io/service-account-token` Secrets associated with the prediction-tools
ServiceAccount. The control plane fills their `token` fields. Helm contains no
token values, and the existing application reads each token through a Secret
environment reference. Automatic API-token mounting stays disabled.

These manually requested tokens are long-lived and do not rotate automatically.
This preserves the application's current environment-token interface. Projected,
short-lived tokens are the preferred general Kubernetes approach, but using them
correctly here requires the application to reread refreshed token files. A
one-time TokenRequest placed in an environment variable would expire silently.
[Kubernetes documents both token mechanisms](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#manually-create-a-long-lived-api-token-for-a-serviceaccount).

For deliberate rotation, choose new model-auth Secret names in the deployment
values, redeploy with the same reviewed application command and values, wait for
the controller to populate the new token fields, and verify the prediction-tools
rollout and authenticated inference. Helm removes the previous managed Secrets,
revoking those tokens; a brief interruption can occur during rotation. If changing
the contents of existing externally managed Secrets instead, restart
prediction-tools so it rereads the environment. Deleting the token Secrets or
removing the model-inference RoleBinding revokes access. Do not print token values
to inspect them. Set `modelAuth.create: false` when supplying external Secrets and
manage their renewal and pod restart explicitly.

## Verify and deploy

Run local configuration checks from the application repository:

```bash
helm -n telco-revenue-assurance-agent lint chart/
make verify-model-assets UV='uv --cache-dir /tmp/telco-training-uv-cache'
```

The verifier checks all ten imported asset hashes, preserved runtime behavior,
the approved OpenShift storage changes, service-certificate DNS, scoped RBAC,
token references, and model ingress. It does not contact the cluster.

Deploy using the reviewed application command saved in the current debug
analysis, after its runtime values and prerequisites are verified. Keep the
selected external/in-cluster LLM option and existing application settings. Do not
use the old example overlay containing placeholder model URIs. After deployment,
require all three InferenceServices Ready, successful readonly artifact downloads,
real predictions, denied unauthenticated inference, and the complete application
test plan. A successful Helm render alone is not deployment verification.

Source references: [KServe connection lookup](https://github.com/opendatahub-io/kserve/blob/a2efb39e449472666335284481ab1f37c6df474a/pkg/credentials/service_account_credentials.go),
[ODH connection aggregation](https://github.com/opendatahub-io/odh-model-controller/blob/d0bf78690a5c102956458d64f24581928c70488d/internal/controller/core/secret_controller.go),
[OpenShift service certificates](https://docs.redhat.com/en/documentation/openshift_container_platform/4.11/html/security_and_compliance/configuring-certificates),
and [HTTPX CA configuration](https://www.python-httpx.org/environment_variables/#ssl_cert_file).

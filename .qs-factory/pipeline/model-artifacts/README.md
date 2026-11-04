# Trained model artifact provenance

All three approved training runs completed successfully. This directory retains
small, sanitized evidence and manifests; datasets, fitted binaries, model pickles
and credential values are excluded. Absolute local paths in inspection records
identify the temporary verification cache. Their S3 URIs and SHA256 hashes identify
the durable source artifacts.

The five-file feature bundle is recorded in `feature-bundle-manifest.json` and was
published to `s3://mybucket/models/features/5a958a26803249104c583dec1b695adceab139cf8a3738419691924f4e65929b/`.
Its three model versions are explicitly assigned successful run IDs. Every file
passed full remote SHA256/size readback and local application FeatureRegistry
validation. The real scaler and label encoders match the QoE pipeline outputs.

`serving-artifact-read-proof.json` records successful GetObject checks using the
separate read-only `telco-qoe-model-storage` connection for all five feature files,
the derived churn model/settings, and fraud/QoE ONNX. The earlier independent
write/delete-denial proof remains in `../model-storage-permissions.json`.

The derived churn serving prefix is
`s3://mybucket/models/churn-serving/1ea4bc0e-f1c7-44af-8d80-58dc53eaa805/`.
Its `model.bst` is byte-identical to the approved pipeline output. Separate settings
correct the observed native output to FP64 `[batch,1]` and assign the deployment
version. Original pipeline output/settings and `models/churn/1/` remain intact.

`trained-artifact-metadata.json`, `trained-runtime-probe.json` and
`runtime-responses/` describe local execution of actual trained artifacts through
the exact pinned ODH MLServer source. They do not claim a live model endpoint has
been verified. The runtime outputs use alias `predict`, include NumPy codec
parameters, and emit churn FP64 probabilities, fraud INT64 labels and QoE FP32
scores. All three proposed-adapter checks passed on 100 actual training rows.

The two `.patch` files are review proposals, saved before application. They require
the pending user approval for application/contract behavior changes. The feature
proposal matches all 78,786 actual churn training rows exactly across 14 features;
it preserves the source boolean flag and two explicit-null category mappings.
The prediction adapter retains strict model identity/version, shape, data-count
and finite/range validation while matching the observed runtime response.

`model-source-integrity-after-artifacts.json` verifies all ten imported source
assets remain byte-identical. `provenance-index.json` hashes the evidence files.
The parent deployment task owns approval, image publication, feature-PVC loading,
model rollout, health scans and final live end-to-end verification.

## Live serving and authorization preflight

The separate diagnostic completed successfully at `2026-10-26T23:38:24Z`.
`live-serving/summary.json` and `live-serving/live-serving-auth-preflight.json`
record verified HTTPS certificate chains/hostnames, anonymous metadata/inference
HTTP 401 responses, and authorized version-specific metadata/inference HTTP 200
responses for all three models. Each model processed 100 actual training rows
using the same inputs as the local runtime comparison. Exact run-ID versions,
`predict` output names, `[100,1]` shapes and expected datatypes matched. Churn and
fraud values matched exactly; QoE's maximum absolute difference was
`0.0000152587890625`, within the recorded floating-point tolerance.

The approved prediction-tools image, actual prediction-tools ServiceAccount,
controller-issued model token Secrets and injected service CA were used. No
application/database writes occurred. The diagnostic Job, ConfigMap and owned pod
were deleted only after evidence capture and UID/label verification; cleanup is
recorded. Historical local-inspection files retain their original scope labels.
This live preflight confirms model serving and authorization only; M02 and full
application E2E remain pending the approved image rollout and actual MCP path.

# Bounded live model verification

`probe_models.py` is a diagnostic client for the normal HTTPS model endpoints. It
uses the prediction-tools ServiceAccount's controller-issued token Secrets and
the injected OpenShift service CA. TLS chain and hostname verification are required;
redirects and automatic retries are disabled. It performs four requests per model:
anonymous metadata and inference must return 401/403; authorized version-specific
metadata and inference must return 200. It then checks the exact model version,
tensor name/type/shape, finite/range bounds and agreement with previously verified
local runtime outputs for 100 actual training rows.

The current run's prepared specification is under the Factory state directory
`debug/live-model-probe/`. `config.json` records exact rendered chart endpoints,
successful pipeline versions, source artifact and request hashes, and local
reference values. It contains no credentials. `resources.yaml` contains one
immutable ConfigMap and one Job. Use only the reviewed prepared specification;
never substitute generated examples for actual trained inputs.

The Job uses the approved prediction-tools image digest, a read-only filesystem,
the restricted container security context, no automatically mounted API token,
and a six-minute deadline with no restart/retry. Its NetworkPolicy labels match
the normal prediction client. Its readiness probe deliberately stays false so
the prediction-tools Service cannot route application traffic to this diagnostic
container. Job completion is the diagnostic success signal.

The diagnostic makes no database, scoring, chat, storage or model-configuration
writes. It prints one sanitized JSON evidence document, including validated numeric
model responses. Credential values, authorization headers, provider exception
details and arbitrary error response bodies are never printed. This verifies
model serving only; the full application test plan remains a separate requirement.

After the coordinating deployment agent confirms model readiness and gives the
go-ahead, create only the named ConfigMap and Job with explicit namespace flags.
Save Job status, pod image ID and the JSON log before removing those exact owned
resources. Preserve failure evidence and diagnose before any retry; do not delete
unrelated resources or loosen authorization/TLS to obtain a passing result.

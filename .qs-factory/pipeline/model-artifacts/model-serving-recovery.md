# Model-serving infrastructure verification

All three trained models loaded successfully in OpenShift on 2026-10-26. The main
Helm release is revision 3. Each model InferenceService is Ready at generation 3,
each storage initializer completed with exit 0, and all replacement model servers
have zero restarts. See `model-serving-readiness.json` for model IDs, files, and logs.

The OpenShift AI connection webhook required
`opendatahub.io/connection-type-protocol: s3` on the three storage Secrets. The
legacy annotation alone allowed admission but removed model storage on updates.
The fix preserves credential bytes, read-only access, the original runtimes and
startup probes, all model binaries, and all ten imported source assets. The
provisioning helper now writes both protocol and legacy annotations.

After the corrected UPDATE dry-runs retained each exact key/path, Helm revision 3
succeeded but left the already-pruned unchanged CR fields absent. The recorded
minimal merge patches restored only the existing Helm-declared model storage
objects. Actual storage initializers then downloaded the models and the original
probes passed.

`models-only-deployment.yaml` records the executed command and a reproduction
command using the durable `models-only-values.yaml`. It uses `--no-hooks` because
existing database schema, grants, and seed were already verified and this stage
has no migration. The three inference services use authenticated HTTPS.

Churn uses the derived deployment prefix
`models/churn-serving/1ea4bc0e-f1c7-44af-8d80-58dc53eaa805`. Its native `model.bst`
is unchanged; separate model settings identify the served version. Fraud uses
`models/fraud`, and QoE uses `models/starlink-qoe`. These are the actually deployed
prefixes; the chart defaults are not a record of a particular training run.

This infrastructure stage deliberately leaves prediction-tools at zero replicas
with the previously approved image. It does not establish application E2E success
or approve pending application patches. Live HTTPS/authentication and response
validation are recorded separately by the model probe.

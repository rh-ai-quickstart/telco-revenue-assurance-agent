# Verified feature-artifact PVC

This separate Helm release creates `telco-model-feature-artifacts` and loads a
verified bundle from an explicit S3 prefix. It never trains models, invents feature
manifests, creates S3 credentials, or updates the application release.

The loader reads exactly `churn.json`, `fraud.json`, `qoe.json`, `scaler.joblib`, and
`label_encoders.joblib`. It verifies every byte against supplied SHA256/size
metadata, then uses the installed prediction-tools `FeatureRegistry` to validate
the three model schemas, names, versions, feature orders, and fitted QoE sidecars.
All five files must pass before the PVC's `current` symlink changes atomically.
The previous generation remains intact on validation failure. Existing generations
are reused only after their bytes and schemas pass again.

Supply **actual** bundle metadata with these fields; this is a schema description,
not a runnable example:

| Field | Required content |
| --- | --- |
| `schema_version` | Integer `1` |
| `source.bucket` | Bucket matching the readonly connection Secret |
| `source.prefix` | Explicit prefix containing precisely the five named objects |
| `models.churn` | `model_name: churn-prediction` and its observed `model_version` |
| `models.fraud` | `model_name: fraud-prediction` and its observed `model_version` |
| `models.qoe` | `model_name: starlink-qoe` and its observed `model_version` |
| `files` | Exactly five filename keys, each with lowercase `sha256` and integer `size_bytes` |

Keep the corresponding dataset revisions, pipeline run IDs, model binary hashes,
and real inference evidence with this bundle. The three feature JSON files must
have the same model identities as `models`. Maximum file size is 64 MiB and total
bundle size is 128 MiB. Joblib is loaded only after all hashes pass; provide trusted
sidecars from the verified training run.

`connectionSecret` must already contain the five `AWS_*` data-connection keys
described in [the serving guide](../README.md), with verified readonly access to
this prefix. The loader makes S3 `GetObject` calls only. It has no mounted
Kubernetes token or RBAC permissions. Its labels match the storage NetworkPolicy's
artifact-loader allowlist.

The main container uses the already-published prediction-tools image by digest.
That image contains the exact application schemas but does not include boto3 or
pip. A pinned UBI Python init container installs seven pinned pure-Python S3 client
dependencies into a shared `emptyDir`; package-index connectivity is required.
No container build or image push is needed. Both containers use a read-only root
filesystem and the normal OpenShift restricted security context.

## Local checks before deployment

From the application repository, set `BUNDLE_MANIFEST` to the actual metadata JSON
and `BUNDLE_DIRECTORY` to the directory containing the five verified local files.
These are nonsecret paths; credential values stay in the cluster Secret.

```bash
uv --cache-dir /tmp/telco-training-uv-cache run --frozen python \
  deployment/serving/artifacts/files/load_bundle.py \
  --manifest "$BUNDLE_MANIFEST" --validate-directory "$BUNDLE_DIRECTORY"

helm -n telco-revenue-assurance-agent lint deployment/serving/artifacts/ \
  --set-file bundleManifest="$BUNDLE_MANIFEST" \
  --set-string connectionSecret=telco-qoe-model-storage
```

The default chart deliberately fails to render without a real bundle manifest and
connection Secret name. Never deploy the synthetic fixtures used by unit tests.

## Publish without racing the RWO volume

The default claim uses the verified cluster storage class `gp3-csi` and ReadWriteOnce.
Live inspection found `gp3-csi` as the default AWS EBS CSI class with
`WaitForFirstConsumer`; both training PVCs are Bound using this class. The other
available class is `gp2-csi`. Override the value only after checking the target
cluster's actual class names.
The loader and prediction-tools cannot concurrently attach that volume on different
nodes. Prediction-tools also caches its manifests and preprocessing in memory;
consumers must be stopped during a bundle switch even though the on-disk switch is
atomic. This prevents a cached old manifest from being paired with new sidecars.

Before publishing, record the existing prediction-tools replica count in the
deployment evidence. If replicas are nonzero, stop that deployment and wait for its
Pods to disappear. This also prevents the current Pending Pods from binding a newly
created PVC ahead of the loader.

```bash
oc -n telco-revenue-assurance-agent get deployment prediction-tools \
  -o jsonpath='{.spec.replicas}{"\n"}'
oc -n telco-revenue-assurance-agent scale deployment prediction-tools --replicas=0
oc -n telco-revenue-assurance-agent wait --for=delete pod \
  -l app.kubernetes.io/instance=telco-revenue-assurance-agent,app.kubernetes.io/component=prediction-tools \
  --timeout=120s
```

Skip the wait when there are already no matching Pods. Do not force-delete Pods or
detach volumes. For an existing claim still attached to an observed node, the
optional `loader.nodeSelector.kubernetes.io/hostname` can select that node; use the
observed node label, never guess it. With no consumers, normal scheduling respects
the bound PV's topology. Do not use `nodeName`, which bypasses scheduling and can
prevent delayed volume binding.

Install the separate release only after the local bundle check passes:

```bash
helm -n telco-revenue-assurance-agent upgrade --install telco-feature-artifacts \
  deployment/serving/artifacts/ \
  --set-file bundleManifest="$BUNDLE_MANIFEST" \
  --set-string connectionSecret=telco-qoe-model-storage \
  --wait --wait-for-jobs --timeout=10m

oc -n telco-revenue-assurance-agent logs \
  -l app.kubernetes.io/instance=telco-feature-artifacts,app.kubernetes.io/component=model-artifact-loader \
  -c load-artifacts --tail=20
```

Require the Job to be Complete and retain its `verified_and_published` output,
bundle hash, and model identities in the deployment evidence. A changed bundle or
loader configuration receives a new immutable Job/ConfigMap name. Rerunning the
same release configuration reuses its completed Job. The PVC is retained on Helm
uninstall; removing a release must not delete trained preprocessing artifacts.

After recording success, remove only completed loader Pods so their volume mounts
are released before prediction-tools resumes:

```bash
oc -n telco-revenue-assurance-agent delete pod \
  -l app.kubernetes.io/instance=telco-feature-artifacts,app.kubernetes.io/component=model-artifact-loader \
  --field-selector=status.phase=Succeeded
```

Keep the completed Job as evidence. Redeploy the application using its reviewed
command and the recorded intended replica count. Its runtime values must set
`predictionTools.modelFeatureManifestDir: /etc/telco-models/current`; keep
`predictionTools.featureArtifactsMountPath: /etc/telco-models`. Confirm all three
model endpoints and the application E2E tests. If loading fails, leave consumers
stopped until the failure is diagnosed; never publish partial files to make a
readiness check pass. Old generations are retained for deliberate rollback and
are not automatically deleted.

## Loader tests

```bash
uv --cache-dir /tmp/telco-training-uv-cache run --frozen pytest -q \
  deployment/serving/artifacts/tests/
```

Tests cover a valid atomic bundle, repeat loading, missing objects, hash and size
mismatches, schema/model-version mismatches, unexpected filenames, symlink
containment, and refusal to replace an unmanaged directory. They use synthetic
unit fixtures and no cluster or S3 connection.

References: [Kubernetes volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes),
[node assignment](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/),
and [Boto3 GetObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html).

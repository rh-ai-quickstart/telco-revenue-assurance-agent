# Optional model training infrastructure

This separate Helm release provides a Data Science Pipelines server and private S4 storage in the quickstart namespace. It uses `configure-pipeline` 0.6.0 with `aws-compatible-storage` 0.2.0. It creates no notebook or RAG resources and does not upgrade the application release.

An existing OpenShift AI pipeline operator and a working default storage class are required. The chart uses 10Gi for S4 and 10Gi for the pipeline metadata database. Increase S4 storage before retaining larger datasets or many training runs.

From the repository root, after logging into OpenShift:

```bash
make prepare-training NAMESPACE=telco-revenue-assurance-agent
make deploy-training NAMESPACE=telco-revenue-assurance-agent
make verify-training NAMESPACE=telco-revenue-assurance-agent
```

`prepare-training` downloads the locked chart dependency and prepares storage Secrets. `deploy-training` also calls preparation, so it can be run directly. The release defaults to `telco-training`; change `TRAINING_RELEASE_NAME` to change its Helm name. The shared chart creates a singleton DSPA named `dspa`, so install only one training release per namespace.

The preparation helper generates random credentials in memory for `telco-training-s3`, then creates the upload Secret names required by the preserved pipeline sources: `my-data-connection` and `dashboard-dspa-secret`. Credential values are never written to files, command-line arguments, or logs. Existing matching Secrets are reused. Conflicting or incomplete existing Secrets cause preparation to stop; they are never silently overwritten or rotated. The Secrets are independent of the Helm release and persist after its removal.

Nonsecret configuration lives in `values.yaml`. The default private endpoint is `http://telco-training-storage.<namespace>.svc.cluster.local:7480`. The bootstrap Job creates `mlpipeline` for DSP artifacts and `mybucket` for original input datasets and trained models. It does not download or generate data. Its distinct labels prevent it from becoming a storage Service endpoint. Completed bootstrap Jobs are retained; ordinary deployment reruns reuse them. Changing its Pod template after installation requires an explicitly reviewed Job replacement.

The storage NetworkPolicy allows the pipeline operator's storage probe, DSP components, pipeline task Pods, this bootstrap Job, and the three model-serving Pods. A later feature-artifact loader must carry both `app.kubernetes.io/part-of: telco-revenue-assurance-agent` and `app.kubernetes.io/component: model-artifact-loader`. The operator manages its own API/database ingress policies. Public S3 and storage UI routes remain disabled.

After infrastructure verification, upload the three original datasets under `mybucket/data/`, verify their hashes, and submit the copied pipelines using the actual DSP route with normal OpenShift authentication and trusted TLS. Pass the private S3 endpoint, bucket, and model prefix explicitly to QoE. Keep the imported pipeline files unchanged. Match the SDK to the installed pipeline backend; infrastructure readiness does not establish training or inference success.

For local configuration checks without creating cluster resources:

```bash
make training-depend
make verify-training-config
```


For KFP 2.16, submit these imported S3 datasets with an explicit run root in the same bucket:

```python
client.create_run_from_pipeline_package(
    pipeline_file=compiled_package,
    arguments=verified_arguments,
    pipeline_root="s3://mybucket/pipeline-artifacts",
    enable_caching=False,
    # Supply the verified run name, experiment ID and namespace as usual.
)
```

The `pipeline_root` argument is a run setting, separate from the pipeline's own parameters. The launcher reuses the configured S3 connection for an imported dataset in the run's bucket. With the default `s3://mlpipeline` root and datasets in `mybucket`, KFP 2.16 falls back to task environment credentials and fails before data preparation. Keeping run artifacts under `mybucket/pipeline-artifacts/` preserves the original dataset paths and training source. The pipeline server continues to use `mlpipeline` for its own files. A failed run created with the old root must be preserved for evidence and replaced with a new run using the corrected root; retrying it retains the old setting.

References: [Kubeflow run root configuration](https://www.kubeflow.org/docs/components/pipelines/user-guides/data-handling/pipeline-root/) and [KFP 2.16 launcher bucket selection](https://github.com/kubeflow/pipelines/blob/2.16.0/backend/src/v2/component/launcher_v2.go#L845).

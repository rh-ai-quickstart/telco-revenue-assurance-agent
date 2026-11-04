# Model pipeline integration — 2026-10-26

This resumes the existing implementation to import the three tested model pipelines from source commit `3037c340c5a4a27ae53f95b9146562c12c985c79` in `/home/yelias/Desktop/Projects/gitlab-telco/telco-revenue-assurance-agent`. Pipeline algorithms, dependencies, parameters, upload behavior and entry points are preserved unchanged. The Python entry points submit real training runs; verification parses them without importing or executing them.

The user confirmed that these pipelines should be prepared for running later. Existing trained models are not assumed to be available. Training outputs and additional application feature metadata must be provided before deployment can perform predictions.

## Imported models

| Model | Pipeline | Artifact layout in the configured bucket |
|---|---|---|
| Fraud, BalancedRandomForest ONNX | `pipeline/01-telco-revass-randomforest-pipeline.py` | `models/1/model.onnx`; serving mounts `models/` |
| Churn, LightGBM | `pipeline/03-telco-churn-kfp-pipeline.py` | `models/churn/1/model.bst` and `model-settings.json` |
| Starlink QoE, transformer ONNX | `pipeline/starlink_qoe_pipeline.py` and existing compiled YAML | `models/starlink-qoe/1/model.onnx`; preprocessing in `models/starlink-qoe/artifacts/` |

All six original serving YAML files are retained as source snapshots. They contain their original namespaces, exported resource metadata and, for fraud, Kubernetes List wrappers. Operational deployment uses the quickstart Helm chart, which preserves source serving behavior while applying the previously approved release namespace and authenticated internal service policy. Do not apply the raw snapshots as this quickstart's deployment resources.

## Deployment inputs still required

The source checkout contains neither trained binaries nor training datasets. Local integration cannot establish that the destination cluster can access the tested artifacts.

- Provide existing S3 connection Secret references in the release namespace, or explicit artifact URIs with the corresponding storage credentials. Secrets and tokens remain outside Git.
- Make the actual model files above available at the configured storage prefixes.
- Populate `predictionTools.featureArtifactsClaim` with `churn.json`, `fraud.json`, `qoe.json`, `scaler.joblib`, and `label_encoders.joblib` at the mounted directory root. Creating an empty claim is insufficient.
- Supply the three model authentication Secrets used by `prediction-tools`.

The three JSON manifests are an existing application interface, not outputs of the copied pipelines. Their common fields are `model_name`, `model_version`, `input_tensor_name`, `output_tensor_name`, `feature_order`, and `feature_width`. Model versions must match serving responses.

Churn uses tensor names `predict`/`predict`; obtain the exact ordered feature names from the trained LightGBM artifact or preserved training metadata. Fraud uses input `float_input`; obtain the exact output name, ordered features, and training-time Plan_Type categories from the tested artifact/training metadata. Its manifest additionally needs `plan_type_categories` and `class_order: [false, true]`. Do not infer semantic feature order from an ONNX tensor width.

QoE uses input `input`, output `output`, and feature order `latitude`, `longitude`, `elevation_m`, `visible_satellites`, `serving_satellites`, `signal_loss_db`, `season_encoded`, `weather_encoded`. Its manifest additionally needs `scaler_file: scaler.joblib` and `label_encoders_file: label_encoders.joblib`. Copy the fitted sidecars from the matching trained model; do not refit them on application data.

The copied KFP pipelines train and upload artifacts; they do not create serving resources or application manifests. They still require their original Data Science Pipelines endpoint, data connections, training inputs and permissions. Fraud retains its hardcoded upload Secret `my-data-connection`; QoE retains `dashboard-dspa-secret`. These names are preserved as requested. Running training is outside this local implementation session.

## Running the copied pipelines later

Prepare a separate SDK environment providing `kfp.compiler`, `kfp.dsl`, `from kfp import kubernetes`, and `dotenv.load_dotenv` (the KFP SDK, Kubernetes extension, and python-dotenv). The source provides no dependency manifest. Its supplied QoE YAML records `kfp-2.17.0`; other SDK compatibility was not established here. Pipeline task dependencies are already declared in the unchanged component decorators and are installed inside task containers.

Set `KUBEFLOW_ENDPOINT` to the intended DSPA endpoint. Each script prefers `/run/secrets/kubernetes.io/serviceaccount/token` and falls back to `BEARER_TOKEN` only when that file is absent. Supply credentials through the execution environment, outside Git. All scripts call `load_dotenv(override=True)`, so a conflicting `.env` value takes precedence over an exported variable. The source's existing TLS client behavior is unchanged.

Prepare these datasets and upload Secrets in the namespace where each KFP run executes:

| Pipeline | Default input dataset | Upload configuration |
|---|---|---|
| Fraud | `s3://mybucket/data/telecom_revass_data.csv.xz` | Hardcoded `my-data-connection`; bucket/endpoint from Secret; `models/1/model.onnx` |
| Churn | `s3://mybucket/data/synthetic_customer_data_evenly_distributed.csv` | `secret_name` defaults to `my-data-connection`; bucket/endpoint from Secret; `models/churn/1/` |
| QoE | `s3://mybucket/data/starlink_data.csv` | Hardcoded `dashboard-dspa-secret`; default endpoint `https://s3.openshift-storage.svc:443`, bucket `mybucket`, prefix `models/starlink-qoe` |

Fraud/churn upload Secrets require `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_BUCKET`, and `AWS_S3_ENDPOINT`. QoE's upload Secret supplies the access key pair; its endpoint/bucket/path are pipeline parameters. The DSPA importer/artifact configuration must independently be able to read the input datasets because upload credentials are attached only to upload tasks.

From the quickstart root, the following commands submit real training/upload runs using the declared pipeline defaults. Run them only after preparing the SDK environment, endpoint, credentials and datasets:

```bash
BASE_IMAGE=quay.io/ecisse/custom-workbench-rafm:1.0 python pipeline/01-telco-revass-randomforest-pipeline.py
BASE_IMAGE=quay.io/ecisse/custom-workbench-rafm:1.0 python pipeline/03-telco-churn-kfp-pipeline.py
BASE_IMAGE=registry.access.redhat.com/ubi9/python-311:latest python pipeline/starlink_qoe_pipeline.py
```

The models are independent. Fraud/churn use experiments `revass`/`churn` and wait up to 300 seconds; QoE uses `starlink-qoe` and waits up to 3600 seconds. All three scripts submit with `arguments={}` and have no CLI parameter or compile-only options. For different dataset/storage/training parameters, use the KFP run interface or a separate parameterized SDK submission without editing the copied files. QoE's existing `pipeline/starlink_qoe_pipeline.yaml` can be uploaded directly; fraud/churn need compilation through the prepared SDK. No new compiled packages were generated in this session.

Retain the run ID, dataset revision and actual output URIs. Before KFP artifact cleanup, preserve fraud's `train_model.model_file` and `data_prep.x_train_file`, churn's `train_lgbm_model.model_file` and `data_prep.x_train_file`, and QoE's `train_model.model_file`, `data_prep.scaler_file` and `data_prep.label_encoders_file`. Fraud/churn model pickles retain ordered feature names; fraud additionally needs the original Plan_Type categories, including the dropped baseline. QoE uploads both fitted sidecars under `models/starlink-qoe/artifacts/`.

Use those actual outputs to prepare the application manifests described above. Confirm served model versions and tensor names; the S3 `1/` directory alone does not establish the response version. Populate the feature-artifact PVC, supply serving storage/authentication Secrets in the quickstart namespace, and configure `modelServing.<model>.storageSecretName` and `storagePath`, or an explicit `uri`, before a later deployment.

## Verification

All 26 distinct local checks passed in their final runs; full command output is in [model-integration-verification-2026-10-26.json](model-integration-verification-2026-10-26.json). All 22 implementation acceptance criteria passed.

- `make test`: 147 Python tests and 4 UI tests passed.
- `make lint`: Python lint/format, strict types, UI lint/types, and configured Prettier checks passed.
- `helm lint chart/` and `helm template chart/`: passed.
- `make verify-model-assets`: all ten source hashes, static Python/YAML parsing, complete source runtime specifications, predictor behavior, data connections, explicit URI overrides, namespace isolation, and internal authentication passed.
- All frozen contract provider/consumer commands passed. Regenerating UI types preserved their exact bytes.
- Source checkout remained clean, all imported files matched it byte-for-byte, and no baseline files were removed.

The existing `.qs-factory/pipeline/deploy-state.yaml` is historical cluster evidence and remains unchanged. No cluster calls, pipeline submissions, model training or uploads occurred. Local source/schema validation does not establish live inference compatibility with artifacts that have not been supplied or trained.

## Scope accounting

The target began with substantial uncommitted implementation/deployment work. A session-start file-hash baseline was captured under `/tmp/telco-model-integration-baseline/`; final review compares against it rather than assuming HEAD is the current implementation. The greenfield component gate's clean-worktree assumption does not apply. Existing work is preserved and no unrelated work is committed or reverted.

The initial lint gate found existing formatting problems in `packages/api/src/api/services/ogx_client.py`, `packages/api/tests/test_chat_idempotency.py`, and `packages/api/tests/test_ogx_client.py`. These three files were formatted only; pre/post Python ASTs are identical. The initial failure remains in the verification record, followed by passing lint and 34 focused API tests. An existing stale OGX contract hash was synchronized with its unchanged session-start contents.

The Prettier pre-commit hook excludes exactly the seven imported YAML files so it cannot require changes to the preserved source snapshots. Other files, including provenance metadata and future assets, remain checked.

Existing deferred items remain recorded in the implementation manifest: final source database schema reconciliation, ambiguous OGX timeout deduplication, live PostgreSQL integration verification, and an optional moderation provider. None were changed by this model import.

"""Kubeflow pipeline for Telco Revenue Assurance - Fraud Detection."""

import os

import kfp.compiler
from dotenv import load_dotenv
from kfp import dsl, kubernetes


load_dotenv(override=True)

kubeflow_endpoint = os.getenv("KUBEFLOW_ENDPOINT", "")
base_image = os.getenv("BASE_IMAGE", "image-registry.openshift-image-registry.svc:5000/telco-revenue-assurance-agent/pipeline-base:latest")


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn"],
)
def data_prep(
    dataset_file: dsl.Input[dsl.Dataset],
    x_train_file: dsl.Output[dsl.Dataset],
    x_test_file: dsl.Output[dsl.Dataset],
    y_train_file: dsl.Output[dsl.Dataset],
    y_test_file: dsl.Output[dsl.Dataset],
):
    import pandas as pd
    from sklearn.model_selection import train_test_split
    import lzma

    def get_data(dataset_path: str) -> pd.DataFrame:
        with lzma.open(dataset_path, "rb") as f:
            data = pd.read_csv(f)
        data.info()
        print("Initial Dataset:")
        print(data.head())
        return data

    def create_training_set(dataset: pd.DataFrame):
        missing_values = dataset.isnull().sum()
        print("Missing values in each column:", missing_values)

        dataset = pd.get_dummies(dataset, columns=["Plan_Type"], drop_first=True)

        X = dataset.drop("Fraud", axis=1)
        y = dataset["Fraud"]

        x_train, x_test, y_train, y_test = train_test_split(
            X, y, test_size=0.3, random_state=42, stratify=y
        )
        return x_train, x_test, y_train, y_test

    dataset = get_data(dataset_file.path)
    x_train, x_test, y_train, y_test = create_training_set(dataset)

    x_train.to_pickle(x_train_file.path)
    x_test.to_pickle(x_test_file.path)
    y_train.to_pickle(y_train_file.path)
    y_test.to_pickle(y_test_file.path)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "imbalanced-learn"],
)
def train_model(
    x_train_file: dsl.Input[dsl.Dataset],
    y_train_file: dsl.Input[dsl.Dataset],
    model_file: dsl.Output[dsl.Model],
):
    import pickle
    import pandas as pd
    from imblearn.ensemble import BalancedRandomForestClassifier

    x_train = pd.read_pickle(x_train_file.path)
    y_train = pd.read_pickle(y_train_file.path)

    model = BalancedRandomForestClassifier(
        random_state=42,
        n_estimators=200,
        min_samples_split=10,
        min_samples_leaf=2,
        max_features="sqrt",
        max_depth=100,
        bootstrap=True,
        sampling_strategy="all",
        replacement=True,
    )
    model.fit(x_train, y_train)

    with open(model_file.path, "wb") as f:
        pickle.dump((model, x_train.columns.tolist()), f)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn"],
)
def evaluate_model(
    x_test_file: dsl.Input[dsl.Dataset],
    y_test_file: dsl.Input[dsl.Dataset],
    model_file: dsl.Input[dsl.Model],
    mlpipeline_metrics_file: dsl.Output[dsl.Metrics],
):
    import pickle
    import json
    import pandas as pd
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix

    x_test = pd.read_pickle(x_test_file.path)
    y_test = pd.read_pickle(y_test_file.path)

    with open(model_file.path, "rb") as f:
        model, feature_names = pickle.load(f)

    y_pred = model.predict(x_test)

    acc_score = accuracy_score(y_test, y_pred)
    print("BalancedRandomForestClassifier Results:")
    print("Confusion Matrix:")
    print(confusion_matrix(y_test, y_pred))
    print("\nClassification Report:")
    print(classification_report(y_test, y_pred))
    print(f"\nAccuracy Score: {acc_score}")

    metrics = {
        "metrics": [
            {"name": "accuracy-score", "numberValue": acc_score, "format": "PERCENTAGE"},
        ]
    }
    with open(mlpipeline_metrics_file.path, "w") as f:
        json.dump(metrics, f)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "imbalanced-learn"],
)
def validate_model(model_file: dsl.Input[dsl.Model]):
    import pickle
    import pandas as pd

    with open(model_file.path, "rb") as f:
        model, feature_names = pickle.load(f)

    sample = {
        "Call_Duration": 5.6,
        "Data_Usage": 150.3,
        "Sms_Count": 20,
        "Roaming_Indicator": 0,
        "MobileWallet_Use": 1,
        "Plan_Type": "prepaid",
        "Cost": 50.75,
        "Cellular_Location_Distance": 1.2,
        "Personal_Pin_Used": 1,
        "Avg_Call_Duration": 4.3,
        "Avg_Data_Usage": 120.6,
        "Avg_Cost": 45.7,
    }
    input_values = pd.DataFrame([sample])
    input_values = input_values.reindex(columns=feature_names, fill_value=0)
    prediction = model.predict(input_values)
    label = "Fraud" if prediction[0] == 1 else "Non-Fraud"
    print(f"Validation prediction: {label}")


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "imbalanced-learn", "skl2onnx", "onnxruntime"],
)
def export_to_onnx(
    model_file: dsl.Input[dsl.Model],
    onnx_file: dsl.Output[dsl.Model],
):
    """Convert BRFC model to ONNX for OVMS serving runtime.

    skl2onnx has no built-in converter for BalancedRandomForestClassifier.
    We register one via update_registered_converter using:
      - calculate_linear_classifier_output_shapes: generic shape calculator for
        any external classifier (same pattern as the skl2onnx LightGBM tutorial)
      - convert_sklearn_random_forest_classifier: sklearn RFC converter, valid for
        BRFC because its decision trees have an identical structure at inference time
    """
    import pickle
    import numpy as np
    from imblearn.ensemble import BalancedRandomForestClassifier
    from skl2onnx import convert_sklearn, update_registered_converter
    from skl2onnx.common.data_types import FloatTensorType
    from skl2onnx.shape_calculators.ensemble_shapes import calculate_tree_classifier_output_shapes
    from skl2onnx.operator_converters.random_forest import convert_sklearn_random_forest_classifier
    import onnxruntime as rt

    # Full options dict mirrors what skl2onnx registers for RandomForestClassifier -
    # the converter validates that every option it may read is declared here.
    update_registered_converter(
        BalancedRandomForestClassifier,
        "ImlearnBalancedRandomForestClassifier",
        calculate_tree_classifier_output_shapes,
        convert_sklearn_random_forest_classifier,
        options={
            "zipmap":              [True, False, "columns"],
            "raw_scores":          [True, False],
            "nocl":                [True, False],
            "output_class_labels": [False, True],
            "decision_path":       [True, False],
            "decision_leaf":       [True, False],
        },
    )

    with open(model_file.path, "rb") as f:
        model, feature_names = pickle.load(f)

    n_features = len(feature_names)
    initial_type = [("float_input", FloatTensorType([None, n_features]))]

    onnx_model = convert_sklearn(
        model,
        initial_types=initial_type,
        options={BalancedRandomForestClassifier: {"zipmap": False}},
    )

    # Verify ONNX model inference before upload
    sess = rt.InferenceSession(onnx_model.SerializeToString())
    dummy = np.zeros((1, n_features), dtype=np.float32)
    input_name = sess.get_inputs()[0].name
    result = sess.run(None, {input_name: dummy})
    print(f"ONNX validation - output shape: {result[0].shape}, prediction: {result[0]}")

    with open(onnx_file.path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    print(f"ONNX model exported with {n_features} features: {feature_names}")


@dsl.component(
    base_image=base_image,
    packages_to_install=["boto3"],
)
def upload_model_to_s3(
    model_file: dsl.Input[dsl.Model],
    s3_key: str,
):
    """Upload model artifact to MinIO/S3 for KServe OVMS to consume.

    Follows the rh-aiservices-bu/fraud-detection reference implementation:
    - boto3 Session + resource API (not client)
    - signature_version='s3v4' required by MinIO
    - S3 key must include version dir: models/1/model.onnx
      OVMS expects <storageUri>/1/model.onnx inside the downloaded directory.

    All S3 env vars injected via kubernetes.use_secret_as_env() from 'my-storage' secret.
    """
    import os
    import boto3
    import botocore
    import botocore.client

    aws_access_key_id     = os.environ["AWS_ACCESS_KEY_ID"]
    aws_secret_access_key = os.environ["AWS_SECRET_ACCESS_KEY"]
    endpoint_url          = os.environ["AWS_S3_ENDPOINT"]
    region_name           = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    bucket_name           = os.environ["AWS_S3_BUCKET"]

    # boto3 requires a full URL — add https:// if the secret stores only the hostname
    if not endpoint_url.startswith("http://") and not endpoint_url.startswith("https://"):
        endpoint_url = "https://" + endpoint_url
        print(f"[upload] endpoint normalized to: {endpoint_url}")

    # Resolve actual file path - KFP may present a Model artifact as a directory
    local_path = model_file.path
    if os.path.isdir(local_path):
        files = [f for f in os.listdir(local_path) if os.path.isfile(os.path.join(local_path, f))]
        if not files:
            raise FileNotFoundError(f"No files found in artifact directory: {local_path}")
        local_path = os.path.join(local_path, files[0])
        print(f"Artifact dir detected - resolved to: {local_path}")

    print(f"[upload] local path  : {local_path}")
    print(f"[upload] file size   : {os.path.getsize(local_path)} bytes")
    print(f"[upload] endpoint    : {endpoint_url}")
    print(f"[upload] bucket      : {bucket_name}")
    print(f"[upload] s3 key      : {s3_key}")
    print(f"[upload] region      : {region_name}")

    print("[upload] Creating boto3 session...")
    session = boto3.session.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    print("[upload] Creating S3 resource (SigV4)...")
    s3_resource = session.resource(
        "s3",
        config=botocore.client.Config(signature_version="s3v4"),
        endpoint_url=endpoint_url,
        region_name=region_name,
    )

    print("[upload] Starting upload...")
    bucket = s3_resource.Bucket(bucket_name)
    bucket.upload_file(local_path, s3_key)
    print(f"[upload] Done: s3://{bucket_name}/{s3_key}")

    # Upload model-settings.json alongside the model for MLServer ONNX runtime
    import json
    import tempfile
    settings = {
        "name": "fraud-prediction",
        "implementation": "mlserver_onnx.OnnxModel",
        "parameters": {"uri": "model.onnx"},
    }
    settings_key = s3_key.rsplit("/", 1)[0] + "/model-settings.json"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(settings, f, indent=2)
        tmp_path = f.name
    bucket.upload_file(tmp_path, settings_key)
    os.unlink(tmp_path)
    print(f"[upload] Done: s3://{bucket_name}/{settings_key}")


@kfp.dsl.pipeline(
    name="Telco Revenue Assurance - Fraud Detection Pipeline",
)
def pipeline(
    artifact_uri: str = "s3://mybucket/data/telecom_revass_data.csv.xz",
    s3_model_key: str = "models/fraud/1/model.onnx",
    secret_name: str = "my-data-connection",
):
    import_dataset_task = dsl.importer(
        artifact_uri=artifact_uri,
        artifact_class=dsl.Dataset,
        reimport=False,
    )

    data_prep_task = data_prep(dataset_file=import_dataset_task.output)

    train_model_task = train_model(
        x_train_file=data_prep_task.outputs["x_train_file"],
        y_train_file=data_prep_task.outputs["y_train_file"],
    )

    evaluate_model(
        x_test_file=data_prep_task.outputs["x_test_file"],
        y_test_file=data_prep_task.outputs["y_test_file"],
        model_file=train_model_task.output,
    )

    validate_model(model_file=train_model_task.output)

    export_onnx_task = export_to_onnx(model_file=train_model_task.output)

    upload_model_task = upload_model_to_s3(
        model_file=export_onnx_task.output,
        s3_key=s3_model_key,
    )

    kubernetes.use_secret_as_env(
        task=upload_model_task,
        secret_name='my-data-connection',
        secret_key_to_env={
            "AWS_ACCESS_KEY_ID":     "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
            #"AWS_DEFAULT_REGION":    "AWS_DEFAULT_REGION",
            "AWS_S3_BUCKET":         "AWS_S3_BUCKET",
            "AWS_S3_ENDPOINT":       "AWS_S3_ENDPOINT",
        },
    )


if __name__ == "__main__":
    print(f"Connecting to kfp: {kubeflow_endpoint}")

    sa_token_path = "/run/secrets/kubernetes.io/serviceaccount/token"
    if os.path.isfile(sa_token_path):
        with open(sa_token_path) as f:
            bearer_token = f.read().rstrip()
    else:
        bearer_token = os.environ["BEARER_TOKEN"]

    sa_ca_cert = "/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
    ssl_ca_cert = sa_ca_cert if os.path.isfile(sa_ca_cert) else None

    client = kfp.Client(
        host=kubeflow_endpoint,
        existing_token=bearer_token,
        verify_ssl=False,
    )
    result = client.create_run_from_pipeline_func(pipeline, arguments={}, experiment_name="revass")
    print(f"Starting pipeline run with run_id: {result.run_id}")
    client.wait_for_run_completion(result.run_id, 300)

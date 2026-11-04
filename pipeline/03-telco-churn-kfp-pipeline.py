"""Kubeflow pipeline for Telco Customer Churn Prediction."""

import os

import kfp.compiler
from dotenv import load_dotenv
from kfp import dsl, kubernetes


load_dotenv(override=True)

kubeflow_endpoint = os.getenv("KUBEFLOW_ENDPOINT", "https://ds-pipeline-dspa-showcase.apps.ptnr2.cert4.bos2.lab")
base_image = os.getenv("BASE_IMAGE", "quay.io/ecisse/custom-workbench-rafm:1.0")


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

    data = pd.read_csv(dataset_file.path)
    data.info()
    print("Initial Dataset:")
    print(data.head())

    # Check for missing values
    print("Missing values:\n", data.isnull().sum())

    # Map categorical columns to numeric
    payment_mapping = {"Paid": 0, "Partial": 0.5, "Unpaid": 1}
    issue_mapping = {"": 0, "Billing": 0.7, "Technical": 0.8, "Service Quality": 0.6}
    support_channel_mapping = {"": 0, "Phone": 1, "Chat": 0.5, "Email": 0.2}

    data["PaymentStatus"] = data["PaymentStatus"].map(payment_mapping)
    data["PrimaryIssueType"] = data["PrimaryIssueType"].fillna("").map(issue_mapping)
    data["SupportChannel"] = data["SupportChannel"].fillna("").map(support_channel_mapping)

    date_columns = ["BillingCycleStart", "BillingCycleEnd", "PaymentDueDate", "LastInteractionDate"]
    cols_to_drop = ["HasChurned"] + date_columns
    if "CustomerID" in data.columns:
        cols_to_drop.append("CustomerID")

    X = data.drop(columns=cols_to_drop)
    y = data["HasChurned"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

    X_train.to_pickle(x_train_file.path)
    X_test.to_pickle(x_test_file.path)
    y_train.to_pickle(y_train_file.path)
    y_test.to_pickle(y_test_file.path)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "lightgbm==4.6.0"],
)
def train_lgbm_model(
    x_train_file: dsl.Input[dsl.Dataset],
    y_train_file: dsl.Input[dsl.Dataset],
    model_file: dsl.Output[dsl.Model],
):
    import pickle
    import pandas as pd
    import lightgbm as lgb

    X_train = pd.read_pickle(x_train_file.path)
    y_train = pd.read_pickle(y_train_file.path)

    model = lgb.LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=7,
        num_leaves=31,
        random_state=42,
    )
    model.fit(X_train, y_train)

    with open(model_file.path, "wb") as f:
        pickle.dump((model, X_train.columns.tolist()), f)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "lightgbm"],
)
def evaluate_model(
    x_test_file: dsl.Input[dsl.Dataset],
    y_test_file: dsl.Input[dsl.Dataset],
    model_file: dsl.Input[dsl.Model],
    model_name: str,
    mlpipeline_metrics_file: dsl.Output[dsl.Metrics],
):
    import json
    import pickle
    import pandas as pd
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, roc_auc_score

    def compute_lift(y_true, y_scores, n_bins=10):
        df = pd.DataFrame({"y_true": y_true, "y_scores": y_scores})
        df = df.sort_values("y_scores", ascending=False).reset_index(drop=True)
        df["bin"] = pd.qcut(df.index, q=n_bins, labels=False)
        overall_rate = df["y_true"].mean()
        lift_df = df.groupby("bin").agg(
            count=("y_true", "count"),
            positive_rate=("y_true", "mean"),
        ).reset_index()
        lift_df["lift"] = lift_df["positive_rate"] / overall_rate
        return lift_df

    def compute_gini(y_true, y_scores):
        return 2 * roc_auc_score(y_true, y_scores) - 1

    X_test = pd.read_pickle(x_test_file.path)
    y_test = pd.read_pickle(y_test_file.path)

    with open(model_file.path, "rb") as f:
        model, feature_names = pickle.load(f)

    y_pred = model.predict(X_test)
    y_pred_proba = model.predict_proba(X_test)[:, 1]

    lift_df = compute_lift(y_test, y_pred_proba)
    gini_score = compute_gini(y_test, y_pred_proba)
    acc_score = accuracy_score(y_test, y_pred)

    print(f"--- {model_name} Results ---")
    print("Lift by Decile:")
    print(lift_df.to_string())
    print(f"\nGini Score: {gini_score:.4f}")
    print("Confusion Matrix:")
    print(confusion_matrix(y_test, y_pred))
    print("\nClassification Report:")
    print(classification_report(y_test, y_pred))
    print(f"\nAccuracy Score: {acc_score}")
    print("---------------")

    metrics = {
        "metrics": [
            {"name": "accuracy-score", "numberValue": acc_score, "format": "PERCENTAGE"},
            {"name": "gini-score", "numberValue": gini_score, "format": "RAW"},
        ]
    }
    with open(mlpipeline_metrics_file.path, "w") as f:
        json.dump(metrics, f)


@dsl.component(
    base_image=base_image,
    packages_to_install=["lightgbm==4.6.0"],
)
def export_lgbm_native(
    model_file: dsl.Input[dsl.Model],
    lgbm_file: dsl.Output[dsl.Model],
) -> int:
    """Save the LightGBM booster in native .bst format for MLServer lgbm runtime.

    Returns feature count so upload_model_to_s3 can write the input schema
    into model-settings.json, populating the /v2/models metadata endpoint.
    """
    import pickle
    with open(model_file.path, "rb") as f:
        model, feature_names = pickle.load(f)

    model.booster_.save_model(lgbm_file.path)
    print(f"LightGBM booster saved with {len(feature_names)} features: {feature_names}")
    return len(feature_names)


@dsl.component(
    base_image=base_image,
    packages_to_install=["boto3"],
)
def upload_model_to_s3(
    model_file: dsl.Input[dsl.Model],
    s3_key: str,
    n_features: int,
    model_name: str = "churn-prediction",
):
    import os
    import boto3
    import botocore.client

    aws_access_key_id     = os.environ["AWS_ACCESS_KEY_ID"]
    aws_secret_access_key = os.environ["AWS_SECRET_ACCESS_KEY"]
    endpoint_url          = os.environ["AWS_S3_ENDPOINT"]
    region_name           = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    bucket_name           = os.environ["AWS_S3_BUCKET"]

    if not endpoint_url.startswith("http://") and not endpoint_url.startswith("https://"):
        endpoint_url = "https://" + endpoint_url

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

    session = boto3.session.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    s3_resource = session.resource(
        "s3",
        config=botocore.client.Config(signature_version="s3v4"),
        endpoint_url=endpoint_url,
        region_name=region_name,
    )

    bucket = s3_resource.Bucket(bucket_name)
    bucket.upload_file(local_path, s3_key)
    print(f"[upload] Done: s3://{bucket_name}/{s3_key}")

    # Upload model-settings.json alongside the model so MLServer resolves model.bst
    import json
    import tempfile
    settings = {
        "name": model_name,
        "implementation": "mlserver_lightgbm.LightGBMModel",
        "parameters": {"uri": "model.bst"},
        "inputs": [
            {"name": "predict", "datatype": "FP32", "shape": [-1, n_features]},
        ],
        "outputs": [
            {"name": "predict", "datatype": "INT64", "shape": [-1]},
        ],
    }
    settings_key = s3_key.rsplit("/", 1)[0] + "/model-settings.json"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(settings, f, indent=2)
        tmp_path = f.name
    bucket.upload_file(tmp_path, settings_key)
    os.unlink(tmp_path)
    print(f"[upload] Done: s3://{bucket_name}/{settings_key}")


@kfp.dsl.pipeline(
    name="Telco Churn Prediction Pipeline",
)
def pipeline(
    artifact_uri: str = "s3://mybucket/data/synthetic_customer_data_evenly_distributed.csv",
    s3_model_key: str = "models/churn/1/model.bst",
    secret_name: str = "my-data-connection",
):
    import_dataset_task = dsl.importer(
        artifact_uri=artifact_uri,
        artifact_class=dsl.Dataset,
        reimport=False,
    )

    data_prep_task = data_prep(dataset_file=import_dataset_task.output)

    train_lgbm_task = train_lgbm_model(
        x_train_file=data_prep_task.outputs["x_train_file"],
        y_train_file=data_prep_task.outputs["y_train_file"],
    )

    evaluate_model(
        x_test_file=data_prep_task.outputs["x_test_file"],
        y_test_file=data_prep_task.outputs["y_test_file"],
        model_file=train_lgbm_task.output,
        model_name="LightGBM",
    )

    export_task = export_lgbm_native(model_file=train_lgbm_task.output)

    upload_model_task = upload_model_to_s3(
        model_file=export_task.outputs["lgbm_file"],
        s3_key=s3_model_key,
        n_features=export_task.outputs["Output"],
        model_name="churn-prediction",
    )

    kubernetes.use_secret_as_env(
        task=upload_model_task,
        secret_name=secret_name,
        secret_key_to_env={
            "AWS_ACCESS_KEY_ID":     "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
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
    result = client.create_run_from_pipeline_func(pipeline, arguments={}, experiment_name="churn")
    print(f"Starting pipeline run with run_id: {result.run_id}")
    client.wait_for_run_completion(result.run_id, 300)

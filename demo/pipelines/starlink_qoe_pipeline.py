"""Kubeflow pipeline for Starlink QoE prediction model training and evaluation.

This pipeline implements the full ML workflow for the Starlink Internet Service
Quality of Experience (QoE) Transformer model:
  1. Data preparation — load CSV, encode categoricals, scale, and split
  2. Model training — train a StarlinkTransformer on the prepared data
  3. Model evaluation — compute regression metrics on the held-out test set
  4. Model validation — run a sample prediction as a sanity check

"""

import os

import kfp.compiler
from dotenv import load_dotenv
from kfp import dsl, kubernetes


load_dotenv(override=True)

kubeflow_endpoint = os.getenv("KUBEFLOW_ENDPOINT", "")
base_image = os.getenv(
    "BASE_IMAGE",
    "image-registry.openshift-image-registry.svc:5000/telco-revenue-assurance-agent/pipeline-base:latest",
)


# ---------------------------------------------------------------------------
# Pipeline components
# ---------------------------------------------------------------------------


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "scikit-learn", "numpy"],
)
def data_prep(
    dataset_file: dsl.Input[dsl.Dataset],
    x_train_file: dsl.Output[dsl.Dataset],
    x_test_file: dsl.Output[dsl.Dataset],
    y_train_file: dsl.Output[dsl.Dataset],
    y_test_file: dsl.Output[dsl.Dataset],
    scaler_file: dsl.Output[dsl.Artifact],
    label_encoders_file: dsl.Output[dsl.Artifact],
):
    """Load Starlink QoE data, encode categoricals, scale features, and split."""
    import joblib
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import LabelEncoder, StandardScaler

    df = pd.read_csv(dataset_file.path)
    print(f"Loaded dataset with {len(df)} rows")
    df.info()
    print(df.head())

    label_encoders: dict = {}
    for categorical_feature in ["season", "weather"]:
        label_encoders[categorical_feature] = LabelEncoder()
        df[f"{categorical_feature}_encoded"] = label_encoders[categorical_feature].fit_transform(
            df[categorical_feature]
        )

    features = [
        "latitude",
        "longitude",
        "elevation_m",
        "visible_satellites",
        "serving_satellites",
        "signal_loss_db",
        "season_encoded",
        "weather_encoded",
    ]
    target = "qoe_score"

    X = df[features]
    y = df[target]

    scaler = StandardScaler()
    X_scaled = pd.DataFrame(scaler.fit_transform(X), columns=features)

    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, test_size=0.2, random_state=42
    )

    print(f"Train set: {len(X_train)} rows | Test set: {len(X_test)} rows")

    X_train.to_pickle(x_train_file.path)
    X_test.to_pickle(x_test_file.path)
    y_train.to_pickle(y_train_file.path)
    y_test.to_pickle(y_test_file.path)
    joblib.dump(scaler, scaler_file.path)
    joblib.dump(label_encoders, label_encoders_file.path)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "torch", "numpy"],
)
def train_model(
    x_train_file: dsl.Input[dsl.Dataset],
    y_train_file: dsl.Input[dsl.Dataset],
    num_epochs: int,
    batch_size: int,
    learning_rate: float,
    model_file: dsl.Output[dsl.Model],
):
    """Train the StarlinkTransformer model and persist its state dict."""
    import pickle

    import numpy as np
    import pandas as pd
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    class StarlinkTransformer(nn.Module):
        def __init__(self, input_dim: int, num_heads: int = 4, dim_feedforward: int = 128, dropout: float = 0.1):
            super().__init__()
            self.transformer = nn.TransformerEncoder(
                nn.TransformerEncoderLayer(
                    d_model=input_dim,
                    nhead=num_heads,
                    dim_feedforward=dim_feedforward,
                    dropout=dropout,
                    batch_first=True,
                ),
                num_layers=2,
            )
            self.fc = nn.Linear(input_dim, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x = self.transformer(x)
            x = torch.mean(x, dim=1)
            return self.fc(x)

    X_train = pd.read_pickle(x_train_file.path)
    y_train = pd.read_pickle(y_train_file.path)

    feature_names = X_train.columns.tolist()
    input_dim = len(feature_names)

    x_tensor = torch.FloatTensor(X_train.values)
    y_tensor = torch.FloatTensor(y_train.values).reshape(-1, 1)

    train_dataset = TensorDataset(x_tensor, y_tensor)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = StarlinkTransformer(input_dim=input_dim).to(device)
    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

    print(f"Training on {device} for {num_epochs} epochs (batch_size={batch_size}, lr={learning_rate})")

    for epoch in range(num_epochs):
        model.train()
        epoch_loss = 0.0
        for batch_x, batch_y in train_loader:
            batch_x, batch_y = batch_x.to(device), batch_y.to(device)
            optimizer.zero_grad()
            outputs = model(batch_x.unsqueeze(1))
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

        if (epoch + 1) % 10 == 0:
            avg_loss = epoch_loss / len(train_loader)
            print(f"Epoch [{epoch + 1}/{num_epochs}], Loss: {avg_loss:.4f}")

    artifact = {
        "model_state_dict": model.cpu().state_dict(),
        "features": feature_names,
        "input_dim": input_dim,
        "architecture": "StarlinkTransformer",
        "hyperparameters": {
            "num_heads": 4,
            "dim_feedforward": 128,
            "dropout": 0.1,
            "num_layers": 2,
            "epochs": num_epochs,
            "batch_size": batch_size,
            "learning_rate": learning_rate,
        },
    }
    with open(model_file.path, "wb") as f:
        pickle.dump(artifact, f)

    print("Model training complete and artifact saved.")


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "torch", "numpy", "scikit-learn"],
)
def evaluate_model(
    x_test_file: dsl.Input[dsl.Dataset],
    y_test_file: dsl.Input[dsl.Dataset],
    model_file: dsl.Input[dsl.Model],
    mlpipeline_metrics_file: dsl.Output[dsl.Metrics],
):
    """Evaluate the trained model on the held-out test set and emit KFP metrics."""
    import json
    import pickle

    import numpy as np
    import pandas as pd
    import torch
    import torch.nn as nn
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    class StarlinkTransformer(nn.Module):
        def __init__(self, input_dim: int, num_heads: int = 4, dim_feedforward: int = 128, dropout: float = 0.1):
            super().__init__()
            self.transformer = nn.TransformerEncoder(
                nn.TransformerEncoderLayer(
                    d_model=input_dim,
                    nhead=num_heads,
                    dim_feedforward=dim_feedforward,
                    dropout=dropout,
                    batch_first=True,
                ),
                num_layers=2,
            )
            self.fc = nn.Linear(input_dim, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x = self.transformer(x)
            x = torch.mean(x, dim=1)
            return self.fc(x)

    X_test = pd.read_pickle(x_test_file.path)
    y_test = pd.read_pickle(y_test_file.path)

    with open(model_file.path, "rb") as f:
        artifact = pickle.load(f)

    model = StarlinkTransformer(input_dim=artifact["input_dim"])
    model.load_state_dict(artifact["model_state_dict"])
    model.eval()

    feature_names = artifact["features"]
    x_tensor = torch.FloatTensor(X_test[feature_names].values)

    with torch.no_grad():
        predictions = model(x_tensor.unsqueeze(1)).flatten().numpy()
    labels = y_test.values.astype(np.float32)

    mse = float(mean_squared_error(labels, predictions))
    rmse = float(np.sqrt(mse))
    mae = float(mean_absolute_error(labels, predictions))
    r2 = float(r2_score(labels, predictions))
    mape = float(np.mean(np.abs((labels - predictions) / np.clip(labels, 1e-8, None))) * 100)

    print("=" * 50)
    print("Starlink QoE Transformer — Evaluation Results")
    print(f"  MSE  : {mse:.4f}")
    print(f"  RMSE : {rmse:.4f}")
    print(f"  MAE  : {mae:.4f}")
    print(f"  R²   : {r2:.4f}")
    print(f"  MAPE : {mape:.2f}%")
    print("=" * 50)

    metrics = {
        "metrics": [
            {"name": "mse", "numberValue": mse, "format": "RAW"},
            {"name": "rmse", "numberValue": rmse, "format": "RAW"},
            {"name": "mae", "numberValue": mae, "format": "RAW"},
            {"name": "r2_score", "numberValue": r2, "format": "RAW"},
            {"name": "mape", "numberValue": mape / 100.0, "format": "PERCENTAGE"},
        ]
    }
    with open(mlpipeline_metrics_file.path, "w") as f:
        json.dump(metrics, f)


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "torch", "numpy", "joblib", "scikit-learn"],
)
def validate_model(
    model_file: dsl.Input[dsl.Model],
    scaler_file: dsl.Input[dsl.Artifact],
    label_encoders_file: dsl.Input[dsl.Artifact],
):
    """Sanity-check the model with a sample prediction using real-world-like inputs."""
    import pickle

    import joblib
    import numpy as np
    import torch
    import torch.nn as nn

    class StarlinkTransformer(nn.Module):
        def __init__(self, input_dim: int, num_heads: int = 4, dim_feedforward: int = 128, dropout: float = 0.1):
            super().__init__()
            self.transformer = nn.TransformerEncoder(
                nn.TransformerEncoderLayer(
                    d_model=input_dim,
                    nhead=num_heads,
                    dim_feedforward=dim_feedforward,
                    dropout=dropout,
                    batch_first=True,
                ),
                num_layers=2,
            )
            self.fc = nn.Linear(input_dim, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x = self.transformer(x)
            x = torch.mean(x, dim=1)
            return self.fc(x)

    with open(model_file.path, "rb") as f:
        artifact = pickle.load(f)

    model = StarlinkTransformer(input_dim=artifact["input_dim"])
    model.load_state_dict(artifact["model_state_dict"])
    model.eval()

    scaler = joblib.load(scaler_file.path)
    label_encoders = joblib.load(label_encoders_file.path)

    sample_data = {
        "latitude": 33.87,
        "longitude": -98.59,
        "elevation_m": 307.0,
        "visible_satellites": 15,
        "serving_satellites": 3,
        "signal_loss_db": 0.51,
        "season": "Summer",
        "weather": "Clear",
    }

    feature_names = artifact["features"]
    season_encoded = label_encoders["season"].transform([sample_data["season"]])[0]
    weather_encoded = label_encoders["weather"].transform([sample_data["weather"]])[0]

    raw_features = np.array([[
        sample_data["latitude"],
        sample_data["longitude"],
        sample_data["elevation_m"],
        sample_data["visible_satellites"],
        sample_data["serving_satellites"],
        sample_data["signal_loss_db"],
        season_encoded,
        weather_encoded,
    ]])

    features_scaled = scaler.transform(raw_features)
    x_tensor = torch.FloatTensor(features_scaled)

    with torch.no_grad():
        predicted_qoe = model(x_tensor.unsqueeze(1)).item()

    print("=" * 50)
    print("Starlink QoE Transformer — Validation (Sample Prediction)")
    print(f"  Location : ({sample_data['latitude']}, {sample_data['longitude']})")
    print(f"  Elevation: {sample_data['elevation_m']} m")
    print(f"  Season   : {sample_data['season']}")
    print(f"  Weather  : {sample_data['weather']}")
    print(f"  Predicted QoE: {predicted_qoe:.2f}")
    print("=" * 50)

    if 0.0 <= predicted_qoe <= 100.0:
        print("PASS: Predicted QoE is within the expected [0, 100] range.")
    else:
        print(f"WARNING: Predicted QoE ({predicted_qoe:.2f}) is outside [0, 100].")


@dsl.component(
    base_image=base_image,
    packages_to_install=["pandas", "torch", "numpy", "onnx", "onnxruntime", "onnxscript"],
)
def export_to_onnx(
    model_file: dsl.Input[dsl.Model],
    onnx_model_file: dsl.Output[dsl.Model],
):
    """Export the trained StarlinkTransformer from PyTorch to ONNX format."""
    import pickle

    import numpy as np
    import onnxruntime as ort
    import torch
    import torch.nn as nn

    class StarlinkTransformer(nn.Module):
        def __init__(self, input_dim: int, num_heads: int = 4, dim_feedforward: int = 128, dropout: float = 0.1):
            super().__init__()
            self.transformer = nn.TransformerEncoder(
                nn.TransformerEncoderLayer(
                    d_model=input_dim,
                    nhead=num_heads,
                    dim_feedforward=dim_feedforward,
                    dropout=dropout,
                    batch_first=True,
                ),
                num_layers=2,
            )
            self.fc = nn.Linear(input_dim, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x = self.transformer(x)
            x = torch.mean(x, dim=1)
            return self.fc(x)

    with open(model_file.path, "rb") as f:
        artifact = pickle.load(f)

    model = StarlinkTransformer(input_dim=artifact["input_dim"])
    model.load_state_dict(artifact["model_state_dict"])
    model.eval()

    input_dim = artifact["input_dim"]
    dummy_input = torch.randn(1, 1, input_dim)

    torch.onnx.export(
        model,
        dummy_input,
        onnx_model_file.path,
        export_params=True,
        opset_version=17,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={
            "input": {0: "batch_size"},
            "output": {0: "batch_size"},
        },
        dynamo=False,
    )

    with torch.no_grad():
        pytorch_output = model(dummy_input).numpy()

    session = ort.InferenceSession(onnx_model_file.path)
    onnx_output = session.run(None, {"input": dummy_input.numpy()})[0]

    max_diff = float(np.max(np.abs(pytorch_output - onnx_output)))
    print(f"ONNX export complete. Max diff vs PyTorch: {max_diff:.8f}")
    assert max_diff < 1e-5, f"ONNX output diverges from PyTorch: {max_diff}"
    print("ONNX parity check PASSED.")


@dsl.component(
    base_image=base_image,
    packages_to_install=["boto3"],
)
def upload_model_to_s3(
    onnx_model_file: dsl.Input[dsl.Model],
    scaler_file: dsl.Input[dsl.Artifact],
    label_encoders_file: dsl.Input[dsl.Artifact],
    s3_endpoint: str,
    s3_bucket: str,
    s3_model_path: str,
):
    """Upload ONNX model and preprocessing artifacts to S3-compatible storage."""
    import os

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        verify=False,
    )

    import json
    import tempfile

    settings = {
        "name": "starlink-qoe",
        "implementation": "mlserver_onnx.OnnxModel",
        "parameters": {"uri": "model.onnx"},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(settings, f, indent=2)
        settings_path = f.name

    uploads = [
        (onnx_model_file.path, f"{s3_model_path}/1/model.onnx"),
        (settings_path, f"{s3_model_path}/1/model-settings.json"),
        (scaler_file.path, f"{s3_model_path}/artifacts/scaler.joblib"),
        (label_encoders_file.path, f"{s3_model_path}/artifacts/label_encoders.joblib"),
    ]

    for local_path, s3_key in uploads:
        s3.upload_file(local_path, s3_bucket, s3_key)
        print(f"Uploaded: s3://{s3_bucket}/{s3_key}")

    for _, s3_key in uploads:
        resp = s3.head_object(Bucket=s3_bucket, Key=s3_key)
        print(f"Verified: {s3_key} ({resp['ContentLength']} bytes)")


# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------


@kfp.dsl.pipeline(
    name="Starlink QoE Prediction Pipeline",
    description="End-to-end ML pipeline for training, evaluating, exporting to ONNX, "
    "and deploying the Starlink QoE Transformer model to S3 for KServe serving.",
)
def starlink_qoe_pipeline(
    artifact_uri: str = "s3://mybucket/data/starlink_data.csv",
    num_epochs: int = 200,
    batch_size: int = 128,
    learning_rate: float = 0.001,
    s3_endpoint: str = "http://minio-service.showcase.svc:9000",
    s3_bucket: str = "mybucket",
    s3_model_path: str = "models/starlink-qoe",
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
        num_epochs=num_epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
    )

    evaluate_model(
        x_test_file=data_prep_task.outputs["x_test_file"],
        y_test_file=data_prep_task.outputs["y_test_file"],
        model_file=train_model_task.outputs["model_file"],
    )

    validate_model(
        model_file=train_model_task.outputs["model_file"],
        scaler_file=data_prep_task.outputs["scaler_file"],
        label_encoders_file=data_prep_task.outputs["label_encoders_file"],
    )

    export_to_onnx_task = export_to_onnx(
        model_file=train_model_task.outputs["model_file"],
    )

    upload_task = upload_model_to_s3(
        onnx_model_file=export_to_onnx_task.outputs["onnx_model_file"],
        scaler_file=data_prep_task.outputs["scaler_file"],
        label_encoders_file=data_prep_task.outputs["label_encoders_file"],
        s3_endpoint=s3_endpoint,
        s3_bucket=s3_bucket,
        s3_model_path=s3_model_path,
    )
    kubernetes.use_secret_as_env(
        upload_task,
        secret_name="my-data-connection",
        secret_key_to_env={
            "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
        },
    )


# ---------------------------------------------------------------------------
# CLI entry point — compile or submit the pipeline
# ---------------------------------------------------------------------------

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
    result = client.create_run_from_pipeline_func(
        starlink_qoe_pipeline,
        arguments={},
        experiment_name="starlink-qoe",
    )
    print(f"Starting pipeline run with run_id: {result.run_id}")
    client.wait_for_run_completion(result.run_id, 3600)

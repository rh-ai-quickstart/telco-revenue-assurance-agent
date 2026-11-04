"""Verify copied assets and Helm integration without importing or running pipelines."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "model-assets-verification"
MODELS = (
    ("churn", "churn", "lightgbmRuntimeName"),
    ("fraud", "rafm", "onnxRuntimeName"),
    ("qoe", "starlink-qoe", "qoeRuntimeName"),
)
PIPELINES = (
    "pipeline/01-telco-revass-randomforest-pipeline.py",
    "pipeline/03-telco-churn-kfp-pipeline.py",
    "pipeline/starlink_qoe_pipeline.py",
    "pipeline/starlink_qoe_pipeline.yaml",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def source_resource(path: str) -> dict[str, Any]:
    resource = yaml.safe_load((ROOT / path).read_text())
    if resource["kind"] == "List":
        require(len(resource["items"]) == 1, f"Expected one source resource in {path}")
        resource = resource["items"][0]
    return resource


def verify_copies() -> None:
    manifest = json.loads((ROOT / "serving/source-manifest.json").read_text())
    expected_paths = set(PIPELINES)
    for _, prefix, _ in MODELS:
        for kind in ("servingruntime", "inferenceservice"):
            expected_paths.add(f"serving/{prefix}-{kind}.yaml")
    entries = manifest["assets"]
    require(
        {entry["path"] for entry in entries} == expected_paths
        and len(entries) == len(expected_paths),
        "Source manifest must list exactly the ten copied model assets",
    )
    for entry in entries:
        path = ROOT / entry["path"]
        data = path.read_bytes()
        require(
            hashlib.sha256(data).hexdigest() == entry["sha256"],
            f"Copied source asset changed: {entry['path']}",
        )
        if path.suffix == ".py":
            ast.parse(data, filename=entry["path"])
        else:
            documents = list(yaml.safe_load_all(data))
            require(all(isinstance(doc, dict) for doc in documents), f"Invalid YAML: {path}")
    sys.stdout.write(
        f"PASS: {len(entries)} source hashes and Python/YAML parses "
        f"(source commit {manifest['source_commit']})\n"
    )


def render(
    helm: str,
    uri_overrides: dict[str, str],
    model_versions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    command = [
        helm,
        "template",
        "model-assets",
        str(ROOT / "chart"),
        "-n",
        NAMESPACE,
        "--kubeconfig",
        "/dev/null",
        "--show-only",
        "templates/model-serving-runtimes.yaml",
        "--show-only",
        "templates/model-inferenceservices.yaml",
    ]
    for key, uri in uri_overrides.items():
        command.extend(["--set-string", f"modelServing.{key}.uri={uri}"])
    for key, version in (model_versions or {}).items():
        command.extend(["--set-string", f"modelServing.{key}.modelVersion={version}"])
    result = subprocess.run(  # noqa: S603 -- local Helm rendering, never pipeline execution
        command, check=True, capture_output=True, text=True, cwd=ROOT
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def verify_rendered(
    documents: list[dict[str, Any]],
    values: dict[str, Any],
    uri_overrides: dict[str, str],
    model_versions: dict[str, str] | None = None,
) -> None:
    require(len(documents) == 6, "Expected three ServingRuntimes and three InferenceServices")
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    require(len(resources) == 6, "Rendered model resource names must be unique per kind")
    managed_fields = {"uid", "resourceVersion", "creationTimestamp", "generation", "finalizers"}
    for document in documents:
        metadata = document["metadata"]
        require(metadata.get("namespace") == NAMESPACE, "Model escaped the release namespace")
        require(not managed_fields.intersection(metadata), "Exported cluster metadata leaked")

    for key, prefix, runtime_key in MODELS:
        config = values["modelServing"][key]
        runtime_name = values["modelServing"][runtime_key]
        runtime = resources[("ServingRuntime", runtime_name)]
        source_runtime = source_resource(f"serving/{prefix}-servingruntime.yaml")
        require(
            runtime["spec"] == source_runtime["spec"],
            f"{key}: runtime behavior differs from source (env/probes/image/resources/formats)",
        )
        require(
            runtime["metadata"]["annotations"]["opendatahub.io/serving-runtime-scope"]
            == "namespace",
            f"{key}: runtime must be namespace-scoped",
        )

        service = resources[("InferenceService", config["serviceName"])]
        source_service = source_resource(f"serving/{prefix}-inferenceservice.yaml")
        annotations = service["metadata"]["annotations"]
        require(
            annotations["serving.kserve.io/deploymentMode"]
            == source_service["metadata"]["annotations"]["serving.kserve.io/deploymentMode"],
            f"{key}: source deployment mode changed",
        )
        require(
            annotations["security.opendatahub.io/enable-auth"] == "true"
            and service["metadata"]["labels"]["networking.kserve.io/visibility"] == "cluster-local",
            f"{key}: model must retain authenticated, internal access",
        )
        expected = copy.deepcopy(source_service["spec"]["predictor"])
        expected["serviceAccountName"] = config["serviceAccountName"]
        model = expected["model"]
        # Standard predictors reject model.name; MLServer receives the ISVC name
        # through KServe's defaulting webhook instead.
        model.pop("name", None)
        model["protocolVersion"] = "v2"
        model["runtime"] = runtime_name
        if key == "fraud":
            model["env"] = [{"name": "MLSERVER_MODEL_URI", "value": "/mnt/models/1/model.onnx"}]
        if key in (model_versions or {}):
            model.setdefault("env", []).insert(
                0, {"name": "MLSERVER_MODEL_VERSION", "value": model_versions[key]}
            )
        if key in uri_overrides:
            del model["storage"]
            model["storageUri"] = uri_overrides[key]
            require(
                annotations.get("serving.kserve.io/storageSecretName")
                == config["storageSecretName"],
                f"{key}: explicit URI requires its direct credential Secret",
            )
            require(
                "opendatahub.io/connections" not in annotations
                and "opendatahub.io/connection-path" not in annotations,
                f"{key}: data connection annotations conflict with explicit URI",
            )
        else:
            model["storage"]["key"] = config["storageSecretName"]
            model["storage"]["path"] = config["storagePath"]
            require(
                "serving.kserve.io/storageSecretName" not in annotations,
                f"{key}: key/path mode must use the operator's aggregate storage-config",
            )
            require(
                annotations["opendatahub.io/connections"] == config["storageSecretName"]
                and annotations["opendatahub.io/connection-path"] == model["storage"]["path"],
                f"{key}: data connection and storage path must agree",
            )
        require(
            service["spec"]["predictor"] == expected,
            f"{key}: predictor differs from source beyond approved identity/storage changes",
        )


def verify_serving_access(helm: str, values: dict[str, Any]) -> None:
    """Check the rendered OpenShift trust and least-privilege boundary."""
    command = [helm, "template", "model-assets", str(ROOT / "chart"), "-n", NAMESPACE]
    result = subprocess.run(  # noqa: S603 -- local Helm rendering only
        command, check=True, capture_output=True, text=True, cwd=ROOT
    )
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    prediction = resources[("Deployment", values["predictionTools"]["serviceName"])]
    pod = prediction["spec"]["template"]["spec"]
    account = pod["serviceAccountName"]
    require(("ServiceAccount", account) in resources, "Inference caller SA is missing")
    require(pod["automountServiceAccountToken"] is False, "Unexpected API token automount")
    container = pod["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    artifact_mount = next(
        item for item in container["volumeMounts"] if item["name"] == "feature-artifacts"
    )
    require(
        artifact_mount["mountPath"] == values["predictionTools"]["featureArtifactsMountPath"]
        and artifact_mount["readOnly"] is True
        and env["MODEL_FEATURE_MANIFEST_DIR"]["value"]
        == values["predictionTools"]["modelFeatureManifestDir"],
        "Feature artifact mount and manifest path must use their separate settings",
    )
    require(
        env["SSL_CERT_FILE"].get("value") == "/etc/telco-model-ca/service-ca.crt",
        "Inference TLS must use the service CA without disabling verification",
    )
    volume = next(item for item in pod["volumes"] if item["name"] == "model-service-ca")
    require(
        volume["configMap"]
        == {
            "name": values["predictionTools"]["modelServiceCA"]["configMapName"],
            "items": [
                {
                    "key": values["predictionTools"]["modelServiceCA"]["key"],
                    "path": "service-ca.crt",
                }
            ],
        },
        "The service CA must be required at startup",
    )
    roles = [
        doc
        for doc in documents
        if doc["kind"] == "Role" and doc["metadata"]["name"].endswith("-model-inference")
    ]
    require(len(roles) == 1, "Expected one model-inference Role")
    role = roles[0]
    model_names = [values["modelServing"][key]["serviceName"] for key, _, _ in MODELS]
    require(
        role["rules"]
        == [
            {
                "apiGroups": ["serving.kserve.io"],
                "resources": ["inferenceservices"],
                "resourceNames": model_names,
                "verbs": ["get"],
            }
        ],
        "Inference Role must grant only get on the three named models",
    )
    binding = resources[("RoleBinding", role["metadata"]["name"])]
    require(
        binding["metadata"]["namespace"] == NAMESPACE
        and role["metadata"]["namespace"] == NAMESPACE
        and binding["subjects"]
        == [{"kind": "ServiceAccount", "name": account, "namespace": NAMESPACE}]
        and binding["roleRef"]
        == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role["metadata"]["name"],
        },
        "Model authorization must bind only the namespace-local prediction caller",
    )
    for key, _, _ in MODELS:
        config = values["modelServing"][key]
        name = config["serviceName"]
        expected_url = (
            f"https://{name}-predictor.{NAMESPACE}.svc:8443/v2/models/{config['modelName']}/infer"
        )
        require(
            env[f"{key.upper()}_MODEL_URL"]["value"] == expected_url,
            f"{key}: URL must match internal service-certificate DNS and HTTPS port",
        )
        auth = values["predictionTools"]["modelAuth"][key]
        require(
            env[f"{key.upper()}_MODEL_AUTH_TOKEN"]["valueFrom"]
            == {"secretKeyRef": {"name": auth["secretName"], "key": auth["tokenKey"]}},
            f"{key}: caller token must come from the configured Secret",
        )
        secret = resources[("Secret", auth["secretName"])]
        require(
            secret["type"] == "kubernetes.io/service-account-token"
            and secret["metadata"]["namespace"] == NAMESPACE
            and secret["metadata"]["annotations"]["kubernetes.io/service-account.name"] == account
            and not secret.get("data")
            and not secret.get("stringData"),
            f"{key}: model credentials must be issued by the controller, never rendered",
        )
        policies = [
            doc
            for doc in documents
            if doc["kind"] == "NetworkPolicy"
            and doc["spec"]["podSelector"]
            .get("matchLabels", {})
            .get("serving.kserve.io/inferenceservice")
            == name
        ]
        require(len(policies) == 1, f"{key}: expected one model ingress policy")
        ingress = policies[0]["spec"]["ingress"]
        require(
            len(ingress) == 1
            and ingress[0]["ports"] == [{"protocol": "TCP", "port": 8443}]
            and ingress[0]["from"]
            == [{"podSelector": {"matchLabels": prediction["spec"]["selector"]["matchLabels"]}}],
            f"{key}: model ingress must allow only the prediction caller on 8443",
        )
    require(
        values["modelServing"]["fraud"]["storagePath"] == "models/fraud",
        "Fraud must not discover churn's model-settings.json under a shared root",
    )
    sys.stdout.write(
        "PASS: HTTPS service CA, scoped model RBAC, controller-issued tokens, "
        "and prediction-only model ingress\n"
    )
    atomic = subprocess.run(  # noqa: S603 -- local Helm rendering only
        [
            *command,
            "--set-string",
            "predictionTools.modelFeatureManifestDir=/etc/telco-models/current",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    atomic_deployment = next(
        doc
        for doc in yaml.safe_load_all(atomic.stdout)
        if doc
        and doc["kind"] == "Deployment"
        and doc["metadata"]["name"] == values["predictionTools"]["serviceName"]
    )
    atomic_container = atomic_deployment["spec"]["template"]["spec"]["containers"][0]
    require(
        next(
            item["mountPath"]
            for item in atomic_container["volumeMounts"]
            if item["name"] == "feature-artifacts"
        )
        == "/etc/telco-models"
        and next(
            item["value"]
            for item in atomic_container["env"]
            if item["name"] == "MODEL_FEATURE_MANIFEST_DIR"
        )
        == "/etc/telco-models/current",
        "Atomic bundle path must remain inside the unchanged PVC mount root",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm", help="Local Helm executable")
    args = parser.parse_args()
    verify_copies()
    values = yaml.safe_load((ROOT / "chart/values.yaml").read_text())
    verify_rendered(render(args.helm, {}), values, {})
    sys.stdout.write(
        "PASS: source runtime/predictor behavior with approved OpenShift configuration\n"
    )
    overrides = {
        key: f"s3://verification-only/{values['modelServing'][key]['storagePath']}"
        for key, _, _ in MODELS
    }
    verify_rendered(render(args.helm, overrides), values, overrides)
    verify_serving_access(args.helm, values)
    sys.stdout.write(
        "PASS: explicit URI overrides, namespace isolation, and internal authentication\n"
    )
    versions = {key: f"verified-run-{key}" for key, _, _ in MODELS}
    verify_rendered(render(args.helm, {}, versions), values, {}, versions)
    sys.stdout.write(
        "PASS: per-model version configuration preserves the runtime and model paths\n"
    )
    sys.stdout.write(
        "No pipelines executed; no cluster contact; trained artifacts remain external.\n"
    )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        SyntaxError,
        yaml.YAMLError,
        subprocess.CalledProcessError,
    ) as error:
        sys.stderr.write(f"FAIL: {error}\n")
        if isinstance(error, subprocess.CalledProcessError):
            sys.stderr.write(error.stderr or "")
        raise SystemExit(1) from error

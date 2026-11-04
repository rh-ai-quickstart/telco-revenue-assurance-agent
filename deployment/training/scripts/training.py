"""Prepare or verify the optional OpenShift training infrastructure.

Secret values stay in process memory and are passed to oc through stdin only.
Existing credentials are never rotated or overwritten by this helper.
"""

# CLI progress intentionally prints object names and never Secret values.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import base64
import json
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def command(argv: list[str], *, document: dict[str, Any] | None = None, timeout: int = 45) -> str:
    result = subprocess.run(  # noqa: S603 -- Explicit argv; no shell; credentials only on stdin.
        argv,
        input=json.dumps(document) if document is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # A server error can echo submitted objects; never relay it with Secret values.
        raise RuntimeError(
            f"Cluster command failed with exit code {result.returncode}; no credentials logged"
        )
    return result.stdout


def read_secret(oc: list[str], namespace: str, name: str) -> dict[str, str] | None:
    output = command(
        [
            *oc,
            "-n",
            namespace,
            "get",
            "secret",
            name,
            "--ignore-not-found=true",
            "-o",
            "json",
            "--request-timeout=30s",
        ]
    )
    if not output.strip():
        return None
    try:
        return {
            key: base64.b64decode(value, validate=True).decode()
            for key, value in json.loads(output).get("data", {}).items()
        }
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"Secret {name} has invalid encoded data") from error


def ensure_secret(oc: list[str], namespace: str, name: str, values: dict[str, str]) -> None:
    existing = read_secret(oc, namespace, name)
    if existing is not None:
        if any(existing.get(key) != value for key, value in values.items()):
            raise RuntimeError(
                f"Existing Secret {name} conflicts with training storage; refusing to overwrite it"
            )
        print(f"Reused Secret: {name}")
        return
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "telco-revenue-assurance-agent"},
        },
        "type": "Opaque",
        "stringData": values,
    }
    command([*oc, "-n", namespace, "create", "-f", "-", "--request-timeout=30s"], document=document)
    created = read_secret(oc, namespace, name)
    if created is None or any(created.get(key) != value for key, value in values.items()):
        raise RuntimeError(f"Secret {name} did not retain the required storage settings")
    print(f"Created Secret: {name}")


def prepare(oc: list[str], namespace: str, values: dict[str, Any]) -> None:
    storage = values["configure-pipeline"]["aws-compatible-storage"]
    name = storage["s3"]["existingSecret"]
    if not name or not storage["fullnameOverride"]:
        raise RuntimeError(
            "Training values must name an existingSecret and stable storage fullnameOverride"
        )
    existing = read_secret(oc, namespace, name)
    if existing is None:
        credentials = {
            "AWS_ACCESS_KEY_ID": secrets.token_hex(16),
            "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(32),
        }
    else:
        credentials = {
            key: existing.get(key, "") for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        }
        if not all(value.strip() for value in credentials.values()):
            raise RuntimeError(
                f"Secret {name} is missing nonempty AWS credentials; refusing to rotate it"
            )
        if (
            credentials["AWS_ACCESS_KEY_ID"] == "s4admin"
            or credentials["AWS_SECRET_ACCESS_KEY"] == "s4secret"  # noqa: S105 -- Reject defaults.
        ):
            raise RuntimeError(
                f"Secret {name} contains upstream example credentials; replace it explicitly"
            )
    endpoint = f"http://{storage['fullnameOverride']}.{namespace}.svc.cluster.local:{storage['service']['s3Port']}"
    upload_values = {
        **credentials,
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_S3_BUCKET": values["bucketBootstrap"]["modelBucket"],
        "AWS_DEFAULT_REGION": storage["s3"]["region"],
    }
    # Check all existing aliases before creating anything, so a mismatch fails cleanly.
    for alias in ("my-data-connection", "dashboard-dspa-secret"):
        current = read_secret(oc, namespace, alias)
        if current is not None and any(
            current.get(key) != value for key, value in upload_values.items()
        ):
            raise RuntimeError(
                f"Existing Secret {alias} conflicts with training storage; refusing to overwrite it"
            )
    ensure_secret(oc, namespace, name, credentials)
    for alias in ("my-data-connection", "dashboard-dspa-secret"):
        ensure_secret(oc, namespace, alias, upload_values)


def verify(
    oc: list[str], helm: list[str], namespace: str, release: str, values: dict[str, Any]
) -> None:
    status = json.loads(command([*helm, "-n", namespace, "status", release, "-o", "json"]))
    if status.get("info", {}).get("status") != "deployed":
        raise RuntimeError("Training Helm release is not deployed")
    for resource, condition in (
        (f"job/{release}-bucket-bootstrap", "Complete"),
        ("datasciencepipelinesapplication/dspa", "Ready"),
    ):
        command(
            [
                *oc,
                "-n",
                namespace,
                "wait",
                resource,
                f"--for=condition={condition}",
                "--timeout=600s",
            ],
            timeout=620,
        )
        print(f"Verified {resource}: {condition}")
    service = values["configure-pipeline"]["aws-compatible-storage"]["fullnameOverride"]
    command(
        [*oc, "-n", namespace, "rollout", "status", f"deployment/{service}", "--timeout=60s"],
        timeout=75,
    )
    route = json.loads(
        command(
            [
                *oc,
                "-n",
                namespace,
                "get",
                "route",
                "ds-pipeline-dspa",
                "-o",
                "json",
                "--request-timeout=30s",
            ]
        )
    )
    admitted = any(
        condition.get("type") == "Admitted" and condition.get("status") == "True"
        for ingress in route.get("status", {}).get("ingress", [])
        for condition in ingress.get("conditions", [])
    )
    if not admitted:
        raise RuntimeError("Training API route is not admitted")
    print(f"Training API route: https://{route['spec']['host']}")
    print(
        "Infrastructure is ready; authenticated pipeline submission and model training "
        "are separate checks."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", default="telco-training")
    parser.add_argument(
        "--values", type=Path, default=Path(__file__).resolve().parents[1] / "values.yaml"
    )
    parser.add_argument("--oc", default="oc")
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    values = yaml.safe_load(args.values.read_text())
    try:
        if args.action == "prepare":
            prepare(shlex.split(args.oc), args.namespace, values)
        else:
            verify(
                shlex.split(args.oc), shlex.split(args.helm), args.namespace, args.release, values
            )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

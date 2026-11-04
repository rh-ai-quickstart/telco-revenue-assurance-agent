"""Bounded model-serving diagnostics; no application or storage writes."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    root = Path("/opt/model-probe")
    config = json.loads((root / "config.json").read_text())
    ca_path = Path("/etc/telco-model-ca/service-ca.crt")
    tls = ssl.create_default_context(cafile=str(ca_path))
    require(
        tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED, "TLS verification required"
    )
    report: dict[str, Any] = {
        "observed_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "scope": "Live HTTPS model-serving diagnostic; no application/DB writes",
        "attempt": config["attempt"],
        "service_account": config["service_account"],
        "image": config["image"],
        "tls_verification": {
            "hostname": True,
            "certificate_chain": True,
            "service_ca_sha256": digest(ca_path.read_bytes()),
        },
        "automatic_retries": 0,
        "models": {},
    }
    with httpx.Client(
        verify=tls,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(25.0, connect=8.0),
        transport=httpx.HTTPTransport(verify=tls, retries=0),
    ) as client:
        for kind, spec in config["models"].items():
            entry: dict[str, Any] = {
                "status": "running",
                "url": spec["infer_url"],
                "expected_version": spec["model_version"],
                "checks": {},
            }
            report["models"][kind] = entry
            try:
                url = spec["infer_url"]
                parsed = urlsplit(url)
                require(
                    parsed.scheme == "https" and parsed.port == 8443,
                    "Only the chart HTTPS 8443 model endpoint is permitted",
                )
                require(
                    parsed.hostname is not None
                    and parsed.hostname.endswith("." + config["namespace"] + ".svc"),
                    "Endpoint must be in the target namespace",
                )
                require(url.endswith("/infer"), "Expected inference endpoint")
                request_bytes = (root / (kind + "-request.json")).read_bytes()
                require(digest(request_bytes) == spec["request_sha256"], "Input body hash mismatch")
                body = json.loads(request_bytes)
                require(body["inputs"][0]["shape"][0] == 100, "Probe must use exactly 100 rows")
                metadata_url = (
                    url.removesuffix("/infer")
                    + "/versions/"
                    + quote(spec["model_version"], safe="")
                )
                for check, method, target, payload in [
                    ("anonymous_metadata_denied", "GET", metadata_url, None),
                    ("anonymous_inference_denied", "POST", url, body),
                ]:
                    response = client.request(method, target, json=payload)
                    entry["checks"][check] = {
                        "status": "pass" if response.status_code in (401, 403) else "fail",
                        "http_status": response.status_code,
                        "body_sha256": digest(response.content),
                    }
                token_path = Path("/var/run/telco-model-auth") / kind / "token"
                token = token_path.read_text().strip()
                require(bool(token), "Controller token is empty")
                headers = {"Authorization": "Bearer " + token}
                response = client.get(metadata_url, headers=headers)
                entry["checks"]["authorized_versioned_metadata"] = {
                    "status": "pass" if response.status_code == 200 else "fail",
                    "http_status": response.status_code,
                    "body_sha256": digest(response.content),
                }
                require(response.status_code == 200, "Authorized version-specific metadata failed")
                metadata = response.json()
                require(metadata.get("name") == spec["model_name"], "Metadata name mismatch")
                entry["metadata"] = {
                    key: metadata[key]
                    for key in ("name", "platform", "versions", "inputs", "outputs")
                    if key in metadata
                }
                entry["metadata_version_note"] = (
                    "Version-specific metadata URL returned 200; metadata.versions describes "
                    "runtime dependencies. Inference model_version is checked separately."
                )
                response = client.post(url, headers=headers, json=body)
                entry["checks"]["authorized_inference"] = {
                    "status": "pass" if response.status_code == 200 else "fail",
                    "http_status": response.status_code,
                    "body_sha256": digest(response.content),
                }
                require(response.status_code == 200, "Authorized inference failed")
                result = response.json()
                require(result.get("id") == body["id"], "Response ID mismatch")
                require(result.get("model_name") == spec["model_name"], "Model name mismatch")
                require(
                    result.get("model_version") == spec["model_version"], "Model version mismatch"
                )
                outputs = result.get("outputs")
                require(isinstance(outputs, list) and len(outputs) == 1, "Expected one output")
                output = outputs[0]
                require(output.get("name") == spec["output_tensor_name"], "Output name mismatch")
                require(
                    output.get("datatype") == spec["output_datatype"], "Output datatype mismatch"
                )
                require(output.get("shape") == [100, 1], "Output shape mismatch")
                require(
                    output.get("parameters") == {"content_type": "np"}, "Codec parameters mismatch"
                )
                values = output.get("data")
                require(
                    isinstance(values, list) and len(values) == 100, "Output data count mismatch"
                )
                expected = spec["expected_values"]
                require(len(expected) == 100, "Local reference count mismatch")
                for value in values:
                    require(
                        type(value) in (int, float) and math.isfinite(value),
                        "Output must be finite numeric",
                    )
                    if kind == "fraud":
                        require(type(value) is int and value in (0, 1), "Invalid fraud class")
                    else:
                        require(
                            0 <= value <= (1 if kind == "churn" else 100),
                            "Output outside model range",
                        )
                close = all(
                    math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-5)
                    for a, b in zip(values, expected, strict=True)
                )
                require(close, "Live output differs from actual local runtime reference")
                entry["checks"]["wire_contract_and_reference"] = {"status": "pass"}
                entry["response"] = {
                    "id": result["id"],
                    "model_name": result["model_name"],
                    "model_version": result["model_version"],
                    "outputs": [
                        {
                            key: output[key]
                            for key in ("name", "datatype", "shape", "parameters", "data")
                        }
                    ],
                }
                entry["summary"] = {
                    "rows": len(values),
                    "minimum": min(values),
                    "maximum": max(values),
                    "maximum_absolute_difference_vs_local": max(
                        abs(a - b) for a, b in zip(values, expected, strict=True)
                    ),
                    "request_sha256": spec["request_sha256"],
                    "source_training_artifact_sha256": spec["source_training_artifact_sha256"],
                }
                if kind == "churn":
                    entry["summary"]["positive_class_count_probability_gt_half"] = sum(
                        v > 0.5 for v in values
                    )
                if kind == "fraud":
                    entry["summary"]["positive_class_count"] = sum(values)
                entry["status"] = (
                    "pass"
                    if all(check["status"] == "pass" for check in entry["checks"].values())
                    else "fail"
                )
            except Exception as error:
                entry["status"] = "fail"
                entry["error_type"] = type(error).__name__
                # Only our own fixed assertion text is safe to print. Provider exceptions
                # and response bodies are omitted to avoid logging tokens or headers.
                if type(error) is ValueError:
                    entry["error"] = str(error)
    report["status"] = (
        "pass" if all(item["status"] == "pass" for item in report["models"].values()) else "fail"
    )
    report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    text = json.dumps(report, sort_keys=True, allow_nan=False)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

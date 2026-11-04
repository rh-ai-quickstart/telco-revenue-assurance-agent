"""Verify an explicit S3 artifact bundle and atomically switch the PVC's current directory."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Protocol

FILES = frozenset(
    {"churn.json", "fraud.json", "qoe.json", "scaler.joblib", "label_encoders.joblib"}
)
MODELS = {"churn": "churn-prediction", "fraud": "fraud-prediction", "qoe": "starlink-qoe"}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024


class BundleError(Exception):
    """Safe deployment error without credentials or provider response content."""


class S3Client(Protocol):
    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BundleError(message)


def parse_manifest(data: bytes) -> dict[str, Any]:
    require(len(data) <= 65536, "Bundle manifest is too large")
    try:
        document = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise BundleError("Bundle manifest is not valid JSON") from None
    require(isinstance(document, dict), "Bundle manifest must be an object")
    require(
        set(document) == {"schema_version", "source", "models", "files"},
        "Bundle manifest has missing or unsupported fields",
    )
    require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "Unsupported bundle schema version",
    )
    source = document["source"]
    require(
        isinstance(source, dict) and set(source) == {"bucket", "prefix"},
        "An explicit source bucket and prefix are required",
    )
    require(
        isinstance(source["bucket"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", source["bucket"]) is not None,
        "Invalid source bucket",
    )
    prefix = source["prefix"]
    require(
        isinstance(prefix, str)
        and bool(prefix)
        and not prefix.startswith("/")
        and "\\" not in prefix
        and all(part not in {"", ".", ".."} for part in prefix.rstrip("/").split("/"))
        and not prefix.endswith("//"),
        "Invalid source prefix",
    )
    models = document["models"]
    require(
        isinstance(models, dict) and set(models) == set(MODELS),
        "Exactly three expected model identities are required",
    )
    for key, name in MODELS.items():
        model = models[key]
        require(
            isinstance(model, dict) and set(model) == {"model_name", "model_version"},
            "Model identity requires only model_name and model_version",
        )
        require(
            model["model_name"] == name
            and isinstance(model["model_version"], str)
            and bool(model["model_version"].strip()),
            "Unexpected model identity or empty version",
        )
    entries = document["files"]
    require(
        isinstance(entries, dict) and set(entries) == FILES,
        "Bundle must contain exactly the five required filenames",
    )
    total = 0
    for entry in entries.values():
        require(
            isinstance(entry, dict) and set(entry) == {"sha256", "size_bytes"},
            "Each artifact requires sha256 and size_bytes",
        )
        require(
            isinstance(entry["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None,
            "Invalid artifact SHA256",
        )
        require(
            type(entry["size_bytes"]) is int and 0 < entry["size_bytes"] <= MAX_FILE_BYTES,
            "Artifact size is missing or outside the supported limit",
        )
        total += entry["size_bytes"]
    require(total <= MAX_BUNDLE_BYTES, "Bundle exceeds its supported total size")
    return document


def file_digest(path: Path) -> tuple[int, str]:
    require(
        path.is_file() and not path.is_symlink(), "Artifact is missing or is not a regular file"
    )
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def verify_bundle(directory: Path, document: dict[str, Any]) -> None:
    require(
        {path.name for path in directory.iterdir()} == FILES,
        "Published directory must contain exactly the five expected files",
    )
    for name, expected in document["files"].items():
        size, digest = file_digest(directory / name)
        require(
            size == expected["size_bytes"] and digest == expected["sha256"],
            "Artifact bytes do not match the approved manifest: " + name,
        )
    # Reuse the application schema and containment checks instead of maintaining
    # a second feature contract. Trusted fitted sidecars are loaded only after all hashes pass.
    from prediction_tools.features import FeatureRegistry, ModelKind

    registry = FeatureRegistry(directory)
    try:
        for key in MODELS:
            manifest = registry.manifest_for(ModelKind(key))
            expected_model = document["models"][key]
            require(
                manifest.model_name == expected_model["model_name"]
                and manifest.model_version == expected_model["model_version"],
                "Feature manifest disagrees with the approved model identity: " + key,
            )
        registry.qoe_preprocessor(registry.manifest_for(ModelKind.QOE))
    except BundleError:
        raise
    except Exception:
        raise BundleError(
            "Feature schema or fitted preprocessing artifacts failed validation"
        ) from None


def download_file(client: S3Client, document: dict[str, Any], name: str, destination: Path) -> None:
    expected = document["files"][name]
    key = document["source"]["prefix"].rstrip("/") + "/" + name
    try:
        response = client.get_object(Bucket=document["source"]["bucket"], Key=key)
        body = response["Body"]
        try:
            require(
                response["ContentLength"] == expected["size_bytes"],
                "S3 object size differs from the approved manifest: " + name,
            )
            size = 0
            digest = hashlib.sha256()
            with destination.open("xb") as output:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    size += len(chunk)
                    require(
                        size <= expected["size_bytes"], "S3 object exceeded expected size: " + name
                    )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            require(
                size == expected["size_bytes"] and digest.hexdigest() == expected["sha256"],
                "Downloaded bytes differ from the approved manifest: " + name,
            )
            destination.chmod(0o644)
        finally:
            body.close()
    except BundleError:
        raise
    except Exception:
        raise BundleError("Cannot download the required artifact: " + name) from None


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_bundle(client: S3Client, document: dict[str, Any], destination: Path) -> dict[str, Any]:
    require(
        destination.is_dir() and not destination.is_symlink(),
        "Destination must be the PVC directory",
    )
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    identity = hashlib.sha256(encoded).hexdigest()
    generations = destination / ".generations"
    require(not generations.is_symlink(), "Generation directory must not be a symlink")
    if not generations.exists():
        generations.mkdir(mode=0o2775)
        generations.chmod(0o2775)
    generation = generations / identity
    current = destination / "current"
    lock_descriptor = os.open(
        destination / ".loader.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660
    )
    with os.fdopen(lock_descriptor, "a+b") as lock:
        os.fchmod(lock.fileno(), 0o660)
        fcntl.flock(lock, fcntl.LOCK_EX)
        require(
            not current.exists() or current.is_symlink(),
            "Existing current path is not a managed symlink",
        )
        if current.is_symlink():
            require(
                current.resolve(strict=True).parent == generations.resolve(strict=True),
                "Existing current symlink points outside managed generations",
            )
        if generation.exists():
            require(generation.is_dir() and not generation.is_symlink(), "Invalid generation path")
            verify_bundle(generation, document)
        else:
            stage = Path(tempfile.mkdtemp(prefix=".download-", dir=generations))
            stage.chmod(0o755)
            try:
                for name in sorted(FILES):
                    download_file(client, document, name, stage / name)
                verify_bundle(stage, document)
                sync_directory(stage)
                stage.rename(generation)
                sync_directory(generations)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        pointer = destination / (".current-" + uuid.uuid4().hex)
        try:
            pointer.symlink_to(Path(".generations") / identity)
            os.replace(pointer, current)
            sync_directory(destination)
        finally:
            if pointer.is_symlink():
                pointer.unlink()
        return {
            "status": "verified_and_published",
            "bundle_sha256": identity,
            "files": sorted(FILES),
            "models": document["models"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--destination", type=Path)
    action.add_argument(
        "--validate-directory",
        type=Path,
        help="Validate local actual artifacts without S3 or cluster contact",
    )
    args = parser.parse_args()
    document = parse_manifest(args.manifest.read_bytes())
    if args.validate_directory:
        verify_bundle(args.validate_directory, document)
        sys.stdout.write(
            json.dumps(
                {
                    "status": "local_bundle_verified",
                    "files": sorted(FILES),
                    "models": document["models"],
                },
                sort_keys=True,
            )
            + "\n"
        )
        return
    require(
        document["source"]["bucket"] == os.environ["AWS_S3_BUCKET"],
        "Bundle source bucket differs from the verified connection Secret",
    )
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_S3_ENDPOINT"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ["AWS_DEFAULT_REGION"],
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    sys.stdout.write(
        json.dumps(publish_bundle(client, document, args.destination), sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    try:
        main()
    except BundleError as error:
        sys.stderr.write("Artifact loading failed: " + str(error) + "\n")
        raise SystemExit(1) from None
    except Exception as error:
        sys.stderr.write(
            "Artifact loading failed: " + type(error).__name__ + "; provider details omitted\n"
        )
        raise SystemExit(1) from None

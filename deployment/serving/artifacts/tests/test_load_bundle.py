"""Synthetic unit fixtures only; no files produced here are deployment artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pytest
from sklearn.preprocessing import LabelEncoder, StandardScaler

SCRIPT = Path(__file__).parents[1] / "files/load_bundle.py"
SPEC = importlib.util.spec_from_file_location("feature_bundle_loader", SCRIPT)
assert SPEC
assert SPEC.loader
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == "unit-test-bucket"
        assert Key.startswith("unit-test-only/")
        self.calls.append(Key)
        content = self.objects[Key.rsplit("/", 1)[-1]]
        return {"Body": io.BytesIO(content), "ContentLength": len(content)}


def make_bundle(tmp_path: Path, version: str = "unit-test-one") -> tuple[dict[str, Any], FakeS3]:
    from prediction_tools.features import QOE_FEATURE_ORDER

    source = tmp_path / version
    source.mkdir()
    manifests = {
        "churn": {"feature_order": ["Tenure", "PaymentStatus"], "feature_width": 2},
        "fraud": {
            "feature_order": ["Usage", "Plan_Type_prepaid"],
            "feature_width": 2,
            "plan_type_categories": ["postpaid", "prepaid"],
            "class_order": [False, True],
        },
        "qoe": {
            "feature_order": list(QOE_FEATURE_ORDER),
            "feature_width": 8,
            "scaler_file": "scaler.joblib",
            "label_encoders_file": "label_encoders.joblib",
        },
    }
    models = {}
    for key, fields in manifests.items():
        identity = {"model_name": loader.MODELS[key], "model_version": version}
        models[key] = identity
        document = {
            **identity,
            "input_tensor_name": "unit-input",
            "output_tensor_name": "unit-output",
            **fields,
        }
        (source / (key + ".json")).write_text(json.dumps(document))
    joblib.dump(StandardScaler().fit(np.asarray([[0.0] * 8, [1.0] * 8])), source / "scaler.joblib")
    joblib.dump(
        {
            "season": LabelEncoder().fit(["winter", "summer"]),
            "weather": LabelEncoder().fit(["clear", "rain"]),
        },
        source / "label_encoders.joblib",
    )
    objects = {name: (source / name).read_bytes() for name in loader.FILES}
    document = {
        "schema_version": 1,
        "source": {"bucket": "unit-test-bucket", "prefix": "unit-test-only"},
        "models": models,
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            for name, data in objects.items()
        },
    }
    return loader.parse_manifest(json.dumps(document).encode()), FakeS3(objects)


def test_complete_bundle_is_published_together_and_reused(tmp_path: Path) -> None:
    document, client = make_bundle(tmp_path)
    volume = tmp_path / "volume"
    volume.mkdir()
    result = loader.publish_bundle(client, document, volume)
    assert result["status"] == "verified_and_published"
    assert len(client.calls) == 5
    current = volume / "current"
    assert current.is_symlink()
    assert {p.name for p in current.iterdir()} == loader.FILES
    loader.verify_bundle(current, document)
    same = loader.publish_bundle(FakeS3({}), document, volume)
    assert same == result


@pytest.mark.parametrize("failure", ["missing", "checksum", "size", "schema", "model-version"])
def test_failed_new_bundle_preserves_previous_current(tmp_path: Path, failure: str) -> None:
    old, old_client = make_bundle(tmp_path)
    volume = tmp_path / "volume"
    volume.mkdir()
    loader.publish_bundle(old_client, old, volume)
    previous = (volume / "current").readlink()
    new, client = make_bundle(tmp_path, "unit-test-two")
    if failure == "missing":
        del client.objects["scaler.joblib"]
    elif failure == "checksum":
        content = client.objects["fraud.json"]
        client.objects["fraud.json"] = b"x" + content[1:]
    elif failure == "size":
        client.objects["fraud.json"] += b"x"
    elif failure == "schema":
        invalid = json.loads(client.objects["fraud.json"])
        invalid["class_order"] = [True, False]
        content = json.dumps(invalid).encode()
        client.objects["fraud.json"] = content
        new["files"]["fraud.json"] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    else:
        new["models"]["churn"]["model_version"] = "unrelated-model-version"
    with pytest.raises(loader.BundleError):
        loader.publish_bundle(client, new, volume)
    assert (volume / "current").readlink() == previous
    loader.verify_bundle(volume / "current", old)
    assert not list((volume / ".generations").glob(".download-*"))


@pytest.mark.parametrize("filename", ["../scaler.joblib", "extra.json"])
def test_bundle_refuses_unexpected_filenames(tmp_path: Path, filename: str) -> None:
    document, _ = make_bundle(tmp_path)
    document["files"][filename] = document["files"].pop("scaler.joblib")
    with pytest.raises(loader.BundleError, match="exactly the five"):
        loader.parse_manifest(json.dumps(document).encode())


def test_expected_model_identity_is_mandatory(tmp_path: Path) -> None:
    document, _ = make_bundle(tmp_path)
    document["models"]["qoe"]["model_name"] = "another-model"
    with pytest.raises(loader.BundleError, match="Unexpected model identity"):
        loader.parse_manifest(json.dumps(document).encode())


def test_schema_rejects_boolean_size(tmp_path: Path) -> None:
    document, _ = make_bundle(tmp_path)
    document["files"]["churn.json"]["size_bytes"] = True
    with pytest.raises(loader.BundleError, match="Artifact size"):
        loader.parse_manifest(json.dumps(document).encode())


def test_sidecar_symlinks_are_refused(tmp_path: Path) -> None:
    document, _ = make_bundle(tmp_path)
    source = tmp_path / "unit-test-one"
    original = source / "scaler.joblib"
    moved = tmp_path / "outside-scaler.joblib"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(loader.BundleError, match="regular file"):
        loader.verify_bundle(source, document)


def test_unmanaged_current_directory_is_not_replaced(tmp_path: Path) -> None:
    document, client = make_bundle(tmp_path)
    volume = tmp_path / "volume"
    (volume / "current").mkdir(parents=True)
    with pytest.raises(loader.BundleError, match="not a managed symlink"):
        loader.publish_bundle(client, document, volume)
    assert not client.calls

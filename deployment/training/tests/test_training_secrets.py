from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Never

import pytest
import yaml


@pytest.fixture
def helper() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "training.py"
    spec = importlib.util.spec_from_file_location("telco_training_helper", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def values() -> dict[str, Any]:
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "values.yaml").read_text())


def fake_cluster(
    helper: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    secrets: dict[str, dict[str, str]],
) -> list[str]:
    writes = []

    def run(argv: list[str], *, document: dict[str, Any] | None = None, timeout: int = 45) -> str:
        assert argv[:3] == ["oc", "-n", "training-test"]
        if argv[3:5] == ["get", "secret"]:
            data = secrets.get(argv[5])
            return (
                ""
                if data is None
                else json.dumps(
                    {
                        "data": {
                            key: base64.b64encode(value.encode()).decode()
                            for key, value in data.items()
                        }
                    }
                )
            )
        assert argv[3:6] == ["create", "-f", "-"]
        assert document is not None
        assert document["metadata"]["namespace"] == "training-test"
        name = document["metadata"]["name"]
        assert name not in secrets
        secrets[name] = dict(document["stringData"])
        writes.append(name)
        return ""

    monkeypatch.setattr(helper, "command", run)
    return writes


def test_creation_and_repeat_preserve_credentials_without_logging(
    helper: ModuleType,
    values: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets = {}
    writes = fake_cluster(helper, monkeypatch, secrets)
    helper.prepare(["oc"], "training-test", values)
    credentials = dict(secrets["telco-training-s3"])
    assert len(writes) == 3
    assert len(credentials["AWS_ACCESS_KEY_ID"]) >= 32
    assert len(credentials["AWS_SECRET_ACCESS_KEY"]) >= 32
    helper.prepare(["oc"], "training-test", values)
    assert len(writes) == 3
    assert secrets["telco-training-s3"] == credentials
    for alias in ("my-data-connection", "dashboard-dspa-secret"):
        assert all(secrets[alias][key] == value for key, value in credentials.items())
        assert (
            secrets[alias]["AWS_S3_ENDPOINT"]
            == "http://telco-training-storage.training-test.svc.cluster.local:7480"
        )
        assert secrets[alias]["AWS_S3_BUCKET"] == "mybucket"
    output = capsys.readouterr()
    assert not any(value in output.out + output.err for value in credentials.values())


def test_conflicting_alias_is_never_overwritten(
    helper: ModuleType, values: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = {
        "AWS_ACCESS_KEY_ID": "existing-access",
        "AWS_SECRET_ACCESS_KEY": "existing-private-value",
    }
    secrets = {"my-data-connection": dict(original)}
    writes = fake_cluster(helper, monkeypatch, secrets)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        helper.prepare(["oc"], "training-test", values)
    assert writes == []
    assert secrets == {"my-data-connection": original}


@pytest.mark.parametrize(
    "credentials", [{}, {"AWS_ACCESS_KEY_ID": "s4admin", "AWS_SECRET_ACCESS_KEY": "s4secret"}]
)
def test_invalid_master_credentials_are_not_rotated(
    helper: ModuleType,
    values: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    credentials: dict[str, str],
) -> None:
    secrets = {"telco-training-s3": dict(credentials)}
    writes = fake_cluster(helper, monkeypatch, secrets)
    with pytest.raises(RuntimeError):
        helper.prepare(["oc"], "training-test", values)
    assert writes == []
    assert secrets["telco-training-s3"] == credentials


def test_failed_secret_read_cannot_trigger_creation(
    helper: ModuleType, values: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []

    def forbidden(argv: list[str], **kwargs: object) -> Never:
        commands.append(argv)
        raise RuntimeError("Cluster command failed")

    monkeypatch.setattr(helper, "command", forbidden)
    with pytest.raises(RuntimeError):
        helper.prepare(["oc"], "training-test", values)
    assert len(commands) == 1
    assert commands[0][3:5] == ["get", "secret"]


def test_failed_helm_status_is_not_reported_as_ready(
    helper: ModuleType, values: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []

    def failed(argv: list[str], **kwargs: object) -> str:
        commands.append(argv)
        return json.dumps({"info": {"status": "failed"}})

    monkeypatch.setattr(helper, "command", failed)
    with pytest.raises(RuntimeError, match="not deployed"):
        helper.verify(["oc"], ["helm"], "training-test", "telco-training", values)
    assert commands == [["helm", "-n", "training-test", "status", "telco-training", "-o", "json"]]

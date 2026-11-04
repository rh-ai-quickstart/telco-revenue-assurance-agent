"""Safety checks for the OpenShift serving credential provisioning helper."""

import argparse
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

SPEC = importlib.util.spec_from_file_location(
    "prepare_storage", Path(__file__).resolve().parents[1] / "prepare_storage.py"
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_write_success_is_never_classified_as_denied() -> None:
    with pytest.raises(RuntimeError, match="unexpectedly performed"):
        MODULE.StorageSetup.denied(Mock(return_value={}))


@pytest.mark.parametrize(("code", "status"), [("InvalidAccessKeyId", 403), ("NoSuchKey", 404)])
def test_unrelated_failure_does_not_prove_readonly(code: str, status: int) -> None:
    operation = Mock(
        side_effect=ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject"
        )
    )
    with pytest.raises(RuntimeError, match="Expected AccessDenied"):
        MODULE.StorageSetup.denied(operation)


def test_only_access_denied_is_permission_proof() -> None:
    operation = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "PutObject",
        )
    )
    assert MODULE.StorageSetup.denied(operation)["denied"] is True


def test_existing_canary_is_never_overwritten_or_deleted() -> None:
    setup = MODULE.StorageSetup(
        argparse.Namespace(
            oc="oc", namespace="test", owner="owner", serving_user="reader", bucket="test"
        )
    )
    owner = Mock()
    owner.list_objects_v2.return_value = {"Contents": [{"Key": "existing"}]}
    with pytest.raises(RuntimeError, match="collision"):
        setup.prove_permissions(owner, Mock(), {})
    owner.put_object.assert_not_called()
    owner.delete_object.assert_not_called()


def test_full_access_user_cannot_supply_credentials() -> None:
    setup = MODULE.StorageSetup(
        argparse.Namespace(oc="oc", namespace="test", owner="owner", serving_user="reader")
    )
    with pytest.raises(RuntimeError, match="exactly read-only"):
        setup.serving_keys({"user_id": "reader", "op_mask": "read, write, delete"})


def test_owner_cannot_be_selected_as_serving_identity() -> None:
    with pytest.raises(RuntimeError, match="different from the training owner"):
        MODULE.StorageSetup(
            argparse.Namespace(oc="oc", namespace="test", owner="owner", serving_user="owner")
        )


def test_policy_preserves_existing_statements_and_is_idempotent() -> None:
    setup = MODULE.StorageSetup(
        argparse.Namespace(
            oc="oc", namespace="test", owner="owner", serving_user="reader", bucket="test"
        )
    )
    existing = {"Version": "2012-10-17", "Statement": [{"Sid": "Existing", "Effect": "Deny"}]}
    merged = setup.policy_with_reader(existing)
    assert existing["Statement"] == [{"Sid": "Existing", "Effect": "Deny"}]
    assert merged["Statement"][0] == existing["Statement"][0]
    assert len(merged["Statement"]) == 3
    assert setup.policy_with_reader(merged) == merged


def test_conflicting_serving_policy_is_never_overwritten() -> None:
    setup = MODULE.StorageSetup(
        argparse.Namespace(
            oc="oc", namespace="test", owner="owner", serving_user="reader", bucket="test"
        )
    )
    existing = {
        "Version": "2012-10-17",
        "Statement": [{"Sid": "TelcoModelServingRead", "Effect": "Deny"}],
    }
    with pytest.raises(RuntimeError, match="refusing overwrite"):
        setup.policy_with_reader(existing)

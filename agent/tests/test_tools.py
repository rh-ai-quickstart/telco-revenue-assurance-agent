"""
Unit tests for agent/tools.py.

All tests mock httpx.post — no real network calls are made.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


def _ok_response(data) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    return mock


def _error_response(status: int, body: str = "error") -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.text = body
    mock.raise_for_status.side_effect = httpx.HTTPStatusError(
        "error", request=MagicMock(), response=mock
    )
    return mock


# ── fetch_customer_data ───────────────────────────────────────────────────────

class TestFetchCustomerData:
    def test_returns_api_response_on_success(self):
        from agent.tools import fetch_customer_data

        expected = {"records": [{"customer_id": "C001", "has_churned": False}], "count": 1}
        with patch("agent.tools.httpx.post", return_value=_ok_response(expected)):
            result = fetch_customer_data(["C001"])

        assert result == expected

    def test_forwards_fields_parameter(self):
        from agent.tools import fetch_customer_data

        with patch("agent.tools.httpx.post", return_value=_ok_response({})) as mock_post:
            fetch_customer_data(["C001"], fields=["customer_id", "qoe_score"])

        payload = mock_post.call_args.kwargs["json"]
        assert payload["fields"] == ["customer_id", "qoe_score"]

    def test_calls_customers_endpoint(self):
        from agent.tools import fetch_customer_data

        with patch("agent.tools.httpx.post", return_value=_ok_response({})) as mock_post:
            fetch_customer_data(["C001"])

        url = mock_post.call_args.args[0]
        assert url.endswith("/customers")

    def test_returns_error_dict_on_http_error(self):
        from agent.tools import fetch_customer_data

        with patch("agent.tools.httpx.post", return_value=_error_response(500)):
            result = fetch_customer_data(["C001"])

        assert "error" in result
        assert "500" in result["error"]

    def test_returns_error_dict_on_connection_failure(self):
        from agent.tools import fetch_customer_data

        with patch("agent.tools.httpx.post", side_effect=httpx.ConnectError("refused")):
            result = fetch_customer_data(["C001"])

        assert "error" in result


# ── predict_* tools ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn_name,expected_path", [
    ("predict_churn", "/predict/churn/batch"),
    ("predict_fraud", "/predict/fraud/batch"),
    ("predict_qoe",   "/predict/qoe/batch"),
])
class TestPredictTools:
    def test_calls_correct_endpoint(self, fn_name, expected_path):
        import agent.tools as tools

        fn = getattr(tools, fn_name)
        with patch("agent.tools.httpx.post", return_value=_ok_response([])) as mock_post:
            fn(["C001", "C002"])

        url = mock_post.call_args.args[0]
        assert url.endswith(expected_path)

    def test_forwards_customer_ids(self, fn_name, expected_path):
        import agent.tools as tools

        fn = getattr(tools, fn_name)
        with patch("agent.tools.httpx.post", return_value=_ok_response([])) as mock_post:
            fn(["C001", "C002"])

        payload = mock_post.call_args.kwargs["json"]
        assert payload["customer_ids"] == ["C001", "C002"]

    def test_returns_error_dict_on_failure(self, fn_name, expected_path):
        import agent.tools as tools

        fn = getattr(tools, fn_name)
        with patch("agent.tools.httpx.post", return_value=_error_response(503)):
            result = fn(["C001"])

        assert "error" in result

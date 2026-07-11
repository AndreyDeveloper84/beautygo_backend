"""S3C — YClientsClient (read-only pull), mocked requests.

Encodes the verified live contract (design §10):
- auth header ``Bearer <partner>, User <user>`` (both in one header)
- envelope ``{success, data, meta}``; ``success:false`` → business error
  carrying ``meta.message`` (this is how the expired-licence 403 surfaces)
- 429 / 5xx → retry with backoff; other 4xx → hard fail
- creds resolved from settings, fail-closed when partner_token / company_id
  are missing.
No network — all HTTP is mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.integrations.yclients.client import YClientsClient
from services.integrations.yclients.errors import (
    YClientsAPIError,
    YClientsBusinessError,
    YClientsConfigError,
    YClientsTimeoutError,
)


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {"success": True, "data": []}
    r.text = str(body)
    return r


@pytest.fixture
def client():
    # backoff base 0 → retries don't sleep, tests stay fast.
    return YClientsClient(
        partner_token="P", user_token="U", company_id=884045,
        retry_backoff_base=0.0,
    )


class TestConfig:
    def test_missing_partner_token_fails_closed(self):
        with pytest.raises(YClientsConfigError):
            YClientsClient(partner_token="", company_id=1)

    def test_missing_company_id_fails_closed(self):
        with pytest.raises(YClientsConfigError):
            YClientsClient(partner_token="P", company_id=0)


class TestAuthAndParsing:
    @patch("services.integrations.yclients.client.requests.get")
    def test_auth_header_both_tokens_one_header(self, mock_get, client):
        mock_get.return_value = _resp(body={"success": True, "data": []})
        client.list_services()
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer P, User U"
        assert headers["Accept"] == "application/vnd.yclients.v2+json"

    @patch("services.integrations.yclients.client.requests.get")
    def test_partner_only_header_when_no_user_token(self, mock_get):
        c = YClientsClient(partner_token="P", company_id=1, retry_backoff_base=0.0)
        mock_get.return_value = _resp(body={"success": True, "data": []})
        c.list_staff()
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer P"

    @patch("services.integrations.yclients.client.requests.get")
    def test_list_services_returns_data_list(self, mock_get, client):
        mock_get.return_value = _resp(body={"success": True, "data": [{"id": 1}, {"id": 2}]})
        assert client.list_services() == [{"id": 1}, {"id": 2}]

    @patch("services.integrations.yclients.client.requests.get")
    def test_list_services_hits_management_endpoint(self, mock_get, client):
        mock_get.return_value = _resp(body={"success": True, "data": []})
        client.list_services()
        url = mock_get.call_args.args[0]
        assert url.endswith("/company/884045/services")

    @patch("services.integrations.yclients.client.requests.get")
    def test_success_false_raises_business_error_with_message(self, mock_get, client):
        mock_get.return_value = _resp(
            status=403,
            body={"success": False, "data": None,
                  "meta": {"message": "Необходимо продлить лицензию"}},
        )
        with pytest.raises(YClientsBusinessError) as exc:
            client.list_services()
        assert "лицензию" in str(exc.value)


class TestRetries:
    @patch("services.integrations.yclients.client.requests.get")
    def test_429_retries_then_succeeds(self, mock_get, client):
        mock_get.side_effect = [
            _resp(status=429, body={"success": False}),
            _resp(body={"success": True, "data": [{"id": 9}]}),
        ]
        assert client.list_services() == [{"id": 9}]
        assert mock_get.call_count == 2

    @patch("services.integrations.yclients.client.requests.get")
    def test_5xx_exhausts_retries_then_raises(self, mock_get, client):
        mock_get.return_value = _resp(status=500, body={"success": False})
        with pytest.raises(YClientsAPIError):
            client.list_services()
        assert mock_get.call_count == client.max_retries + 1

    @patch("services.integrations.yclients.client.requests.get")
    def test_4xx_does_not_retry(self, mock_get, client):
        mock_get.return_value = _resp(status=404, body={"success": False})
        with pytest.raises(YClientsAPIError):
            client.list_services()
        assert mock_get.call_count == 1

    @patch("services.integrations.yclients.client.requests.get")
    def test_timeout_retries_then_raises(self, mock_get, client):
        import requests as _rq
        mock_get.side_effect = _rq.Timeout("slow")
        with pytest.raises(YClientsTimeoutError):
            client.list_staff()
        assert mock_get.call_count == client.max_retries + 1

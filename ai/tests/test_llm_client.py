"""Tests for ai.services.llm_client.

We avoid real network — assertions check that the SDK clients are
constructed with the expected kwargs (api_key, base_url, http_client
when proxy is set). httpx.Client / AsyncClient instantiation goes
through the real library so we catch surface-level breakage from a
future httpx version.
"""
from unittest.mock import patch

from django.test import override_settings

from ai.services.llm_client import get_async_openai_client, get_openai_client


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="", OPENAI_PROXY="")
def test_sync_no_proxy_no_base_url():
    client = get_openai_client()
    # No proxy → http_client unused; OpenAI SDK falls back to its default
    assert client.api_key == "sk-test"


@override_settings(
    OPENAI_API_KEY="sk-test",
    OPENAI_BASE_URL="https://gateway.example/openai/v1",
    OPENAI_PROXY="",
)
def test_sync_with_base_url():
    client = get_openai_client()
    assert client.api_key == "sk-test"
    assert "gateway.example" in str(client.base_url)


@override_settings(
    OPENAI_API_KEY="sk-test",
    OPENAI_BASE_URL="",
    OPENAI_PROXY="http://user:pass@proxy.example:3128",
)
def test_sync_with_proxy_passes_http_client():
    """When OPENAI_PROXY is set we must pass a configured httpx.Client.

    We patch the OpenAI ctor so we can assert on the kwargs without
    needing a network round-trip.
    """
    with patch("ai.services.llm_client.OpenAI" if False else "openai.OpenAI") as mock_openai:
        get_openai_client()
        kwargs = mock_openai.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert "http_client" in kwargs
        # httpx.Client doesn't expose .proxy publicly post-0.28; the fact
        # that it constructed without raising is the contract we care about.


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_PROXY="")
def test_async_no_proxy():
    client = get_async_openai_client()
    assert client.api_key == "sk-test"


@override_settings(
    OPENAI_API_KEY="sk-test",
    OPENAI_PROXY="http://user:pass@proxy.example:3128",
)
def test_async_with_proxy_passes_http_client():
    with patch("openai.AsyncOpenAI") as mock_openai:
        get_async_openai_client()
        kwargs = mock_openai.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert "http_client" in kwargs


@override_settings(OPENAI_API_KEY="", OPENAI_PROXY="")
def test_sync_empty_api_key_constructs_anyway():
    """Empty key is the dev/CI default — constructor must not raise so
    Django can boot in environments that don't use AI. Real failures
    happen at call time, surfaced as 401 from OpenAI."""
    client = get_openai_client()
    assert client.api_key == ""

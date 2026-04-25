"""OpenAI client factories for sync + async.

Production VPS lives in RU jurisdiction; api.openai.com is geo-blocked since
Feb 2024, so the client must tunnel through OPENAI_PROXY (HTTP/SOCKS proxy
that exits in a non-RU IP). Settings are read lazily so the module imports
without OpenAI/httpx installed in environments that don't use AI.

Pattern mirrored from mysite/agents/agents/__init__.py — kept intentionally
small (zero abstraction over the OpenAI SDK) so any new model/feature lands
without touching this layer.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings


def _build_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"api_key": settings.OPENAI_API_KEY}
    base_url = getattr(settings, "OPENAI_BASE_URL", "")
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def get_openai_client():
    from openai import OpenAI

    kwargs = _build_kwargs()
    proxy = getattr(settings, "OPENAI_PROXY", "")
    if proxy:
        import httpx

        kwargs["http_client"] = httpx.Client(proxy=proxy)
    return OpenAI(**kwargs)


def get_async_openai_client():
    from openai import AsyncOpenAI

    kwargs = _build_kwargs()
    proxy = getattr(settings, "OPENAI_PROXY", "")
    if proxy:
        import httpx

        kwargs["http_client"] = httpx.AsyncClient(proxy=proxy)
    return AsyncOpenAI(**kwargs)

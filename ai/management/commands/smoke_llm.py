"""Smoke-test the OpenAI integration end-to-end through the configured
proxy. Exits 1 on any failure so deploy hooks / CI can chain it.

Usage:
    python manage.py smoke_llm                # 5 calls, gpt-4o-mini
    python manage.py smoke_llm --calls 10
    python manage.py smoke_llm --model gpt-4o
"""
from __future__ import annotations

import statistics
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai.services.llm_client import get_openai_client


class Command(BaseCommand):
    help = "Send N small chat completions through the configured OpenAI client."

    def add_arguments(self, parser):
        parser.add_argument("--calls", type=int, default=5)
        parser.add_argument("--model", default=None)

    def handle(self, *args, calls: int, model: str | None, **options):
        if not settings.OPENAI_API_KEY:
            raise CommandError("OPENAI_API_KEY is empty — set it in .env first.")

        chosen_model = model or settings.OPENAI_MODEL
        proxy = settings.OPENAI_PROXY or "(none)"
        self.stdout.write(f"model={chosen_model} proxy={proxy} calls={calls}")

        client = get_openai_client()
        latencies: list[float] = []
        errors = 0
        total_in = total_out = 0

        for i in range(calls):
            t0 = time.monotonic()
            try:
                resp = client.chat.completions.create(
                    model=chosen_model,
                    messages=[
                        {"role": "user", "content": f"Ping {i + 1}, ответь pong."}
                    ],
                    max_tokens=5,
                )
                dt = (time.monotonic() - t0) * 1000
                latencies.append(dt)
                total_in += resp.usage.prompt_tokens
                total_out += resp.usage.completion_tokens
                reply = resp.choices[0].message.content
                self.stdout.write(f"  call {i + 1}: {dt:.0f}ms reply={reply!r}")
            except Exception as exc:  # noqa: BLE001 — surfacing every error type
                errors += 1
                self.stderr.write(f"  call {i + 1}: ERROR {type(exc).__name__}: {exc}")

        if not latencies:
            raise CommandError(f"All {calls} calls failed.")

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"summary: {len(latencies)}/{calls} ok, {errors} err"))
        self.stdout.write(f"  p50={p50:.0f}ms  p95={p95:.0f}ms  max={max(latencies):.0f}ms")
        self.stdout.write(f"  tokens: in={total_in} out={total_out}")

        if errors:
            sys.exit(1)

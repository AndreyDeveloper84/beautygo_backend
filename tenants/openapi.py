"""drf-spectacular hooks — DRF-242.8 X-Tenant header documentation.

Without this hook, the generated OpenAPI schema doesn't mention the
``X-Tenant`` header at all, so Swagger UI / ReDoc users have to discover
it from prose docs. Adding it as a global parameter makes it appear on
every operation that actually reads it (everything outside auth /
internal / health), which is also the surface where strict mode will
return 400 if the header is missing.
"""
from __future__ import annotations

from typing import Any


_TENANT_PARAMETER: dict[str, Any] = {
    "name": "X-Tenant",
    "in": "header",
    "required": False,
    "description": (
        "Tenant slug (e.g. ``ayla-marketplace``). Scopes the request to "
        "a single marketplace tenant. Optional in permissive rollout "
        "mode; required (HTTP 400 ``TENANT_REQUIRED`` otherwise) once "
        "``MULTI_TENANT_STRICT`` is enabled. Falls back to the "
        "``tenant_id`` claim in the JWT when omitted."
    ),
    "schema": {"type": "string"},
}


# Path prefixes that NEVER read the X-Tenant header (mirrors the
# middleware excluded list — keep these two in sync). Listing the header
# on these paths would mislead clients into sending it where it has no
# effect, so we skip them in the schema too.
_EXCLUDED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/",
    "/api/v1/health/",
    "/api/v1/nutrition/internal/",
)


def _path_excluded(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _EXCLUDED_PATH_PREFIXES)


def add_x_tenant_header(
    result: dict[str, Any],
    generator: Any,  # noqa: ARG001 — drf-spectacular hook signature
    request: Any,  # noqa: ARG001
    public: bool,  # noqa: ARG001
) -> dict[str, Any]:
    """Postprocessing hook — inject X-Tenant on every tenant-scoped op.

    drf-spectacular calls this with the fully-built schema dict, after
    all view introspection. We walk paths/operations and append the
    parameter where appropriate. Idempotent: if the parameter is already
    present (e.g. a view explicitly declared it via ``@extend_schema``)
    we leave the existing entry alone.
    """
    paths = result.get("paths", {})
    for path, path_item in paths.items():
        if _path_excluded(path):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method in {"parameters", "summary", "description"}:
                continue
            if not isinstance(operation, dict):
                continue
            params = operation.setdefault("parameters", [])
            if any(
                p.get("name") == "X-Tenant" and p.get("in") == "header"
                for p in params
            ):
                continue
            params.append(dict(_TENANT_PARAMETER))
    return result

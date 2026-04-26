"""Project-wide pagination defaults.

Two pieces:

1. ``DefaultPagination`` — page-number style with ``?page_size`` override,
   capped at 100. Wired in via ``REST_FRAMEWORK.DEFAULT_PAGINATION_CLASS``
   so any ``GenericViewSet`` / ``ListAPIView`` paginates without extra code.

2. ``paginated_success_response`` — helper for views that override ``list()``
   manually (we have a few — appointments, services-pro). They wrap their
   data through ``users.response.success_response``, which doesn't go
   through DRF's pagination renderer; this helper paginates first, then
   serializes, then wraps in our ``{"data": {...}}`` envelope.
"""
from __future__ import annotations

from typing import Type

from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response

from users.response import success_response


class DefaultPagination(PageNumberPagination):
    """Project default — 20 per page, ``?page_size`` override up to 100."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def paginated_success_response(
    queryset,
    serializer_class: Type,
    request: Request,
    *,
    serializer_context: dict | None = None,
    pagination_class: Type[PageNumberPagination] = DefaultPagination,
) -> Response:
    """Paginate ``queryset``, serialize, and wrap in our envelope.

    Use this in custom ``list()`` overrides that want the
    ``{"data": {results, count, page, page_size}}`` shape consistently
    with the rest of the API.
    """
    paginator = pagination_class()
    page = paginator.paginate_queryset(queryset, request)
    if page is None:
        # No pagination requested (`?page_size=0` or pagination disabled);
        # fall back to all-results, but still keep the envelope shape so
        # the mobile client doesn't branch.
        data = serializer_class(
            queryset, many=True, context=serializer_context or {}
        ).data
        return success_response(
            {
                "results": data,
                "count": len(data),
                "page": 1,
                "page_size": len(data),
            }
        )

    data = serializer_class(
        page, many=True, context=serializer_context or {}
    ).data
    return success_response(
        {
            "results": data,
            "count": paginator.page.paginator.count,
            "page": paginator.page.number,
            "page_size": paginator.get_page_size(request),
        }
    )

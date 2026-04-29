"""Favorites API — saved specialists for the client app.

Per Notion API Spec v2.0 §FAVORITES (DRF-72):

  POST   /api/v1/favorites/specialists/{id}/   → 201 {data: {added: true}}
  DELETE /api/v1/favorites/specialists/{id}/   → 204
  GET    /api/v1/favorites/specialists/        → 200 {data: [SpecialistListItem]}

Idempotency:
- POST against an already-favourited specialist returns 200 with
  ``added: true`` — the post-condition ("is it favourited?") is the
  same as a fresh add, so the mobile UI doesn't need to distinguish.
- DELETE against a non-favourited / unknown specialist returns 204 —
  the post-condition ("is it favourited?") is the same as a real
  delete; client retry on flaky network is safe.

Owner scoping:
- All three endpoints work only on the calling user's favourites; we
  never read another user's favourites list.
- DELETE on someone else's favourite is a no-op (idempotent semantics
  cover the leak — there's nothing to leak when the row never
  existed for the caller).
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from rest_framework import permissions, status
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from .models import FavoriteSpecialist, SpecialistProfile
from .permissions import IsClient, IsClientApp
from .response import error_response, success_response
from .specialists_api import SpecialistListSerializer


logger = logging.getLogger(__name__)


class FavoriteListView(GenericAPIView):
    """GET /api/v1/favorites/specialists/ — saved specialists.

    Returns SpecialistListItem shape (same as /specialists/) so the
    mobile catalog and favourites screens render with one component.
    Ordered by ``-created_at`` so the most recently saved is first.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    serializer_class = SpecialistListSerializer

    def get(self, request: Request) -> Response:
        specialists = (
            SpecialistProfile.objects
            .filter(favorited_by__user=request.user)
            .select_related("user")
            .prefetch_related("services")
            .order_by("-favorited_by__created_at")
        )
        data = SpecialistListSerializer(
            specialists, many=True, context={"request": request},
        ).data
        return success_response(data, status_code=status.HTTP_200_OK)


class FavoriteAddRemoveView(GenericAPIView):
    """POST + DELETE /api/v1/favorites/specialists/{id}/."""

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]

    def post(self, request: Request, pk) -> Response:
        # Existence check — POST against an unknown specialist still
        # surfaces 404 so the mobile UI can show "not found" instead of
        # silently swallowing.
        try:
            specialist = SpecialistProfile.objects.get(pk=pk)
        except SpecialistProfile.DoesNotExist:
            return error_response(
                "SPECIALIST_NOT_FOUND",
                "Мастер не найден",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Idempotent insert. ``IntegrityError`` from the unique
        # constraint races a concurrent POST — treat as already-added.
        try:
            with transaction.atomic():
                FavoriteSpecialist.objects.create(
                    user=request.user, specialist=specialist,
                )
            created = True
        except IntegrityError:
            created = False

        return success_response(
            {"added": True},
            status_code=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
        )

    def delete(self, request: Request, pk) -> Response:
        # Idempotent delete. We don't 404 on missing rows — POST/DELETE
        # symmetry: the post-condition is "not favourited", which is
        # already true if the row was never created.
        FavoriteSpecialist.objects.filter(
            user=request.user, specialist_id=pk,
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

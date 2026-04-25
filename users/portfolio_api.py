"""Portfolio CRUD for the Pro app (DRF-194).

The specialist manages their portfolio at /api/v1/specialists/me/portfolio/
— upload, list, delete, reorder. The public detail card surfaces the
same rows via DRF-61 (specialist detail serializer).

Caps and validation here are deliberate guardrails:

- 30-photo ceiling per specialist — keeps card-load sane and storage
  bills bounded. Enforced in the view, not the DB, so we can adjust
  without a migration.
- Allowed MIME types: image/jpeg, image/png, image/webp. Anything
  else (HEIC, AVIF, SVG) is rejected — SVG bites because the storage
  serves with text/html sometimes and that's an XSS lever.
- Hard size cap 10 MiB. The client app should compress before upload
  but the server is the only enforcer the user can't disable.
"""
from __future__ import annotations

import logging

from rest_framework import permissions, serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from .models import SpecialistPortfolio, SpecialistProfile
from .permissions import IsProApp, IsSpecialist
from .response import error_response, success_response

logger = logging.getLogger(__name__)

MAX_PHOTOS_PER_SPECIALIST = 30
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class PortfolioItemSerializer(serializers.ModelSerializer):
    """Read serializer — surfaces image_url instead of the raw FieldFile."""
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistPortfolio
        fields = ['id', 'image_url', 'sort_order', 'created_at']
        read_only_fields = ['id', 'image_url', 'created_at']

    def get_image_url(self, obj: SpecialistPortfolio) -> str:
        # ``image.url`` resolves through django-storages → S3 public URL
        # in deployed envs, /media/... in dev with FileSystemStorage.
        return obj.image.url if obj.image else ""


class PortfolioReorderSerializer(serializers.Serializer):
    """PATCH body — only sort_order is mutable."""
    sort_order = serializers.IntegerField()


def _get_specialist(user) -> SpecialistProfile:
    """Resolve the SpecialistProfile of the current Pro-app user.

    The IsSpecialist permission already guarantees ``specialist_profile``
    exists, so we don't catch DoesNotExist — surfacing a 500 there is
    correct: the request reached this view despite missing prerequisites.
    """
    return user.specialist_profile


class PortfolioListCreateView(GenericAPIView):
    """GET / POST /api/v1/specialists/me/portfolio/"""
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]
    parser_classes = [MultiPartParser, FormParser]
    serializer_class = PortfolioItemSerializer

    def get(self, request: Request) -> Response:
        specialist = _get_specialist(request.user)
        items = specialist.portfolio.all()
        data = PortfolioItemSerializer(items, many=True).data
        return success_response({"items": data})

    def post(self, request: Request) -> Response:
        specialist = _get_specialist(request.user)

        # 30-photo ceiling — checked before any file handling so we don't
        # waste an S3 PUT on a request we'll reject.
        if specialist.portfolio.count() >= MAX_PHOTOS_PER_SPECIALIST:
            return error_response(
                code="PORTFOLIO_LIMIT_EXCEEDED",
                message=f"Maximum {MAX_PHOTOS_PER_SPECIALIST} photos per specialist.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        image = request.FILES.get('image')
        if not image:
            return error_response(
                code="VALIDATION_ERROR",
                message="image field is required (multipart/form-data).",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if image.size > MAX_FILE_BYTES:
            return error_response(
                code="VALIDATION_ERROR",
                message=f"File too large. Max {MAX_FILE_BYTES // (1024 * 1024)} MiB.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if image.content_type not in ALLOWED_CONTENT_TYPES:
            return error_response(
                code="VALIDATION_ERROR",
                message=(
                    "Unsupported file type. Allowed: "
                    f"{', '.join(sorted(ALLOWED_CONTENT_TYPES))}."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        item = SpecialistPortfolio.objects.create(
            specialist=specialist,
            image=image,
        )
        logger.info(
            "portfolio.uploaded specialist_id=%s item_id=%s bytes=%d",
            specialist.id, item.id, image.size,
        )
        return success_response(
            PortfolioItemSerializer(item).data,
            status_code=status.HTTP_201_CREATED,
        )


class PortfolioDetailView(GenericAPIView):
    """DELETE / PATCH /api/v1/specialists/me/portfolio/{id}/"""
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]
    serializer_class = PortfolioItemSerializer
    lookup_field = 'pk'

    def _get_owned_or_404(self, request, pk):
        specialist = _get_specialist(request.user)
        try:
            return specialist.portfolio.get(pk=pk)
        except SpecialistPortfolio.DoesNotExist:
            return None

    def delete(self, request: Request, pk) -> Response:
        item = self._get_owned_or_404(request, pk)
        if item is None:
            return error_response(
                code="NOT_FOUND",
                message="Portfolio item not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        # Triggering ``image.delete`` removes the file from the storage
        # backend; the model row is removed by ``item.delete()`` next.
        # Order matters — once the row is gone we lose the reference.
        item.image.delete(save=False)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def patch(self, request: Request, pk) -> Response:
        item = self._get_owned_or_404(request, pk)
        if item is None:
            return error_response(
                code="NOT_FOUND",
                message="Portfolio item not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        ser = PortfolioReorderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item.sort_order = ser.validated_data['sort_order']
        item.save(update_fields=['sort_order'])
        return success_response(PortfolioItemSerializer(item).data)

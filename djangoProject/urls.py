from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

from .health import liveness, readiness
from payments.views import InternalPaymentStatusView, InternalPayoutPreviewView
from users.internal_schedule_api import (
    InternalSpecialistScheduleView,
    InternalSpecialistTimeOffView,
)


urlpatterns = [
    path('admin/', admin.site.urls),

    # API v1
    path('api/v1/auth/', include('users.urls')),
    path('api/v1/users/', include('users.users_urls')),
    path('api/v1/services/', include('services.urls')),
    path('api/v1/service-templates/', include('services.templates_urls')),
    path('api/v1/categories/', include('services.categories_urls')),
    path('api/v1/specialists/', include('users.specialists_urls')),
    path('api/v1/masters/', include('users.masters_urls')),
    path('api/v1/tenants/', include('tenants.urls')),
    path(
        'api/v1/internal/me/bookings/',
        include('appointments.records_urls'),
    ),
    # P1-3 codex audit — internal user-profile fetch for the bot's
    # user.profile.updated consumer (#446). PII §7 closed subset
    # (display_name + avatar_url only); Bearer-auth only.
    path(
        'api/v1/internal/users/',
        include('users.internal_users_urls'),
    ),
    path(
        'api/v1/internal/me/catalog/recommendations/',
        include('users.catalog_recommendations_urls'),
    ),
    # DRF-1190 — goal layer: эфемерный документ состояния (проекция
    # понимания, экран-отрисовщик) + фиксация выбора цели. Новая
    # аддитивная поверхность; поведение существующих ручек не меняется.
    path(
        'api/v1/internal/me/decision-context/',
        include('goals.urls'),
    ),
    path(
        'api/v1/internal/me/goals/select/',
        include('goals.select_urls'),
    ),
    # DRF-1344 — wellness-context read для решающего слоя бота: только
    # коды состояний (никогда значения наблюдений), fail-closed через
    # гейты wellness/services.py. Аддитивная поверхность.
    path(
        'api/v1/internal/me/wellness-context/',
        include('wellness.urls'),
    ),
    # DRF-1043 (backend half of DRF-1035) — identity read-back: lets the
    # bot learn the canonical Ayla user-id of the subject it already
    # authenticated as. `me/` is a namespace prefix here, not a resource,
    # so the endpoint hangs off `me/identity/` like `me/bookings/`.
    path(
        'api/v1/internal/me/identity/',
        include('users.internal_identity_urls'),
    ),
    # #1016 S2 — internal Bearer REST surface the Ayla bot reads/writes
    # (slots + catalog mirror + booking create/cancel/reschedule).
    # Contract co-owned with S1: ai-bot-platform/docs/architecture/.
    # C3 payout preview — explicit route BEFORE the specialists include
    # so the resolver doesn't feed "payout-preview" into the include's
    # patterns (owner: payments/, PILOT_CONTRACTS §4).
    path(
        'api/v1/internal/specialists/<uuid:specialist_id>/payout-preview/',
        InternalPayoutPreviewView.as_view(),
        name='internal-payout-preview',
    ),
    # DRF-1062 — the bot's Mini App approves a master's day-off request
    # here, because the customer picker now reads slots from Ayla: an
    # approval written into the bot's own store would change nothing a
    # client can see. Same "explicit route BEFORE the include" reason as
    # payout-preview above.
    path(
        'api/v1/internal/specialists/<uuid:specialist_id>/time-off/',
        InternalSpecialistTimeOffView.as_view(),
        name='internal-specialist-time-off',
    ),
    # DRF-1126 — the master's own schedule screen builds its days from
    # the bot's local `apps.scheduling` mirror, which lost its last
    # Ayla-syncing writer when DRF-1062 removed the invite-flow seeder.
    # The salon edits the graph, the customer's picker (on Ayla since
    # PR #1186) shows the new hours, the master's screen shows the old
    # ones, and neither side reports an error. This is the read that
    # lets the bot draw the master's day from the same source the
    # customer is being sold. Same "explicit route BEFORE the include"
    # reason as the two above.
    path(
        'api/v1/internal/specialists/<uuid:specialist_id>/schedule/',
        InternalSpecialistScheduleView.as_view(),
        name='internal-specialist-schedule',
    ),
    path(
        'api/v1/internal/specialists/',
        include('users.internal_catalog_urls'),
    ),
    path(
        'api/v1/internal/services/',
        include('services.internal_urls'),
    ),
    # S3A canonical catalog mirror (#1044 / #200) — new SalonService /
    # SpecialistService layer the bot (S3B) reads.
    path(
        'api/v1/internal/catalog/',
        include('services.internal_catalog_urls'),
    ),
    # C7.3 — on-demand payment status read model for the bot (Bearer).
    path(
        'api/v1/internal/payments/<uuid:payment_id>/',
        InternalPaymentStatusView.as_view(),
        name='internal-payment-status',
    ),
    path(
        'api/v1/internal/appointments/',
        include('appointments.internal_urls'),
    ),
    # W2 billing (P3): C2 status + card-setup for the bot (Bearer), and
    # the YooKassa webhook (IP allowlist + Basic — AMD-014: lives under
    # the internal/ prefix, which is exempt from X-App-Type).
    path("api/v1/internal/billing/", include("billing.internal_urls")),
    path("api/v1/internal/billing/", include("billing.urls")),
    path('api/v1/appointments/', include('appointments.urls')),
    path('api/v1/reviews/', include('reviews.urls')),
    path('api/v1/payments/', include('payments.urls')),
    path('api/v1/search/', include('search.urls')),
    path('api/v1/devices/', include('users.devices_urls')),
    path('api/v1/favorites/', include('users.favorites_urls', namespace='favorites')),
    path('api/v1/ai/', include('ai.urls', namespace='ai')),
    path('api/v1/nutrition/', include('nutrition.urls', namespace='nutrition')),
    path('api/v1/notifications/', include('notifications.urls', namespace='notifications')),
    path('api/v1/analytics/', include('analytics.urls', namespace='analytics')),
    path(
        'api/v1/home/',
        __import__('users.home_api', fromlist=['HomeView']).HomeView.as_view(),
        name='home',
    ),

    # Health: liveness for loadbalancer, readiness for deploy + on-call.
    path('api/v1/health/', liveness, name='health-check'),
    path('api/v1/health/ready/', readiness, name='health-ready'),

    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

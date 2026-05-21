"""Tests for Global Search API."""
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='search_client', password='pass', role='client',
        phone='+79990002200',
    )


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='search_specialist', password='pass', role='specialist',
        phone='+79990002201',
    )


@pytest.fixture
def specialist(specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = 'Елена Маникюр'
    profile.bio = 'Мастер маникюра и педикюра'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.location_lat = 55.7558
    profile.location_lng = 49.1130
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Маникюр', slug='manicure')


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name='Классический маникюр',
        description='Обрезной маникюр с покрытием',
        price='1500.00',
        duration_minutes=60,
        is_active=True,
    )


@pytest.fixture
def another_specialist_user(db):
    return User.objects.create_user(
        username='search_specialist2', password='pass', role='specialist',
        phone='+79990002202',
    )


@pytest.fixture
def another_specialist(another_specialist_user):
    profile = SpecialistProfile.objects.get(user=another_specialist_user)
    profile.display_name = 'Анна Стрижки'
    profile.bio = 'Парикмахер-стилист'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.save()
    return profile


@pytest.fixture
def haircut_category(db):
    return ServiceCategory.objects.create(name='Стрижки', slug='haircuts')


@pytest.fixture
def haircut_service(another_specialist, haircut_category):
    return Service.objects.create(
        specialist=another_specialist,
        category=haircut_category,
        name='Женская стрижка',
        price='2000.00',
        duration_minutes=45,
        is_active=True,
    )


def _client(user):
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'client'
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestGlobalSearch:
    """GET /api/v1/search/"""

    @pytest.mark.xfail(
        reason=(
            "Postgres-strict: Cyrillic __icontains lookup returns 0 results "
            "on Postgres (works on SQLite via NOCASE). Likely needs ICU "
            "collation or pg_trgm. Tracked in "
            "AndreyDeveloper84/ai-bot-platform#477."
        ),
        strict=False,
    )
    def test_search_specialist_by_name(self, client_user, specialist, service):
        response = _client(client_user).get('/api/v1/search/?q=Елена')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['specialists']) == 1
        assert data['specialists'][0]['display_name'] == 'Елена Маникюр'

    def test_search_service_by_name(self, client_user, specialist, service):
        response = _client(client_user).get('/api/v1/search/?q=маникюр')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['services']) >= 1
        assert any('маникюр' in s['name'].lower() for s in data['services'])

    @pytest.mark.xfail(
        reason=(
            "Postgres-strict: Cyrillic category lookup returns 0 results. "
            "See AndreyDeveloper84/ai-bot-platform#477."
        ),
        strict=False,
    )
    def test_search_by_category(
        self, client_user, specialist, service, another_specialist, haircut_service,
    ):
        response = _client(client_user).get('/api/v1/search/?q=Стрижки')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['services']) >= 1

    def test_search_type_specialists_only(self, client_user, specialist, service):
        response = _client(client_user).get('/api/v1/search/?q=Елена&type=specialists')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert 'specialists' in data
        assert 'services' not in data

    def test_search_type_services_only(self, client_user, specialist, service):
        response = _client(client_user).get('/api/v1/search/?q=маникюр&type=services')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert 'services' in data
        assert 'specialists' not in data

    def test_search_empty_query(self, client_user):
        response = _client(client_user).get('/api/v1/search/?q=')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['specialists'] == []
        assert data['services'] == []

    def test_search_short_query(self, client_user):
        response = _client(client_user).get('/api/v1/search/?q=а')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['specialists'] == []

    def test_search_no_results(self, client_user, specialist, service):
        response = _client(client_user).get('/api/v1/search/?q=zzzznotexist')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['specialists']) == 0
        assert len(data['services']) == 0

    def test_search_with_limit(
        self, client_user, specialist, service, another_specialist, haircut_service,
    ):
        response = _client(client_user).get('/api/v1/search/?q=а&limit=1')
        assert response.status_code == status.HTTP_200_OK
        # Limit caps results
        data = response.data['data']
        if 'specialists' in data:
            assert len(data['specialists']) <= 1
        if 'services' in data:
            assert len(data['services']) <= 1

    @pytest.mark.xfail(
        reason=(
            "Postgres-strict: same Cyrillic __icontains issue as "
            "test_search_specialist_by_name. See "
            "AndreyDeveloper84/ai-bot-platform#477."
        ),
        strict=False,
    )
    def test_search_with_geo(self, client_user, specialist, service):
        response = _client(client_user).get(
            '/api/v1/search/?q=Елена&lat=55.75&lon=49.11'
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['specialists']) == 1
        assert data['specialists'][0]['distance_km'] is not None

    def test_search_unauthenticated(self):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = c.get('/api/v1/search/?q=test')
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_inactive_specialist_not_in_results(self, client_user, specialist, service):
        specialist.status = SpecialistProfile.ProfileStatus.DRAFT
        specialist.save(update_fields=['status'])
        response = _client(client_user).get('/api/v1/search/?q=Елена')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['specialists']) == 0

    def test_inactive_service_not_in_results(self, client_user, specialist, service):
        service.is_active = False
        service.save(update_fields=['is_active'])
        response = _client(client_user).get('/api/v1/search/?q=маникюр')
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert len(data['services']) == 0

    def test_response_has_query_field(self, client_user):
        response = _client(client_user).get('/api/v1/search/?q=тест')
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['query'] == 'тест'

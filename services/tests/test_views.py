import logging

import pytest
from rest_framework import status

from services.models import Service
from users.models import User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestServiceViewSet:
    def test_specialist_can_create(self, authenticated_specialist):
        data = {
            'name': 'Консультация',
            'description': 'Описание',
            'price': '1000.00',
            'duration_minutes': 60,
        }
        logger.info("POST /api/v1/services/ — specialist creates service")
        response = authenticated_specialist.post(
            '/api/v1/services/', data,
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['name'] == 'Консультация'

    def test_client_cannot_create(self, pro_api_client):
        user = User.objects.create_user(
            username='svc_client', password='pass', role='client',
            phone='+79990100002',
        )
        pro_api_client.force_authenticate(user=user)
        response = pro_api_client.post(
            '/api/v1/services/',
            {'name': 'Test', 'price': '100', 'duration_minutes': 30},
        )
        logger.info("Client tries to create service → %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated_denied(self, pro_api_client):
        response = pro_api_client.get('/api/v1/services/')
        logger.info("Unauthenticated → %s", response.status_code)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_specialist_sees_own_services(
        self, authenticated_specialist, specialist_user,
    ):
        Service.objects.create(
            specialist=specialist_user, name='Mine',
            price='500', duration_minutes=30,
        )
        other = User.objects.create_user(
            username='other_svc_spec', password='pass',
            role='specialist', phone='+79990100099',
        )
        Service.objects.create(
            specialist=other, name='NotMine',
            price='500', duration_minutes=30,
        )
        response = authenticated_specialist.get('/api/v1/services/')
        logger.info("Specialist sees %d services", len(response.data))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Mine'

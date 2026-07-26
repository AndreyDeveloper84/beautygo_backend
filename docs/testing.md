# Testing (Ayla backend)

> Вынесено из `CLAUDE.md` (chore/slim-claude-md). Справочник — читать при написании тестов.

## Commands
```bash
make test              # All tests
make test-cov          # With coverage report
make test-fast         # Parallel execution
make test-app APP=users  # Single app
```

## Test Structure
```
apps/users/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Fixtures for this app
│   ├── test_models.py           # Model tests
│   ├── test_serializers.py      # Serializer tests
│   ├── test_views.py            # API endpoint tests
│   ├── test_services.py         # Business logic tests
│   └── factories.py             # Model factories
```

## Fixtures (conftest.py)
```python
import pytest
from rest_framework.test import APIClient
from apps.users.tests.factories import UserFactory, ClientProfileFactory

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def user():
    return UserFactory()

@pytest.fixture
def client_user():
    user = UserFactory(role="client")
    ClientProfileFactory(user=user)
    return user

@pytest.fixture
def authenticated_client(api_client, client_user):
    api_client.force_authenticate(user=client_user)
    return api_client
```

## Factories
```python
import factory
from factory.django import DjangoModelFactory
from apps.users.models import User, ClientProfile

class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    phone = factory.Sequence(lambda n: f"+7900000{n:04d}")
    first_name = factory.Faker("first_name", locale="ru_RU")
    last_name = factory.Faker("last_name", locale="ru_RU")
    role = "client"
    is_active = True
    is_verified = True

class ClientProfileFactory(DjangoModelFactory):
    class Meta:
        model = ClientProfile

    user = factory.SubFactory(UserFactory, role="client")
```

## Test Examples
```python
import pytest
from rest_framework import status
from apps.appointments.models import Appointment

@pytest.mark.django_db
class TestAppointmentCreate:
    """Test POST /api/v1/appointments/"""

    def test_create_appointment_success(self, authenticated_client, specialist, service):
        """Client can create appointment."""
        response = authenticated_client.post(
            "/api/v1/appointments/",
            data={
                "specialist_id": str(specialist.pk),
                "service_id": str(service.pk),
                "start_datetime": "2026-03-20T14:00:00Z",
            },
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert Appointment.objects.count() == 1
        appointment = Appointment.objects.first()
        assert appointment.status == "pending"
        assert appointment.specialist == specialist

    def test_create_appointment_slot_taken(self, authenticated_client, existing_appointment):
        """Cannot book already taken slot."""
        response = authenticated_client.post(
            "/api/v1/appointments/",
            data={
                "specialist_id": str(existing_appointment.specialist_id),
                "service_id": str(existing_appointment.service_id),
                "start_datetime": existing_appointment.start_datetime.isoformat(),
            },
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"]["code"] == "SLOT_NOT_AVAILABLE"

    def test_create_appointment_unauthenticated(self, api_client):
        """Unauthenticated user cannot create appointment."""
        response = api_client.post("/api/v1/appointments/", data={})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
```

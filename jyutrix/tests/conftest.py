import os
import sys
from pathlib import Path
import tempfile

os.environ["APP_MODE"] = "test"
os.environ["ADMIN_PASSWORD"] = "test-admin-password-12345"
os.environ["KAPSO_WEBHOOK_SECRET"] = "test-webhook-secret-123456789012345678901"
os.environ["KAPSO_API_KEY"] = "test-only-key"
os.environ["VOICE_TOOL_KEY"] = "test-voice-key"
os.environ["PUBLIC_URL"] = "http://testserver"
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "sqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db")
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.app.database import Base, engine


@pytest.fixture(autouse=True)
def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password-12345"},
    )
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    return client


@pytest.fixture
def clinic(admin):
    data = {
        "name": "Test Dental",
        "doctor": "Dr Test",
        "phone": "+919999000000",
        "address": "Test street, Hyderabad",
        "fee": 500,
        "enabled": True,
        "number_id": "123456789",
        "template_approved": True,
        "schedule": {
            "open": "09:00",
            "close": "19:00",
            "duration": 30,
            "closed_days": [],
            "breaks": [{"start": "13:00", "end": "14:00"}],
            "leave_dates": [],
        },
        "faqs": [{"question": "Are cards accepted?", "answer": "Yes, at reception."}],
    }
    r = admin.post("/api/clinics", json=data)
    assert r.status_code == 200, r.text
    return r.json()

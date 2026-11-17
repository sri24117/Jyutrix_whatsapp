from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from backend.main import app
from backend.app.config import settings, Settings
from backend.app.database import (
    transaction,
    Clinic,
    Appointment,
    Thread,
    Message,
    Job,
    LoginSession,
    engine,
    User,
)
from backend.app.receptionist import inbound, takeover
from backend.app.worker import tick, process_job
from backend.app.kapso import ProviderError, normalize_meta


def msg(admin, clinic, text, contact="919888777666"):
    return admin.post(
        "/api/demo/message",
        json={
            "clinic_id": clinic["id"],
            "text": text,
            "contact": contact,
            "name": "Test Patient",
        },
    )


def tomorrow_slot(clinic, h=10, m=0):
    d = datetime.now(ZoneInfo(clinic["timezone"])) + timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0).isoformat()


def manual(admin, clinic, time_value, key="manual-unique-1"):
    return admin.post(
        "/api/appointments",
        json={
            "clinic_id": clinic["id"],
            "name": "Patient",
            "contact": "+919888777666",
            "scheduled_at": time_value,
            "request_key": key,
        },
    )


def webhook_payload(clinic, message_id="wamid.1", text="BOOK", contact="919888777666"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": clinic["number_id"]},
                            "contacts": [
                                {"wa_id": contact, "profile": {"name": "Patient"}}
                            ],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": contact,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def wh(client, clinic, payload, key="delivery1", signature=None):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    sign = (
        signature
        or hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    )
    return client.post(
        f'/webhooks/kapso/{clinic["id"]}',
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sign,
            "X-Idempotency-Key": key,
        },
    )


def test_auth_required(client):
    assert client.get("/api/clinics").status_code == 401


def test_cookie_csrf(admin, clinic):
    assert (
        "HttpOnly"
        in admin.cookies.jar._cookies["testserver.local"]["/"]["jyutrix_session"]._rest
    )
    csrf = admin.headers.pop("X-CSRF-Token")
    assert admin.post("/api/threads/1/takeover").status_code == 403
    admin.headers["X-CSRF-Token"] = csrf


def test_wrong_origin(admin):
    assert (
        admin.post(
            "/api/auth/logout", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )


def test_login_rate_limit(client):
    for i in range(10):
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "bad"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": "bad"}
        ).status_code
        == 429
    )


def test_password_change_revokes_sessions(admin):
    assert (
        admin.post(
            "/api/auth/password",
            json={
                "current": "test-admin-password-12345",
                "new": "new-unique-password-12345",
            },
        ).status_code
        == 200
    )
    assert admin.get("/api/auth/me").status_code == 401
    assert (
        admin.post(
            "/api/auth/login",
            json={"username": "admin", "password": "new-unique-password-12345"},
        ).status_code
        == 200
    )


def test_booking_requires_yes(admin, clinic):
    r = msg(admin, clinic, "What appointment slots are available?")
    assert r.status_code == 200 and r.json()["state"]["step"] == "choose"
    assert len(admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()) == 0
    assert msg(admin, clinic, "1").json()["state"]["step"] == "confirm"
    assert len(admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()) == 0
    assert "Confirmed:" in msg(admin, clinic, "YES").json()["reply"]
    assert len(admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()) == 1


def test_yes_twice_no_duplicate(admin, clinic):
    msg(admin, clinic, "BOOK")
    msg(admin, clinic, "1")
    msg(admin, clinic, "YES")
    msg(admin, clinic, "YES")
    assert len(admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()) == 1


def test_manual_idempotent(admin, clinic):
    one = manual(admin, clinic, tomorrow_slot(clinic))
    two = manual(admin, clinic, tomorrow_slot(clinic))
    assert one.status_code == two.status_code == 200
    assert one.json()["id"] == two.json()["id"]


def test_overlap_grid_and_break(admin, clinic):
    assert (
        manual(admin, clinic, tomorrow_slot(clinic), key="first-key").status_code == 200
    )
    assert (
        manual(admin, clinic, tomorrow_slot(clinic), key="second-key").status_code
        == 409
    )
    assert (
        manual(
            admin, clinic, tomorrow_slot(clinic, 10, 5), key="unaligned-key"
        ).status_code
        == 409
    )
    assert (
        manual(admin, clinic, tomorrow_slot(clinic, 13), key="break-key").status_code
        == 409
    )
    assert (
        manual(
            admin, clinic, tomorrow_slot(clinic, 18, 45), key="closing-key"
        ).status_code
        == 409
    )


def test_aware_timezone_input(admin, clinic):
    assert manual(admin, clinic, tomorrow_slot(clinic)).status_code == 200


def test_cancel_confirm(admin, clinic):
    msg(admin, clinic, "BOOK")
    msg(admin, clinic, "1")
    msg(admin, clinic, "YES")
    assert "Reply YES to cancel" in msg(admin, clinic, "CANCEL").json()["reply"]
    assert (
        admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()[0]["status"]
        == "confirmed"
    )
    assert "cancelled" in msg(admin, clinic, "YES").json()["reply"]
    assert (
        admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()[0]["status"]
        == "cancelled"
    )


def test_reschedule_not_extra_booking(admin, clinic):
    msg(admin, clinic, "BOOK")
    msg(admin, clinic, "1")
    msg(admin, clinic, "YES")
    original = admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()[0]
    msg(admin, clinic, "RESCHEDULE")
    msg(admin, clinic, "2")
    msg(admin, clinic, "YES")
    rows = admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()
    assert (
        len(rows) == 1
        and rows[0]["id"] == original["id"]
        and rows[0]["starts"] != original["starts"]
    )


def test_tenant_isolation(admin, clinic):
    second = admin.post(
        "/api/clinics",
        json={
            **{k: v for k, v in clinic.items() if k != "id"},
            "name": "Second",
            "number_id": "987654321",
        },
    ).json()
    admin.post(
        "/api/users",
        json={
            "username": "staff",
            "password": "clinic-staff-password-123",
            "clinic_id": clinic["id"],
            "role": "receptionist",
        },
    )
    with TestClient(app) as staff:
        login = staff.post(
            "/api/auth/login",
            json={"username": "staff", "password": "clinic-staff-password-123"},
        )
        staff.headers["X-CSRF-Token"] = login.json()["csrf"]
        assert len(staff.get("/api/clinics").json()) == 1
        for route in ["appointments", "threads", "jobs", "reports", "availability"]:
            assert (
                staff.get(f'/api/{route}?clinic_id={second["id"]}').status_code == 404
            )
        assert (
            staff.post(
                "/api/users",
                json={
                    "username": "another",
                    "password": "a-long-password-123",
                    "clinic_id": second["id"],
                },
            ).status_code
            == 403
        )


def test_webhook_signature_and_duplicate(admin, clinic):
    p = webhook_payload(clinic)
    assert wh(admin, clinic, p, signature="bad").status_code == 401
    assert wh(admin, clinic, p).status_code == 200
    assert wh(admin, clinic, p).json()["duplicate"] is True
    assert wh(admin, clinic, p, key="different-delivery").status_code == 200
    with transaction() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "inbound")
            )
            == 1
        )


def test_webhook_wrong_number_rejected(admin, clinic):
    p = webhook_payload(clinic)
    p["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "wrong"
    assert wh(admin, clinic, p).status_code == 403


def test_inbound_worker_and_dedupe(admin, clinic, monkeypatch):
    wh(admin, clinic, webhook_payload(clinic))
    tick()
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(Thread)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == "in")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(Appointment)) == 0


def seed_outbound(clinic, text="Hello", consent=False):
    with transaction(clinic["id"]) as db:
        c = db.get(Clinic, clinic["id"])
        t, _ = inbound(
            db, c, "919888777666", "Patient", text, "seed-msg", provider_id="seed-msg"
        )
        t.consent = consent
        job = db.scalar(select(Job).where(Job.kind == "outbound"))
        return t.id, job.id


def test_takeover_suppresses_queued_bot(admin, clinic, monkeypatch):
    tid, jid = seed_outbound(clinic)
    sent = []
    monkeypatch.setattr(
        "backend.app.worker.send", lambda *args: sent.append(args) or "send1"
    )
    assert admin.post(f"/api/threads/{tid}/takeover").status_code == 200
    process_job(jid, clinic["id"])
    assert not sent
    with transaction() as db:
        assert db.get(Job, jid).status == "cancelled"
    assert msg(admin, clinic, "STAFF").json()["mode"] == "human"


def test_paused_thread_receives_without_auto_reply(admin, clinic):
    tid, jid = seed_outbound(clinic)
    admin.post(f"/api/threads/{tid}/takeover")
    with transaction(clinic["id"]) as db:
        _, reply = inbound(
            db,
            db.get(Clinic, clinic["id"]),
            "919888777666",
            "Patient",
            "BOOK",
            "after-takeover",
            provider_id="after-takeover",
        )
        assert reply is None


def test_delivery_tracking_no_regression(admin, clinic, monkeypatch):
    tid, jid = seed_outbound(clinic)
    monkeypatch.setattr("backend.app.worker.send", lambda *a: "sent-provider-id")
    process_job(jid, clinic["id"])
    value = {
        "metadata": {"phone_number_id": clinic["number_id"]},
        "statuses": [
            {
                "id": "sent-provider-id",
                "status": "read",
                "timestamp": str(int(time.time())),
            }
        ],
    }
    p = {"entry": [{"changes": [{"value": value}]}]}
    assert wh(admin, clinic, p, key="read-event").status_code == 200
    value["statuses"][0]["status"] = "sent"
    wh(admin, clinic, p, key="late-sent")
    with transaction() as db:
        assert db.get(Job, jid).status == "read"


def test_unknown_delivery_is_not_auto_retried(admin, clinic, monkeypatch):
    _, jid = seed_outbound(clinic)

    def timeout(*args):
        raise ProviderError("Unknown outcome", uncertain=True)

    monkeypatch.setattr("backend.app.worker.send", timeout)
    process_job(jid, clinic["id"])
    tick()
    with transaction() as db:
        assert db.get(Job, jid).status == "uncertain" and db.get(Job, jid).attempts == 1
    assert admin.post(f"/api/jobs/{jid}/retry", json={}).status_code == 409
    assert (
        admin.post(
            f"/api/jobs/{jid}/retry", json={"reviewed_provider_logs": True}
        ).status_code
        == 200
    )


def test_rate_limit_retry(admin, clinic, monkeypatch):
    _, jid = seed_outbound(clinic)

    def limited(*args):
        raise ProviderError("429", retry=True)

    monkeypatch.setattr("backend.app.worker.send", limited)
    process_job(jid, clinic["id"])
    with transaction() as db:
        assert db.get(Job, jid).status == "retry" and db.get(Job, jid).due > time.time()


def test_stop_cancels_reminders(admin, clinic):
    tid, _ = seed_outbound(clinic)
    with transaction(clinic["id"]) as db:
        t = db.get(Thread, tid)
        t.consent = True
        from backend.app.booking import book, as_stamp

        a = book(
            db,
            db.get(Clinic, clinic["id"]),
            t,
            as_stamp(tomorrow_slot(clinic), db.get(Clinic, clinic["id"])),
            "whatsapp-appt",
        )
        assert (
            db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "reminder")
            )
            >= 1
        )
        inbound(
            db,
            db.get(Clinic, clinic["id"]),
            t.contact,
            t.name,
            "STOP",
            "stop-1",
            provider_id="stop-1",
        )
    with transaction() as db:
        assert db.get(Thread, tid).opted_out
        assert not list(
            db.scalars(
                select(Job).where(Job.kind == "reminder", Job.status == "pending")
            )
        )


def test_reminder_template_and_consent(admin, clinic, monkeypatch):
    tid, _ = seed_outbound(clinic)
    calls = []
    monkeypatch.setattr(
        "backend.app.worker.send", lambda *args: calls.append(args) or "reminder-sent"
    )
    with transaction(clinic["id"]) as db:
        t = db.get(Thread, tid)
        t.consent = True
        from backend.app.booking import book, as_stamp

        a = book(
            db,
            db.get(Clinic, clinic["id"]),
            t,
            as_stamp(tomorrow_slot(clinic), db.get(Clinic, clinic["id"])),
            "reminder-appt",
        )
        job = db.scalar(select(Job).where(Job.kind == "reminder"))
        job.due = int(time.time()) - 1
        jid = job.id
    process_job(jid, clinic["id"])
    assert calls[0][2]["type"] == "template"
    assert len(calls[0][2]["template"]["components"][0]["parameters"]) == 4


def test_service_window_closed(admin, clinic, monkeypatch):
    tid, jid = seed_outbound(clinic)
    with transaction() as db:
        db.get(Thread, tid).last_inbound = int(time.time()) - 90000
    sent = []
    monkeypatch.setattr("backend.app.worker.send", lambda *a: sent.append(a))
    process_job(jid, clinic["id"])
    assert not sent
    with transaction() as db:
        assert db.get(Job, jid).status == "failed"


def test_medical_escalation(admin, clinic):
    r = msg(admin, clinic, "What antibiotic should I take?").json()
    assert r["mode"] == "human" and "cannot give medical advice" in r["reply"]


def test_unknown_question_escalates(admin, clinic):
    r = msg(admin, clinic, "What is your guarantee of treatment success?").json()
    assert r["mode"] == "human"


def test_demo_excluded_from_live_reports(admin, clinic):
    msg(admin, clinic, "BOOK")
    msg(admin, clinic, "1")
    msg(admin, clinic, "YES")
    report = admin.get(f'/api/reports?clinic_id={clinic["id"]}').json()
    assert (
        report["booked"]
        == report["enquiries"]
        == report["estimated_booking_value"]
        == 0
    )


def test_patient_deletion(admin, clinic):
    tid, _ = seed_outbound(clinic)
    assert admin.delete(f"/api/threads/{tid}/patient").status_code == 200
    with transaction() as db:
        assert not db.get(Thread, tid)
        assert not db.scalar(select(Message.id))
        assert not db.scalar(select(Job.id))


def test_bsuid_preserved(clinic):
    p = webhook_payload(clinic, contact="US.123456789")
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m.pop("from")
    m["business_scoped_user_id"] = "US.123456789"
    assert normalize_meta(p)[0]["contact"] == "US.123456789"


def test_clinic_settings_validation(admin, clinic):
    payload = {k: v for k, v in clinic.items() if k != "id"}
    payload["schedule"]["closed_days"] = list(range(7))
    assert admin.put(f'/api/clinics/{clinic["id"]}', json=payload).status_code == 422


def test_body_size_limit(admin):
    assert admin.post("/api/auth/login", content="x" * 262145).status_code == 413


def test_security_headers(client):
    r = client.get("/")
    assert (
        r.status_code == 200
        and "script-src 'self'" in r.headers["content-security-policy"]
    )
    assert r.headers["x-content-type-options"] == "nosniff"


def test_concurrent_booking_has_one_winner(admin, clinic):
    # API uses clinic row lock on PostgreSQL and BEGIN IMMEDIATE on SQLite.
    slot = tomorrow_slot(clinic)
    from backend.app.booking import book, as_stamp

    def attempt(i):
        try:
            with transaction(clinic["id"]) as db:
                c = db.get(Clinic, clinic["id"])
                book(
                    db,
                    c,
                    None,
                    as_stamp(slot, c),
                    f"concurrent:{i}",
                    source="staff",
                    name="Patient",
                    contact="919888777666",
                )
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(results) == 1
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(Appointment)) == 1


def test_session_survives_database_reconnect(admin):
    engine.dispose()
    assert admin.get("/api/auth/me").status_code == 200


def test_voice_request_is_not_booking(admin, clinic):
    r = admin.post(
        "/api/voice/tools",
        headers={"Authorization": "Bearer test-voice-key"},
        json={
            "clinic_id": clinic["id"],
            "action": "request",
            "contact": "+919888777666",
            "call_id": "call1",
            "scheduled_at": tomorrow_slot(clinic),
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "pending_staff_confirmation"
    with transaction() as db:
        assert not db.scalar(select(Appointment.id))


def test_production_config_fails_closed():
    with pytest.raises(RuntimeError):
        Settings(
            mode="production",
            admin_password="demo-reception-2026",
            public_url="https://clinic.example",
            database_url="postgresql://x",
            kapso_key="x",
            webhook_secret="x" * 32,
        ).validate()
    with pytest.raises(RuntimeError):
        Settings(
            mode="production",
            admin_password="",
            public_url="https://clinic.example",
            database_url="postgresql://x",
            kapso_key="x",
            webhook_secret="x" * 32,
        ).validate()
    with pytest.raises(RuntimeError):
        Settings(
            mode="production",
            admin_password="a-unique-password-123456",
            public_url="https://clinic.example",
            database_url="sqlite:///x",
            kapso_key="x",
            webhook_secret="x" * 32,
        ).validate()


def test_growth_plan_supports_doctor_specific_scheduling(admin, clinic):
    cid = clinic["id"]
    doctors = admin.get(f"/api/doctors?clinic_id={cid}").json()
    services = admin.get(f"/api/services?clinic_id={cid}").json()
    assert len(doctors) == 1 and len(services) == 1
    assert admin.get(f"/api/clinics/{cid}/entitlements").json()["plan"] == "Starter"
    blocked = admin.post(f"/api/clinics/{cid}/doctors", json={"name": "Dr Growth"})
    assert blocked.status_code == 403

    upgraded = {key: value for key, value in clinic.items() if key != "id"}
    upgraded["plan"] = "Growth"
    assert admin.put(f"/api/clinics/{cid}", json=upgraded).status_code == 200
    growth_entitlements = admin.get(f"/api/clinics/{cid}/entitlements").json()
    assert growth_entitlements["limits"]["reminder_offsets_hours"] == [24, 3]
    second = admin.post(
        f"/api/clinics/{cid}/doctors",
        json={"name": "Dr Growth", "schedule": clinic["schedule"]},
    )
    assert second.status_code == 200, second.text
    added_service = admin.post(
        f"/api/clinics/{cid}/services",
        json={"name": "Extended consultation", "duration": 60, "fee": 1500},
    )
    assert added_service.status_code == 200, added_service.text
    downgrade = {**upgraded, "plan": "Starter"}
    assert admin.put(f"/api/clinics/{cid}", json=downgrade).status_code == 409

    day = tomorrow_slot(clinic)[:10]
    first_slots = admin.get(
        f"/api/availability?clinic_id={cid}&day={day}&doctor_id={doctors[0]['id']}&service_id={services[0]['id']}"
    ).json()
    second_slots = admin.get(
        f"/api/availability?clinic_id={cid}&day={day}&doctor_id={second.json()['id']}&service_id={services[0]['id']}"
    ).json()
    assert first_slots and second_slots and first_slots[0]["starts"] == second_slots[0]["starts"]
    chosen = first_slots[0]["iso"]
    payload = {
        "clinic_id": cid,
        "name": "Patient A",
        "contact": "+919876543210",
        "scheduled_at": chosen,
        "request_key": "doctor-slot-one",
        "doctor_id": doctors[0]["id"],
        "service_id": services[0]["id"],
    }
    assert admin.post("/api/appointments", json=payload).status_code == 200
    parallel = {**payload, "name": "Patient B", "contact": "+919876543211", "request_key": "doctor-slot-two", "doctor_id": second.json()["id"]}
    assert admin.post("/api/appointments", json=parallel).status_code == 200
    duplicate = {**payload, "name": "Patient C", "contact": "+919876543212", "request_key": "doctor-slot-three"}
    assert admin.post("/api/appointments", json=duplicate).status_code == 409


def test_patient_booking_flow_selects_service_then_doctor(admin, clinic):
    cid = clinic["id"]
    updated = {key: value for key, value in clinic.items() if key != "id"}
    updated["plan"] = "Growth"
    assert admin.put(f"/api/clinics/{cid}", json=updated).status_code == 200
    doctor = admin.post(
        f"/api/clinics/{cid}/doctors",
        json={"name": "Dr Growth", "schedule": clinic["schedule"]},
    ).json()
    service = admin.post(
        f"/api/clinics/{cid}/services",
        json={"name": "Extended consultation", "duration": 60, "fee": 1500},
    ).json()

    def send(text):
        return admin.post(
            "/api/demo/message",
            json={"clinic_id": cid, "name": "Growth patient", "contact": "919123456789", "text": text},
        ).json()["reply"]

    assert "Which service" in send("BOOK")
    assert "Choose a doctor" in send("2")
    offered = send("2")
    assert "Available with Dr Growth for Extended consultation" in offered
    assert "Nothing is booked yet" in offered
    assert "Fee: Rs 1500" in send("1")
    assert "Confirmed:" in send("YES")
    appt = admin.get(f"/api/appointments?clinic_id={cid}").json()[0]
    assert appt["doctor_id"] == doctor["id"]
    assert appt["service_id"] == service["id"]
    assert appt["service_fee"] == 1500


def test_public_marketing_and_workspace_routes(client):
    landing = client.get("/")
    workspace = client.get("/app")
    assert landing.status_code == 200 and "Request a live demo" in landing.text
    assert "/static/marketing.css" in landing.text
    assert workspace.status_code == 200 and 'id="workspace"' in workspace.text


def test_website_demo_request_is_admin_managed_and_deletable(client, admin):
    payload = {
        "name": "Asha Rao",
        "clinic_name": "Lakeview Dental",
        "city": "Hyderabad",
        "phone": "+919876543210",
        "email": "asha@example.com",
        "note": "We need after-hours enquiry support.",
        "consent": True,
        "website": "",
    }
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/leads").status_code == 401
    response = client.post("/api/leads", json=payload)
    assert response.status_code == 201, response.text
    lead = admin.get("/api/leads").json()[0]
    assert lead["clinic_name"] == "Lakeview Dental"
    assert lead["status"] == "new"
    assert admin.patch(f"/api/leads/{lead['id']}", json={"status": "contacted"}).status_code == 200
    assert admin.delete(f"/api/leads/{lead['id']}").status_code == 200
    assert admin.get("/api/leads").json() == []


def test_website_honeypot_and_consent_validation(client):
    payload = {
        "name": "Bot Submission",
        "clinic_name": "Fake Clinic",
        "city": "Hyderabad",
        "phone": "+919876543210",
        "consent": True,
        "website": "https://spam.example",
    }
    assert client.post("/api/leads", json=payload).status_code == 201
    payload["website"] = ""
    payload["consent"] = False
    assert client.post("/api/leads", json=payload).status_code == 422


def test_crashed_dispatch_is_quarantined(admin, clinic, monkeypatch):
    _, jid = seed_outbound(clinic)
    with transaction() as db:
        j = db.get(Job, jid)
        j.status = "dispatching"
        j.due = int(time.time()) - 180
    calls = []
    monkeypatch.setattr("backend.app.worker.send", lambda *a: calls.append(a))
    tick()
    with transaction() as db:
        assert db.get(Job, jid).status == "uncertain"
    assert not calls


def test_manual_replay_does_not_duplicate_confirmation(admin, clinic):
    seed_outbound(clinic)
    first = manual(admin, clinic, tomorrow_slot(clinic))
    second = manual(admin, clinic, tomorrow_slot(clinic))
    assert first.status_code == second.status_code == 200
    with transaction() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.key == f'manual-confirm:{first.json()["id"]}')
            )
            == 1
        )


def test_repeated_webhook_after_processing(admin, clinic):
    p = webhook_payload(clinic)
    wh(admin, clinic, p)
    tick()
    assert (
        wh(admin, clinic, p, key="provider-redelivery-different-key").status_code == 200
    )
    with transaction() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "inbound")
            )
            == 1
        )


def test_due_reminder_cancelled_on_reschedule(admin, clinic):
    tid, _ = seed_outbound(clinic)
    from backend.app.booking import book, as_stamp, reschedule

    with transaction(clinic["id"]) as db:
        c = db.get(Clinic, clinic["id"])
        t = db.get(Thread, tid)
        t.consent = True
        a = book(db, c, t, as_stamp(tomorrow_slot(clinic), c), "resched-reminder-key")
        old = list(db.scalars(select(Job).where(Job.kind == "reminder")))
        reschedule(db, c, a, as_stamp(tomorrow_slot(clinic, 11), c))
        assert all(j.status == "cancelled" for j in old)


def test_production_demo_disabled(admin, clinic, monkeypatch):
    monkeypatch.setattr("backend.main.settings", Settings(mode="production"))
    assert msg(admin, clinic, "BOOK").status_code == 404


def test_staff_disabled_sessions_revoked(admin, clinic):
    r = admin.post(
        "/api/users",
        json={
            "username": "staff2",
            "password": "staff-password-long-123",
            "clinic_id": clinic["id"],
        },
    ).json()
    with TestClient(app) as staff:
        login = staff.post(
            "/api/auth/login",
            json={"username": "staff2", "password": "staff-password-long-123"},
        )
        assert login.status_code == 200
        assert admin.post(f'/api/users/{r["id"]}/disable').status_code == 200
        assert staff.get("/api/auth/me").status_code == 401


def test_approved_faq_exact(admin, clinic):
    assert (
        msg(admin, clinic, "Are cards accepted?").json()["reply"]
        == "Yes, at reception."
    )


def test_monthly_allowance_stops_send(admin, clinic, monkeypatch):
    tid, jid = seed_outbound(clinic)
    with transaction() as db:
        c = db.get(Clinic, clinic["id"])
        c.monthly_limit = 1
        db.add(
            Job(
                clinic_id=c.id,
                key="used-message",
                kind="outbound",
                due=int(time.time()),
                payload={},
                status="accepted",
            )
        )
    sent = []
    monkeypatch.setattr("backend.app.worker.send", lambda *a: sent.append(a))
    process_job(jid, clinic["id"])
    assert not sent
    with transaction() as db:
        assert "allowance" in db.get(Job, jid).error


def test_whatsapp_can_cancel_staff_booking(admin, clinic):
    manual(admin, clinic, tomorrow_slot(clinic))
    with transaction(clinic["id"]) as db:
        t, reply = inbound(
            db,
            db.get(Clinic, clinic["id"]),
            "919888777666",
            "Patient",
            "CANCEL",
            "cancel-staff-booking",
            provider_id="cancel-staff-booking",
        )
        assert "Reply YES to cancel" in reply
        _, reply = inbound(
            db,
            db.get(Clinic, clinic["id"]),
            t.contact,
            t.name,
            "YES",
            "cancel-staff-confirm",
            provider_id="cancel-staff-confirm",
        )
        assert "cancelled" in reply


def test_invalid_requested_date_gets_help_not_job_failure(admin, clinic):
    r = msg(admin, clinic, "BOOK 2027-99-99")
    assert r.status_code == 200 and "Invalid date" in r.json()["reply"]
    assert msg(admin, clinic, "BOOK 2099-01-01").status_code == 200


def test_staff_can_reply_when_automation_paused(admin, clinic, monkeypatch):
    tid, _ = seed_outbound(clinic)
    with transaction() as db:
        db.get(Clinic, clinic["id"]).enabled = False
    assert (
        admin.post(
            f"/api/threads/{tid}/reply", json={"text": "Reception here"}
        ).status_code
        == 200
    )
    with transaction() as db:
        jid = db.scalar(
            select(Job.id).where(Job.payload["actor"].as_string() == "staff")
        )
    sent = []
    monkeypatch.setattr(
        "backend.app.worker.send",
        lambda *args: sent.append(args) or "staff-paused-send",
    )
    process_job(jid, clinic["id"])
    assert len(sent) == 1


def test_demo_sqlite_backup_restore(admin, clinic, tmp_path):
    if engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL restoration requires pg_dump/pg_restore in staging")
    import sqlite3

    manual(admin, clinic, tomorrow_slot(clinic))
    source = sqlite3.connect(engine.url.database)
    backup = sqlite3.connect(tmp_path / "restored.db")
    try:
        source.backup(backup)
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1
    finally:
        source.close()
        backup.close()


def test_demo_slots_do_not_block_live_booking(admin, clinic):
    msg(admin, clinic, "BOOK")
    msg(admin, clinic, "1")
    msg(admin, clinic, "YES")
    demo = admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()[0]
    live_time = datetime.fromtimestamp(
        demo["starts"], ZoneInfo(clinic["timezone"])
    ).isoformat()
    assert manual(admin, clinic, live_time, key="live-same-slot-key").status_code == 200
    assert len(admin.get(f'/api/appointments?clinic_id={clinic["id"]}').json()) == 2


def test_unsupported_media_enters_staff_queue(admin, clinic):
    p = webhook_payload(clinic, text="ignored")
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m.pop("text")
    m["type"] = "audio"
    m["audio"] = {"id": "media-id"}
    assert wh(admin, clinic, p).status_code == 200
    tick()
    with transaction() as db:
        t = db.scalar(select(Thread))
        assert t.mode == "human"
        assert "staff" in t.attention.lower()


def test_kapso_text_contract(monkeypatch):
    import httpx
    from backend.app.kapso import send

    def fake(url, headers, json, timeout):
        assert url.endswith("/v24.0/123456789/messages")
        assert headers["X-API-Key"] == "test-only-key"
        assert json == {
            "messaging_product": "whatsapp",
            "to": "919888777666",
            "type": "text",
            "text": {"body": "Hello"},
        }
        return httpx.Response(200, json={"messages": [{"id": "wamid.contract"}]})

    monkeypatch.setattr("backend.app.kapso.httpx.post", fake)
    assert (
        send("123456789", "+919888777666", {"type": "text", "text": {"body": "Hello"}})
        == "wamid.contract"
    )


def test_kapso_bsuid_contract(monkeypatch):
    import httpx
    from backend.app.kapso import send

    def fake(url, headers, json, timeout):
        assert json["recipient"] == "US.123456789"
        assert json["recipient_type"] == "individual" and "to" not in json
        return httpx.Response(200, json={"messages": [{"id": "wamid.bsuid"}]})

    monkeypatch.setattr("backend.app.kapso.httpx.post", fake)
    assert (
        send("123456789", "US.123456789", {"type": "text", "text": {"body": "Hello"}})
        == "wamid.bsuid"
    )


def test_kapso_unknown_http_outcome(monkeypatch):
    import httpx
    from backend.app.kapso import send

    monkeypatch.setattr(
        "backend.app.kapso.httpx.post", lambda *a, **k: httpx.Response(503)
    )
    with pytest.raises(ProviderError) as exc:
        send("123456789", "919888777666", {"type": "text", "text": {"body": "Hello"}})
    assert exc.value.uncertain and not exc.value.retry


def set_plan(clinic_id, plan="Growth"):
    with transaction(clinic_id) as db:
        db.get(Clinic, clinic_id).plan = plan


def whatsapp_inbound(clinic_id, contact, text):
    with transaction(clinic_id) as db:
        row = db.get(Clinic, clinic_id)
        return inbound(
            db,
            row,
            contact,
            "Waitlist Patient",
            text,
            f"test-waitlist:{contact}:{text}:{time.time_ns()}",
            channel="whatsapp",
        )


def join_waitlist_patient(clinic_id, contact):
    _, prompt = whatsapp_inbound(clinic_id, contact, "WAITLIST")
    assert "Reply YES" in prompt
    _, result = whatsapp_inbound(clinic_id, contact, "YES")
    assert "on the cancellation waitlist" in result


def test_waitlist_growth_entitlement_and_explicit_consent(admin, clinic):
    assert admin.get(f'/api/waitlist?clinic_id={clinic["id"]}').status_code == 403
    set_plan(clinic["id"])
    _, prompt = whatsapp_inbound(clinic["id"], "919111111111", "WAITLIST")
    assert "Reply YES to agree" in prompt
    with transaction(clinic["id"]) as db:
        from backend.app.database import WaitlistEntry

        assert db.query(WaitlistEntry).count() == 0
    join_waitlist_patient(clinic["id"], "919111111111")
    response = admin.get(f'/api/waitlist?clinic_id={clinic["id"]}')
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["status"] == "waiting"


def test_cancellation_offer_acceptance_books_once(admin, clinic):
    from backend.app.database import WaitlistEntry

    set_plan(clinic["id"])
    candidate = "919111111112"
    join_waitlist_patient(clinic["id"], candidate)
    slot = tomorrow_slot(clinic, 10, 0)
    appointment = admin.post(
        "/api/appointments",
        json={
            "clinic_id": clinic["id"],
            "name": "Existing patient",
            "contact": "+919222222222",
            "scheduled_at": slot,
            "request_key": "waitlist-cancel-slot-1",
        },
    )
    assert appointment.status_code == 200, appointment.text
    appt_id = appointment.json()["id"]
    assert admin.patch(f"/api/appointments/{appt_id}", json={"status": "cancelled"}).status_code == 200
    with transaction(clinic["id"]) as db:
        entry = db.query(WaitlistEntry).filter_by(contact=candidate).one()
        thread = db.get(Thread, entry.thread_id)
        assert entry.status == "offered"
        assert thread.state["step"] == "waitlist_offer"
    _, reminder = whatsapp_inbound(clinic["id"], candidate, "WAITLIST")
    assert "Reply YES within the offer window" in reminder
    _, confirmation = whatsapp_inbound(clinic["id"], candidate, "YES")
    assert "Confirmed: appointment #" in confirmation
    with transaction(clinic["id"]) as db:
        entry = db.query(WaitlistEntry).filter_by(contact=candidate).one()
        bookings = db.query(Appointment).filter_by(contact=candidate, status="confirmed").all()
        assert entry.status == "booked"
        assert len(bookings) == 1


def test_expired_waitlist_offer_advances_to_next_candidate(admin, clinic):
    from backend.app.database import WaitlistEntry, Doctor, Service
    from backend.app.waitlist import offer_waitlist_for_slot, expire_waitlist_offer
    from backend.app.booking import as_stamp

    set_plan(clinic["id"])
    first, second = "919111111113", "919111111114"
    join_waitlist_patient(clinic["id"], first)
    join_waitlist_patient(clinic["id"], second)
    starts = int(datetime.fromisoformat(tomorrow_slot(clinic, 10, 0)).timestamp())
    with transaction(clinic["id"]) as db:
        row = db.get(Clinic, clinic["id"])
        doctor = db.query(Doctor).filter_by(clinic_id=row.id).first()
        service = db.query(Service).filter_by(clinic_id=row.id).first()
        offered_id = offer_waitlist_for_slot(db, row, starts, doctor.id, service.id)
        first_entry = db.get(WaitlistEntry, offered_id)
        first_entry.expires = int(time.time()) - 1
        assert expire_waitlist_offer(db, row, first_entry)
        entries = {entry.contact: entry for entry in db.query(WaitlistEntry).all()}
        assert entries[first].status == "expired"
        assert entries[second].status == "offered"
        assert db.get(Thread, entries[second].thread_id).state["waitlist_id"] == entries[second].id


def test_waitlist_opt_out_cancels_offer_and_advances(admin, clinic):
    from backend.app.database import WaitlistEntry, Doctor, Service
    from backend.app.waitlist import offer_waitlist_for_slot
    from backend.app.booking import as_stamp

    set_plan(clinic["id"])
    first, second = "919111111115", "919111111116"
    join_waitlist_patient(clinic["id"], first)
    join_waitlist_patient(clinic["id"], second)
    starts = int(datetime.fromisoformat(tomorrow_slot(clinic, 10, 0)).timestamp())
    with transaction(clinic["id"]) as db:
        row = db.get(Clinic, clinic["id"])
        doctor = db.query(Doctor).filter_by(clinic_id=row.id).first()
        service = db.query(Service).filter_by(clinic_id=row.id).first()
        offer_waitlist_for_slot(db, row, starts, doctor.id, service.id)
    _, reply = whatsapp_inbound(clinic["id"], first, "STOP")
    assert "messages are stopped" in reply
    with transaction(clinic["id"]) as db:
        entries = {entry.contact: entry for entry in db.query(WaitlistEntry).all()}
        assert entries[first].status == "cancelled"
        assert entries[second].status == "offered"
        assert db.get(Thread, entries[first].thread_id).opted_out


def test_demo_waitlist_consent_does_not_create_real_entry(admin, clinic):
    from backend.app.database import WaitlistEntry

    set_plan(clinic["id"])
    prompt = msg(admin, clinic, "WAITLIST", "919111111117").json()["reply"]
    assert "Reply YES" in prompt
    reply = msg(admin, clinic, "YES", "919111111117").json()["reply"]
    assert "demo has not added you to a real waitlist" in reply
    with transaction(clinic["id"]) as db:
        assert db.query(WaitlistEntry).count() == 0


def prepare_button_reminder(clinic, monkeypatch):
    from backend.app.booking import book, as_stamp
    from backend.app.kapso import verify_reminder_action_token

    thread_id, _ = seed_outbound(clinic, "Hello", consent=True)
    sent = []
    monkeypatch.setattr(
        "backend.app.worker.send", lambda *args: sent.append(args) or "buttons-sent"
    )
    with transaction(clinic["id"]) as db:
        row = db.get(Clinic, clinic["id"])
        row.reminder_buttons_approved = True
        thread = db.get(Thread, thread_id)
        appointment = book(
            db,
            row,
            thread,
            as_stamp(tomorrow_slot(clinic), row),
            "buttons-appt-key",
            consent=True,
        )
        reminder = db.scalar(
            select(Job).where(Job.kind == "reminder").order_by(Job.id)
        )
        reminder.due = int(time.time()) - 1
        appointment_id = appointment.id
    process_job(reminder.id, clinic["id"])
    template = sent[0][2]["template"]
    assert template["name"] == "jyutrix_appointment_reminder_buttons"
    components = template["components"]
    buttons = [component for component in components if component["type"] == "button"]
    assert len(buttons) == 3
    tokens = [component["parameters"][0]["payload"] for component in buttons]
    actions = [verify_reminder_action_token(token)["action"] for token in tokens]
    assert actions == ["confirm", "reschedule", "cancel"]
    return thread_id, appointment_id, dict(zip(actions, tokens))


def button_inbound(clinic_id, thread_id, text, token):
    with transaction(clinic_id) as db:
        clinic_row = db.get(Clinic, clinic_id)
        thread = db.get(Thread, thread_id)
        return inbound(
            db,
            clinic_row,
            thread.contact,
            thread.name,
            text,
            f"button-event:{thread_id}:{time.time_ns()}",
            provider_id=f"button-provider:{thread_id}:{time.time_ns()}",
            button_payload=token,
        )


def test_signed_reminder_confirm_button_records_only_matching_appointment(admin, clinic, monkeypatch):
    from backend.app.kapso import make_reminder_action_token

    thread_id, appt_id, tokens = prepare_button_reminder(clinic, monkeypatch)
    _, reply = button_inbound(clinic["id"], thread_id, "Confirm", tokens["confirm"])
    assert "is confirmed for" in reply
    with transaction(clinic["id"]) as db:
        appointment = db.get(Appointment, appt_id)
        assert appointment.status == "confirmed"
        assert appointment.reminder_response == "confirmed"
        invalid = make_reminder_action_token(clinic["id"], appt_id + 999, appointment.starts, "cancel")
    _, invalid_reply = button_inbound(clinic["id"], thread_id, "Cancel", invalid)
    assert "no longer active" in invalid_reply
    tampered = tokens["cancel"][:-1] + ("0" if tokens["cancel"][-1] != "0" else "1")
    _, tampered_reply = button_inbound(clinic["id"], thread_id, "Cancel", tampered)
    assert "invalid or expired" in tampered_reply
    with transaction(clinic["id"]) as db:
        assert db.get(Appointment, appt_id).status == "confirmed"


def test_signed_reminder_cancel_requires_second_confirmation(admin, clinic, monkeypatch):
    thread_id, appt_id, tokens = prepare_button_reminder(clinic, monkeypatch)
    _, prompt = button_inbound(clinic["id"], thread_id, "Cancel", tokens["cancel"])
    assert "Reply YES to cancel" in prompt
    with transaction(clinic["id"]) as db:
        assert db.get(Appointment, appt_id).status == "confirmed"
    _, result = whatsapp_inbound(clinic["id"], "919888777666", "YES")
    assert "cancelled" in result
    with transaction(clinic["id"]) as db:
        appointment = db.get(Appointment, appt_id)
        assert appointment.status == "cancelled"
        assert appointment.reminder_response == "cancelled"


def test_signed_reminder_reschedule_button_runs_existing_booking_checks(admin, clinic, monkeypatch):
    thread_id, appt_id, tokens = prepare_button_reminder(clinic, monkeypatch)
    _, prompt = button_inbound(clinic["id"], thread_id, "Reschedule", tokens["reschedule"])
    assert "preferred date as YYYY-MM-DD" in prompt
    new_date = (datetime.now(ZoneInfo(clinic["timezone"])).date() + timedelta(days=2)).isoformat()
    _, choices = whatsapp_inbound(clinic["id"], "919888777666", new_date)
    assert "Available with" in choices
    _, selected = whatsapp_inbound(clinic["id"], "919888777666", "1")
    assert "Reply YES to confirm" in selected
    _, confirmation = whatsapp_inbound(clinic["id"], "919888777666", "YES")
    assert "Confirmed: appointment #" in confirmation
    with transaction(clinic["id"]) as db:
        appointment = db.get(Appointment, appt_id)
        assert appointment.starts != int(datetime.fromisoformat(tomorrow_slot(clinic)).timestamp())
        assert appointment.reminder_response == "rescheduled"


def test_webhook_worker_preserves_signed_quick_reply_payload(admin, clinic, monkeypatch):
    thread_id, appt_id, tokens = prepare_button_reminder(clinic, monkeypatch)
    payload = webhook_payload(
        clinic,
        message_id="wamid.quick-reply-confirm",
        text="Confirm",
        contact="919888777666",
    )
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "interactive"
    message["interactive"] = {
        "type": "button_reply",
        "button_reply": {
            "id": "confirm",
            "title": "Confirm",
            "payload": tokens["confirm"],
        },
    }
    response = wh(admin, clinic, payload, key="quick-reply-delivery")
    assert response.status_code == 200, response.text
    with transaction(clinic["id"]) as db:
        job = db.scalar(
            select(Job)
            .where(Job.kind == "inbound", Job.payload["provider_id"].as_string() == "wamid.quick-reply-confirm")
            .order_by(Job.id.desc())
        )
        assert job
        job_id = job.id
    process_job(job_id, clinic["id"])
    with transaction(clinic["id"]) as db:
        appointment = db.get(Appointment, appt_id)
        assert appointment.reminder_response == "confirmed"
        assert db.get(Thread, thread_id)

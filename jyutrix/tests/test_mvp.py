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

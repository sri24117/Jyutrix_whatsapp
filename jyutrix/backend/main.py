from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import date, datetime, timezone
from uuid import uuid4
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlsplit
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from .app.config import settings, ROOT
from .app.database import (
    transaction,
    Clinic,
    User,
    LoginSession,
    Message,
    Thread,
    Job,
    Appointment,
    Delivery,
    Audit,
    Heartbeat,
    SalesLead,
    engine,
    init_db,
    audit,
)
from .app.auth import (
    principal,
    check_scope,
    admin_only,
    bootstrap,
    hash_password,
    verify_password,
    digest_token,
    limit,
)
from .app.schemas import (
    Schedule,
    ClinicInput,
    Login,
    UserInput,
    AppointmentInput,
    AppointmentUpdate,
    ReplyInput,
    DemoInput,
    PasswordInput,
    RetryInput,
    VoiceRequest,
    LeadInput,
    LeadStatusInput,
)
from .app.booking import (
    available,
    as_stamp,
    book,
    reschedule,
    cancel_reminders,
    readable,
)
from .app.receptionist import inbound, takeover, queue_reply, get_thread
from .app.kapso import verify, normalize_meta

log = logging.getLogger("jyutrix")


@asynccontextmanager
async def lifespan(app):
    settings.validate()
    init_db()
    bootstrap()
    if settings.mode == "demo":
        with transaction() as db:
            if not db.scalar(select(Clinic.id)):
                db.add(
                    Clinic(
                        name="Demo Dental Clinic",
                        doctor="Dr. Demo",
                        phone="+919999000000",
                        address="Demo address, Hyderabad",
                        timezone="Asia/Kolkata",
                        fee=500,
                        schedule=Schedule().model_dump(),
                        services=[
                            "Dental consultation",
                            "Cleaning",
                            "Root canal consultation",
                        ],
                        faqs=[
                            {
                                "question": "Do you accept card payments?",
                                "answer": "Yes, card and UPI payments are accepted at reception.",
                            }
                        ],
                        enabled=True,
                    )
                )
    yield


app = FastAPI(
    title="Jyutrix Clinic Reception",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.mode != "production" else None,
    redoc_url=None,
)


@app.middleware("http")
async def security(request, call_next):
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        allowed_origin = origin == settings.public_url
        if settings.mode != "production" and not allowed_origin:
            host = request.headers.get("host", "")
            scheme = request.headers.get(
                "x-forwarded-proto", request.url.scheme
            ).split(",", 1)[0].strip()
            parsed_origin = urlsplit(origin)
            allowed_origin = parsed_origin.netloc == host and parsed_origin.scheme == scheme
        if not allowed_origin:
            return JSONResponse({"detail": "Origin not allowed"}, 403)
    # Streamed body cap protects against requests without Content-Length too.
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, 400)
        if length > 262144:
            return JSONResponse({"detail": "Request too large"}, 413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                return JSONResponse({"detail": "Request too large"}, 413)
        request._body = bytes(body)
    result = await call_next(request)
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["X-Frame-Options"] = "DENY"
    result.headers["Referrer-Policy"] = "same-origin"
    result.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    result.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api"):
        result.headers["Cache-Control"] = "no-store"
    if settings.mode == "production":
        result.headers["Strict-Transport-Security"] = "max-age=31536000"
    return result


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(
        {"detail": "This record conflicts with an existing record. Refresh and retry."},
        409,
    )


@app.get("/")
def marketing_site():
    return FileResponse(ROOT / "frontend/marketing.html")


@app.get("/app")
@app.get("/app/")
def clinic_workspace():
    return FileResponse(ROOT / "frontend/index.html")


@app.post("/api/leads", status_code=201)
def create_sales_lead(payload: LeadInput, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    limit("sales-lead:" + client_ip, maximum=5, window=3600)
    # Honeypot submissions receive the normal success response but are not stored.
    if payload.website.strip():
        return {"ok": True}
    with transaction() as db:
        lead = SalesLead(
            name=payload.name.strip(),
            clinic_name=payload.clinic_name.strip(),
            city=payload.city.strip(),
            phone=payload.phone,
            email=payload.email.strip() if payload.email else None,
            note=payload.note.strip(),
            consent=True,
        )
        db.add(lead)
        db.flush()
        audit(db, "public_website", "sales_lead.created", lead.id)
    return {"ok": True, "message": "Thanks — our team will contact you about the demo."}


@app.get("/api/leads")
def sales_leads(user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        return [
            {
                "id": lead.id,
                "name": lead.name,
                "clinic_name": lead.clinic_name,
                "city": lead.city,
                "phone": lead.phone,
                "email": lead.email,
                "note": lead.note,
                "status": lead.status,
                "created": lead.created,
            }
            for lead in db.scalars(select(SalesLead).order_by(SalesLead.id.desc()).limit(500))
        ]


@app.patch("/api/leads/{lead_id}")
def update_sales_lead(lead_id: int, payload: LeadStatusInput, user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        lead = db.get(SalesLead, lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        lead.status = payload.status
        audit(db, user["username"], "sales_lead.status_changed", lead.id)
    return {"ok": True}


@app.delete("/api/leads/{lead_id}")
def delete_sales_lead(lead_id: int, user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        lead = db.get(SalesLead, lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        audit(db, user["username"], "sales_lead.deleted", lead.id)
        db.delete(lead)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.get("/health")
def health():
    with transaction() as db:
        db.execute(select(1))
    return {"status": "ok", "database": engine.dialect.name, "mode": settings.mode}


@app.post("/api/auth/login")
def login(payload: Login, request: Request, response: Response):
    limit("login:" + request.client.host)
    with transaction() as db:
        user = db.scalar(select(User).where(User.username == payload.username))
        # Constant work for unknown usernames; credential errors are indistinguishable.
        fallback = "0" * 32 + "$" + "0" * 128
        valid = verify_password(
            payload.password, user.password_hash if user else fallback
        )
        if not user or not valid or not user.active:
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(40)
        csrf = secrets.token_urlsafe(32)
        db.add(
            LoginSession(
                token_hash=digest_token(token),
                csrf=csrf,
                user_id=user.id,
                expires=int(time.time()) + 43200,
            )
        )
        audit(db, user.username, "auth.login", user.id, user.clinic_id)
    response.set_cookie(
        "jyutrix_session",
        token,
        httponly=True,
        secure=settings.mode == "production",
        samesite="strict",
        max_age=43200,
        path="/",
    )
    return {
        "csrf": csrf,
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "mode": settings.mode,
    }


@app.get("/api/auth/me")
def me(user=Depends(principal)):
    return {**user, "mode": settings.mode}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(principal)):
    with transaction() as db:
        db.execute(
            delete(LoginSession).where(
                LoginSession.token_hash
                == digest_token(request.cookies.get("jyutrix_session", ""))
            )
        )
    response.delete_cookie("jyutrix_session", path="/")
    return {"ok": True}


@app.post("/api/auth/password")
def password(payload: PasswordInput, response: Response, user=Depends(principal)):
    with transaction() as db:
        account = db.get(User, user["id"])
        if not verify_password(payload.current, account.password_hash):
            raise HTTPException(401, "Current password incorrect")
        account.password_hash = hash_password(payload.new)
        db.execute(delete(LoginSession).where(LoginSession.user_id == account.id))
        audit(
            db, user["username"], "auth.password_changed", account.id, account.clinic_id
        )
    response.delete_cookie("jyutrix_session", path="/")
    return {"ok": True, "sign_in_again": True}


def clinic_dict(c):
    return {
        k: getattr(c, k)
        for k in [
            "id",
            "name",
            "doctor",
            "phone",
            "address",
            "timezone",
            "fee",
            "schedule",
            "services",
            "faqs",
            "number_id",
            "enabled",
            "template_approved",
            "plan",
            "monthly_fee",
            "monthly_limit",
            "pilot_end",
        ]
    }


@app.get("/api/clinics")
def clinics(user=Depends(principal)):
    with transaction() as db:
        q = select(Clinic).order_by(Clinic.id)
        if user["role"] != "admin":
            q = q.where(Clinic.id == user["clinic_id"])
        return [clinic_dict(c) for c in db.scalars(q)]


@app.post("/api/clinics")
def create_clinic(payload: ClinicInput, user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        c = Clinic(**payload.model_dump())
        db.add(c)
        db.flush()
        audit(db, user["username"], "clinic.created", c.id, c.id)
        return clinic_dict(c)


@app.put("/api/clinics/{clinic_id}")
def update_clinic(clinic_id: int, payload: ClinicInput, user=Depends(principal)):
    check_scope(user, clinic_id)
    if user["role"] == "receptionist":
        raise HTTPException(403, "Owner access required")
    with transaction(clinic_id) as db:
        c = db.get(Clinic, clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        values = payload.model_dump()
        if user["role"] != "admin":
            for field in [
                "number_id",
                "plan",
                "monthly_fee",
                "monthly_limit",
                "pilot_end",
            ]:
                values.pop(field)
        for key, value in values.items():
            setattr(c, key, value)
        if not c.enabled:
            for thread in db.scalars(
                select(Thread).where(
                    Thread.clinic_id == clinic_id, Thread.mode == "bot"
                )
            ):
                takeover(db, thread, "Clinic automation disabled", user["username"])
        audit(db, user["username"], "clinic.updated", c.id, c.id)
        return clinic_dict(c)


@app.post("/api/users")
def create_user(payload: UserInput, user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        if not db.get(Clinic, payload.clinic_id):
            raise HTTPException(404, "Clinic not found")
        account = User(
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
            clinic_id=payload.clinic_id,
        )
        db.add(account)
        db.flush()
        audit(db, user["username"], "user.created", account.id, account.clinic_id)
        return {"id": account.id, "username": account.username, "role": account.role}


@app.get("/api/users")
def users(user=Depends(principal)):
    admin_only(user)
    with transaction() as db:
        return [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "clinic_id": u.clinic_id,
                "active": u.active,
            }
            for u in db.scalars(select(User).order_by(User.id))
        ]


@app.post("/api/users/{user_id}/disable")
def disable_user(user_id: int, user=Depends(principal)):
    admin_only(user)
    if user_id == user["id"]:
        raise HTTPException(409, "Cannot disable your own account")
    with transaction() as db:
        u = db.get(User, user_id)
        if not u:
            raise HTTPException(404, "User not found")
        u.active = False
        db.execute(delete(LoginSession).where(LoginSession.user_id == u.id))
        audit(db, user["username"], "user.disabled", u.id, u.clinic_id)
    return {"ok": True}


@app.get("/api/availability")
def availability(clinic_id: int, day: str | None = None, user=Depends(principal)):
    check_scope(user, clinic_id)
    with transaction(clinic_id) as db:
        c = db.get(Clinic, clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        try:
            target = date.fromisoformat(day) if day else None
        except ValueError:
            raise HTTPException(422, "Use YYYY-MM-DD")
        return [
            {
                "starts": s,
                "label": readable(c, s),
                "iso": datetime.fromtimestamp(s, ZoneInfo(c.timezone)).isoformat(),
            }
            for s in available(db, c, target)
        ]


def appt_dict(a, c):
    return {
        "id": a.id,
        "clinic_id": a.clinic_id,
        "name": a.name,
        "contact": a.contact,
        "starts": a.starts,
        "ends": a.ends,
        "status": a.status,
        "source": a.source,
        "paid_amount": a.paid_amount,
        "label": readable(c, a.starts),
        "consent": a.consent,
    }


@app.get("/api/appointments")
def appointments(clinic_id: int, user=Depends(principal)):
    check_scope(user, clinic_id)
    with transaction() as db:
        c = db.get(Clinic, clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        return [
            appt_dict(a, c)
            for a in db.scalars(
                select(Appointment)
                .where(Appointment.clinic_id == clinic_id)
                .order_by(Appointment.starts.desc())
                .limit(500)
            )
        ]


@app.post("/api/appointments")
def appointment(payload: AppointmentInput, user=Depends(principal)):
    check_scope(user, payload.clinic_id)
    with transaction(payload.clinic_id) as db:
        c = db.get(Clinic, payload.clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        contact = payload.contact.lstrip("+")
        thread = db.scalar(
            select(Thread).where(
                Thread.clinic_id == c.id,
                Thread.contact == contact,
                Thread.channel == "whatsapp",
            )
        )
        if payload.consent and thread and not thread.opted_out:
            thread.consent = True
        a = book(
            db,
            c,
            thread,
            as_stamp(payload.scheduled_at, c),
            f"manual:{c.id}:{payload.request_key}",
            source="staff",
            name=payload.name,
            contact=contact,
            consent=payload.consent and not bool(thread and thread.opted_out),
        )
        audit(db, user["username"], "appointment.staff_created", a.id, c.id)
        if thread and not thread.opted_out:
            queue_reply(
                db,
                c,
                thread,
                f"Appointment #{a.id} confirmed for {readable(c,a.starts)} with {c.doctor}. Reply CANCEL or STAFF for changes.",
                f"manual-confirm:{a.id}",
                actor="staff",
            )
        return appt_dict(a, c)


@app.patch("/api/appointments/{appointment_id}")
def change_appointment(
    appointment_id: int, payload: AppointmentUpdate, user=Depends(principal)
):
    with transaction() as db:
        existing = db.get(Appointment, appointment_id)
        if not existing:
            raise HTTPException(404, "Appointment not found")
        cid = existing.clinic_id
    check_scope(user, cid)
    with transaction(cid) as db:
        a = db.get(Appointment, appointment_id)
        c = db.get(Clinic, cid)
        if payload.status == "confirmed" and a.status != "confirmed":
            raise HTTPException(
                409, "Create a new booking to reactivate a closed appointment"
            )
        if payload.scheduled_at:
            if a.status != "confirmed":
                raise HTTPException(
                    409, "Only confirmed appointments can be rescheduled"
                )
            reschedule(db, c, a, as_stamp(payload.scheduled_at, c))
        if payload.status:
            a.status = payload.status
            if a.status != "confirmed":
                cancel_reminders(db, a)
        if payload.paid_amount is not None:
            a.paid_amount = payload.paid_amount
        audit(db, user["username"], "appointment.updated", a.id, cid)
        thread = db.get(Thread, a.thread_id) if a.thread_id else None
        if (
            thread
            and thread.channel == "whatsapp"
            and not thread.opted_out
            and (payload.scheduled_at or payload.status == "cancelled")
        ):
            queue_reply(
                db,
                c,
                thread,
                f"Appointment #{a.id}: {a.status}, {readable(c,a.starts)}. Reply STAFF for help.",
                f"update:{a.id}:{uuid4()}",
                actor="staff",
            )
        return appt_dict(a, c)


@app.get("/api/threads")
def threads(clinic_id: int, user=Depends(principal)):
    check_scope(user, clinic_id)
    with transaction() as db:
        result = []
        for t in db.scalars(
            select(Thread)
            .where(Thread.clinic_id == clinic_id)
            .order_by(Thread.updated.desc())
            .limit(200)
        ):
            last = db.scalar(
                select(Message)
                .where(Message.thread_id == t.id)
                .order_by(Message.id.desc())
                .limit(1)
            )
            result.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "contact": t.contact,
                    "mode": t.mode,
                    "channel": t.channel,
                    "attention": t.attention,
                    "updated": t.updated,
                    "consent": t.consent,
                    "opted_out": t.opted_out,
                    "last": last.text if last else "",
                }
            )
        return result


def thread_scope(thread_id, user):
    with transaction() as db:
        t = db.get(Thread, thread_id)
        if not t:
            raise HTTPException(404, "Conversation not found")
        check_scope(user, t.clinic_id)
        return t.clinic_id


@app.get("/api/threads/{thread_id}/messages")
def messages(thread_id: int, user=Depends(principal)):
    thread_scope(thread_id, user)
    with transaction() as db:
        return [
            {
                "id": m.id,
                "direction": m.direction,
                "text": m.text,
                "status": m.status,
                "created": m.created,
            }
            for m in db.scalars(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.id)
                .limit(500)
            )
        ]


@app.post("/api/threads/{thread_id}/takeover")
def take(thread_id: int, user=Depends(principal)):
    cid = thread_scope(thread_id, user)
    with transaction(cid) as db:
        takeover(
            db,
            db.get(Thread, thread_id),
            "Staff handling conversation",
            user["username"],
        )
    return {"ok": True}


@app.post("/api/threads/{thread_id}/resume")
def resume(thread_id: int, user=Depends(principal)):
    cid = thread_scope(thread_id, user)
    with transaction(cid) as db:
        t = db.get(Thread, thread_id)
        t.mode = "bot"
        t.state = {}
        t.attention = ""
        t.generation += 1
        audit(db, user["username"], "conversation.resumed", thread_id, cid)
    return {"ok": True}


@app.post("/api/threads/{thread_id}/reply")
def reply(thread_id: int, payload: ReplyInput, user=Depends(principal)):
    cid = thread_scope(thread_id, user)
    with transaction(cid) as db:
        t = db.get(Thread, thread_id)
        if t.opted_out:
            raise HTTPException(409, "Patient opted out")
        if t.channel == "whatsapp" and t.last_inbound < time.time() - 86400:
            raise HTTPException(409, "24-hour reply window closed")
        takeover(db, t, "Staff handling conversation", user["username"])
        queue_reply(
            db, db.get(Clinic, cid), t, payload.text, f"staff:{uuid4()}", actor="staff"
        )
    return {"ok": True}


@app.delete("/api/threads/{thread_id}/patient")
def erase_patient(thread_id: int, user=Depends(principal)):
    cid = thread_scope(thread_id, user)
    if user["role"] == "receptionist":
        raise HTTPException(403, "Owner access required")
    with transaction(cid) as db:
        t = db.get(Thread, thread_id)
        contact = t.contact
        tids = list(
            db.scalars(
                select(Thread.id).where(
                    Thread.clinic_id == cid, Thread.contact == contact
                )
            )
        )
        appts = list(
            db.scalars(
                select(Appointment).where(
                    Appointment.clinic_id == cid, Appointment.contact == contact
                )
            )
        )
        for a in appts:
            cancel_reminders(db, a)
            a.name = "Deleted patient"
            a.contact = "deleted"
            a.thread_id = None
            a.consent = False
        # Pending inbound jobs carry contact data before a thread exists.
        for j in db.scalars(select(Job).where(Job.clinic_id == cid)):
            if j.thread_id in tids or j.payload.get("contact") == contact:
                db.delete(j)
        db.flush()
        db.execute(delete(Message).where(Message.thread_id.in_(tids)))
        db.execute(delete(Thread).where(Thread.id.in_(tids)))
        audit(db, user["username"], "patient.erased", thread_id, cid)
    return {"ok": True}


@app.post("/api/demo/message")
def demo(payload: DemoInput, user=Depends(principal)):
    if settings.mode == "production":
        raise HTTPException(404, "Demo disabled in production")
    check_scope(user, payload.clinic_id)
    with transaction(payload.clinic_id) as db:
        c = db.get(Clinic, payload.clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        t, result = inbound(
            db,
            c,
            payload.contact,
            payload.name,
            payload.text,
            str(uuid4()),
            channel="demo",
        )
        return {"thread_id": t.id, "reply": result, "mode": t.mode, "state": t.state}


@app.post("/webhooks/kapso/{clinic_id}")
async def webhook(clinic_id: int, request: Request):
    raw = await request.body()
    if not verify(raw, request.headers.get("x-webhook-signature", "")):
        raise HTTPException(401, "Invalid signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("entry"), list):
        raise HTTPException(
            422, "Configure a Kapso kind=meta webhook; expected Meta entry payload"
        )
    try:
        records = normalize_meta(body)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Malformed Meta payload")
    key = request.headers.get("x-idempotency-key") or hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(f"{clinic_id}:{key}".encode()).hexdigest()
    with transaction(clinic_id) as db:
        c = db.get(Clinic, clinic_id)
        if not c or not c.number_id:
            raise HTTPException(404, "Clinic number not configured")
        if len(records) > 100:
            raise HTTPException(422, "Too many events in one delivery")
        if db.get(Delivery, key):
            return {"ok": True, "duplicate": True}
        for r in records:
            if r["number_id"] != c.number_id:
                raise HTTPException(403, "Webhook number does not match clinic")
            if not r["provider_id"]:
                raise HTTPException(422, "Message id required")
            if r["type"] == "inbound":
                if not r["contact"]:
                    raise HTTPException(422, "Sender identity required")
                jobkey = f'inbound:{c.id}:{r["provider_id"]}'
                if not db.scalar(
                    select(Job.id).where(Job.key == jobkey)
                ) and not db.scalar(
                    select(Message.id).where(Message.provider_id == r["provider_id"])
                ):
                    db.add(
                        Job(
                            clinic_id=c.id,
                            key=jobkey,
                            kind="inbound",
                            payload=r,
                            due=int(time.time()),
                        )
                    )
            elif r["status"] in {"sent", "delivered", "read", "failed"}:
                msg = db.scalar(
                    select(Message)
                    .join(Thread, Message.thread_id == Thread.id)
                    .where(
                        Message.provider_id == r["provider_id"],
                        Thread.clinic_id == clinic_id,
                    )
                )
                if msg:
                    rank = {
                        "queued": 0,
                        "accepted": 1,
                        "sent": 2,
                        "delivered": 3,
                        "read": 4,
                        "failed": -1,
                    }
                    if (
                        r["status"] == "failed"
                        and msg.status not in {"delivered", "read"}
                        or rank.get(r["status"], 0) > rank.get(msg.status, 0)
                    ):
                        msg.status = r["status"]
                    for job in db.scalars(select(Job).where(Job.message_id == msg.id)):
                        job.status = msg.status
                        if msg.status == "failed":
                            job.error = "Provider reported delivery failure"
                            db.get(Thread, msg.thread_id).attention = job.error
        db.add(Delivery(key=key, clinic_id=clinic_id))
    return {"ok": True, "accepted": len(records)}


@app.get("/api/jobs")
def jobs(clinic_id: int, user=Depends(principal)):
    check_scope(user, clinic_id)
    with transaction() as db:
        return [
            {
                "id": j.id,
                "kind": j.kind,
                "status": j.status,
                "attempts": j.attempts,
                "due": j.due,
                "error": j.error,
                "thread_id": j.thread_id,
            }
            for j in db.scalars(
                select(Job)
                .where(Job.clinic_id == clinic_id)
                .order_by(Job.id.desc())
                .limit(200)
            )
        ]


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int, payload: RetryInput, user=Depends(principal)):
    with transaction() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        cid = job.clinic_id
    check_scope(user, cid)
    with transaction(cid) as db:
        job = db.get(Job, job_id)
        if job.status not in {"failed", "uncertain"}:
            raise HTTPException(409, "Job is not failed or uncertain")
        if job.status == "uncertain" and not payload.reviewed_provider_logs:
            raise HTTPException(
                409,
                "Review provider logs and acknowledge the send did not succeed before retrying",
            )
        job.status = "pending"
        job.due = int(time.time())
        job.attempts = 0
        job.error = ""
        audit(db, user["username"], "job.retry", job.id, cid)
    return {"ok": True}


@app.get("/api/reports")
def report(clinic_id: int, user=Depends(principal)):
    check_scope(user, clinic_id)
    with transaction() as db:
        c = db.get(Clinic, clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        q = select(Appointment).where(
            Appointment.clinic_id == clinic_id, Appointment.source != "demo"
        )
        appts = list(db.scalars(q))
        today = datetime.now(ZoneInfo(c.timezone)).date()
        threads = list(
            db.scalars(
                select(Thread).where(
                    Thread.clinic_id == clinic_id, Thread.channel != "demo"
                )
            )
        )
        now = datetime.now(timezone.utc)
        month_start = int(
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        usage = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.clinic_id == clinic_id,
                Job.kind.in_(["outbound", "reminder"]),
                Job.status.in_(["accepted", "sent", "delivered", "read", "uncertain"]),
                Job.created >= month_start,
            )
        )
        return {
            "enquiries": len(threads),
            "booked": len(appts),
            "attended": sum(a.status == "completed" for a in appts),
            "no_shows": sum(a.status == "no_show" for a in appts),
            "today": sum(
                datetime.fromtimestamp(a.starts, ZoneInfo(c.timezone)).date() == today
                and a.status == "confirmed"
                for a in appts
            ),
            "needs_attention": sum(bool(t.attention) for t in threads),
            "recorded_payments": sum(a.paid_amount for a in appts),
            "estimated_booking_value": sum(
                a.status in {"confirmed", "completed"} for a in appts
            )
            * c.fee,
            "monthly_messages": usage,
            "monthly_limit": c.monthly_limit,
            "definition": "Operational totals across all retained live data. Bookings include staff bookings; payments are staff recorded, not payment-gateway verified. No incremental revenue claim.",
        }


@app.get("/api/status")
def status(user=Depends(principal)):
    with transaction() as db:
        hb = db.get(Heartbeat, "worker")
        return {
            "mode": settings.mode,
            "database": engine.dialect.name,
            "worker_healthy": bool(hb and hb.seen > time.time() - 60),
            "kapso_configured": bool(settings.kapso_key and settings.webhook_secret),
            "kapso_live_verified": False,
            "ai_mode": (
                "approved FAQ semantic matching"
                if settings.openai_key
                else "approved rules and exact FAQs"
            ),
            "voice_tools_configured": bool(settings.voice_key),
            "voice_telephony_connected": False,
        }


@app.get("/api/audit")
def audit_logs(clinic_id: int, user=Depends(principal)):
    check_scope(user, clinic_id)
    if user["role"] == "receptionist":
        raise HTTPException(403, "Owner access required")
    with transaction() as db:
        return [
            {
                "actor": a.actor,
                "action": a.action,
                "target": a.target,
                "created": a.created,
            }
            for a in db.scalars(
                select(Audit)
                .where(Audit.clinic_id == clinic_id)
                .order_by(Audit.id.desc())
                .limit(100)
            )
        ]


@app.post("/api/voice/tools")
def voice_tools(payload: VoiceRequest, request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not settings.voice_key or not hmac.compare_digest(token, settings.voice_key):
        raise HTTPException(401, "Invalid voice tool key")
    limit("voice:" + str(payload.clinic_id), 60, 60)
    with transaction(payload.clinic_id) as db:
        c = db.get(Clinic, payload.clinic_id)
        if not c:
            raise HTTPException(404, "Clinic not found")
        if payload.action == "info":
            return {
                "name": c.name,
                "doctor": c.doctor,
                "fee": c.fee,
                "address": c.address,
                "phone": c.phone,
                "services": c.services,
                "schedule": c.schedule,
                "timezone": c.timezone,
            }
        if payload.action == "availability":
            try:
                target = date.fromisoformat(payload.date) if payload.date else None
            except ValueError:
                raise HTTPException(422, "Use YYYY-MM-DD")
            return {
                "slots": [
                    {"starts": s, "label": readable(c, s)}
                    for s in available(db, c, target, 3)
                ]
            }
        if not payload.contact:
            raise HTTPException(
                422, "Patient phone required for a staff-reviewed request"
            )
        # Voice caller ID is not authentication. No cancellation or automatic confirmation through this endpoint.
        t = get_thread(
            db, c, payload.contact.lstrip("+"), payload.name, channel="voice"
        )
        key = f"voice:{c.id}:{payload.call_id}"
        if not db.scalar(select(Job.id).where(Job.key == key)):
            takeover(
                db, t, "Voice appointment request awaiting staff confirmation", "voice"
            )
            db.add(
                Message(
                    thread_id=t.id,
                    direction="in",
                    text=f'Voice request: preferred time {payload.scheduled_at or "not supplied"}',
                    status="received",
                )
            )
            db.add(
                Job(
                    clinic_id=c.id,
                    thread_id=t.id,
                    key=key,
                    kind="voice_request",
                    status="done",
                    due=int(time.time()),
                    payload={},
                )
            )
        return {
            "status": "pending_staff_confirmation",
            "thread_id": t.id,
            "instruction": "Tell caller this is a request, not a confirmed appointment.",
        }

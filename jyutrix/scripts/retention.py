"""Scheduled retention task. Run daily from a trusted operator scheduler."""

import time
from sqlalchemy import select, delete
from backend.app.config import settings
from backend.app.database import (
    transaction,
    Message,
    Job,
    Delivery,
    LoginSession,
    RateBucket,
    Thread,
    Appointment,
    Audit,
    Clinic,
    SalesLead,
    audit,
)

cutoff = int(time.time()) - settings.retention_days * 86400
with transaction() as db:
    clinic_ids = list(db.scalars(select(Clinic.id)))
for cid in clinic_ids:
    with transaction(cid) as db:
        db.execute(
            delete(Job).where(
                Job.clinic_id == cid,
                Job.created < cutoff,
                Job.status.not_in(["pending", "retry", "dispatching"]),
            )
        )
        tids = list(db.scalars(select(Thread.id).where(Thread.clinic_id == cid)))
        if tids:
            db.execute(
                delete(Message).where(
                    Message.thread_id.in_(tids), Message.created < cutoff
                )
            )
        for t in db.scalars(
            select(Thread).where(Thread.clinic_id == cid, Thread.updated < cutoff)
        ):
            live = db.scalar(
                select(Appointment.id).where(
                    Appointment.thread_id == t.id,
                    Appointment.status == "confirmed",
                    Appointment.starts > time.time(),
                )
            )
            pending = db.scalar(
                select(Job.id).where(
                    Job.thread_id == t.id,
                    Job.status.in_(["pending", "retry", "dispatching"]),
                )
            )
            if not live and not pending:
                db.execute(delete(Job).where(Job.thread_id == t.id))
                db.execute(delete(Message).where(Message.thread_id == t.id))
                for a in db.scalars(
                    select(Appointment).where(Appointment.thread_id == t.id)
                ):
                    a.thread_id = None
                    a.name = "Retention anonymized"
                    a.contact = "deleted"
                    a.consent = False
                db.flush()
                db.delete(t)
        db.execute(
            delete(Delivery).where(Delivery.clinic_id == cid, Delivery.created < cutoff)
        )
        audit(db, "retention", "retention.completed", settings.retention_days, cid)
with transaction() as db:
    db.execute(delete(LoginSession).where(LoginSession.expires < time.time()))
    db.execute(delete(RateBucket).where(RateBucket.reset_at < time.time() - 86400))
    db.execute(delete(SalesLead).where(SalesLead.created < cutoff))
print(
    "Retention task complete. Provider-held data must be deleted separately under your provider policy."
)

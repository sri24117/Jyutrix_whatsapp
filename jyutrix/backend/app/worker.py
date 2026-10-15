import logging
import time
from sqlalchemy import select, func
from .database import (
    transaction,
    Job,
    Clinic,
    Thread,
    Message,
    Appointment,
    Heartbeat,
    audit,
)
from .config import settings
from .kapso import send, ProviderError
from .receptionist import inbound, takeover
from .booking import readable

log = logging.getLogger("jyutrix.worker")


def fail(db, job, error, status="failed"):
    job.status = status
    job.error = error
    if job.message_id:
        msg = db.get(Message, job.message_id)
        if msg:
            msg.status = status
    if job.thread_id:
        thread = db.get(Thread, job.thread_id)
        if thread:
            thread.attention = error
            thread.updated = int(time.time())


def _run_job(job_id, clinic_id):
    # Same clinic lock as takeover and booking. Provider call timeout is bounded.
    with transaction(clinic_id) as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if (
            not job
            or job.status not in {"pending", "retry", "dispatching"}
            or job.due > time.time()
        ):
            return
        clinic = db.get(Clinic, clinic_id)
        job.attempts += 1
        if job.kind == "inbound":
            p = job.payload
            if not db.scalar(
                select(Message.id).where(Message.provider_id == p["provider_id"])
            ):
                try:
                    inbound(
                        db,
                        clinic,
                        p["contact"],
                        p["name"],
                        p["text"],
                        p["provider_id"],
                        provider_id=p["provider_id"],
                        received_at=p.get("timestamp"),
                    )
                except Exception:
                    # Roll back the whole event, including partial booking/reply, before recording failure.
                    raise
            job.status = "done"
            job.payload = {}
            return
        thread = db.get(Thread, job.thread_id) if job.thread_id else None
        if not thread:
            fail(db, job, "Conversation removed", "cancelled")
            return
        if thread.opted_out:
            fail(db, job, "Patient opted out", "cancelled")
            return
        actor = job.payload.get("actor")
        if (
            job.kind == "outbound"
            and actor == "bot"
            and (
                thread.mode != "bot"
                or thread.generation != job.payload.get("generation")
            )
        ):
            fail(db, job, "Suppressed by staff takeover", "cancelled")
            return
        if not clinic.enabled and (actor == "bot" or job.kind == "reminder"):
            fail(db, job, "Clinic automation disabled")
            return
        if clinic.pilot_end and clinic.pilot_end < time.time():
            fail(db, job, "Plan expired; renew to continue")
            return
        if job.kind == "reminder":
            appt = db.get(Appointment, job.appointment_id)
            if (
                not appt
                or appt.status != "confirmed"
                or appt.starts != job.payload["starts"]
                or not appt.consent
                or not thread.consent
            ):
                job.status = "cancelled"
                return
            if appt.starts <= time.time():
                job.status = "cancelled"
                return
            if not clinic.template_approved:
                fail(db, job, "Reminder template not marked approved")
                return
            payload = {
                "type": "template",
                "template": {
                    "name": settings.reminder_template,
                    "language": {"code": settings.template_language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": thread.name},
                                {"type": "text", "text": clinic.name},
                                {"type": "text", "text": readable(clinic, appt.starts)},
                                {"type": "text", "text": str(appt.id)},
                            ],
                        }
                    ],
                },
            }
            if not job.message_id:
                msg = Message(
                    thread_id=thread.id,
                    direction="out",
                    text=f"Reminder: appointment #{appt.id}, {readable(clinic,appt.starts)}",
                    status="queued",
                )
                db.add(msg)
                db.flush()
                job.message_id = msg.id
        else:
            if thread.last_inbound < int(time.time()) - 24 * 3600:
                fail(
                    db,
                    job,
                    "24-hour reply window closed; ask patient to message or use approved template",
                )
                return
            payload = {"type": "text", "text": {"body": job.payload["text"]}}
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        month_start = int(
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        usage = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.clinic_id == clinic.id,
                Job.kind.in_(["outbound", "reminder"]),
                Job.status.in_(["accepted", "sent", "delivered", "read", "uncertain"]),
                Job.created >= month_start,
            )
        )
        if usage >= clinic.monthly_limit:
            fail(db, job, "Monthly message allowance reached")
            return
        try:
            provider_id = send(clinic.number_id, thread.contact, payload)
        except ProviderError as e:
            if e.retry and job.attempts < 5:
                job.status = "retry"
                job.due = int(time.time()) + min(600, 15 * 2**job.attempts)
                job.error = str(e)
            else:
                fail(db, job, str(e), "uncertain" if e.uncertain else "failed")
            return
        msg = db.get(Message, job.message_id)
        msg.provider_id = provider_id
        msg.status = "accepted"
        job.status = "accepted"
        job.error = ""
        audit(db, "worker", "message.accepted", job.id, clinic.id)


def process_job(job_id, clinic_id):
    # Commit an outbound send claim BEFORE the HTTP call. A killed process leaves
    # dispatching, which is quarantined rather than blindly resent.
    with transaction(clinic_id) as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status not in {"pending", "retry"} or job.due > time.time():
            return
        if job.kind == "inbound":
            earlier = db.scalar(
                select(Job.id)
                .where(
                    Job.clinic_id == clinic_id,
                    Job.kind == "inbound",
                    Job.id < job.id,
                    Job.status.in_(["pending", "retry"]),
                )
                .limit(1)
            )
            if earlier:
                return
        if job.kind != "inbound":
            job.status = "dispatching"
            job.due = int(time.time())
    _run_job(job_id, clinic_id)


def tick(max_jobs=20):
    with transaction() as db:
        beat = db.get(Heartbeat, "worker")
        if beat:
            beat.seen = int(time.time())
        else:
            db.add(Heartbeat(name="worker", seen=int(time.time())))
        for stale in db.scalars(
            select(Job)
            .where(Job.status == "dispatching", Job.due < int(time.time()) - 120)
            .with_for_update(skip_locked=True)
        ):
            fail(
                db,
                stale,
                "Worker stopped during send; review Kapso logs before retry",
                "uncertain",
            )
        jobs = list(
            db.execute(
                select(Job.id, Job.clinic_id)
                .where(Job.status.in_(["pending", "retry"]), Job.due <= time.time())
                .order_by(Job.id)
                .limit(max_jobs)
            )
        )
    for job_id, clinic_id in jobs:
        try:
            process_job(job_id, clinic_id)
        except Exception:
            log.exception("Job %s failed", job_id)
            with transaction(clinic_id) as db:
                job = db.get(Job, job_id)
                if job and job.status in {"pending", "retry", "dispatching"}:
                    job.attempts += 1
                    if job.kind == "inbound":
                        job.status = "retry" if job.attempts < 5 else "failed"
                        job.due = int(time.time()) + 30 * job.attempts
                        job.error = "Processing failed; inspect server logs"
                    else:
                        # A crash after send may mean delivery happened. Never automatically resend.
                        fail(
                            db,
                            job,
                            "Processing outcome uncertain; review provider logs",
                            "uncertain",
                        )
    return len(jobs)


def run():
    logging.basicConfig(level=logging.INFO)
    settings.validate()
    while True:
        tick()
        time.sleep(2)


if __name__ == "__main__":
    run()

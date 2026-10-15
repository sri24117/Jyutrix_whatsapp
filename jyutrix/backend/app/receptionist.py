from datetime import date, timedelta
import re
import time
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from .database import Thread, Message, Job, Appointment, audit
from .booking import available, readable, book, reschedule, cancel_reminders, local_time

MEDICAL = re.compile(
    r"\b(medicine|medication|dosage|diagnos|prescrib|antibiotic|chest pain|bleeding|breath|emergency|suicide|overdose|severe pain|swelling|guarantee|treatment success|pain|symptom|cure|fever)\w*|రక్త|శ్వాస|अत्यधिक|सांस",
    re.I,
)
BOOKING = re.compile(
    r"\b(book|booking|appointment|slot|visit|appt|kavali)\b|అపాయింట్|బుక్|अपॉइंट|बुक",
    re.I,
)
HUMAN = re.compile(
    r"\b(human|staff|receptionist|complaint|agent|person|help me)\b|సిబ్బంది|मनुष्य|स्टाफ",
    re.I,
)


def get_thread(db, clinic, contact, name, channel="whatsapp"):
    row = db.scalar(
        select(Thread).where(
            Thread.clinic_id == clinic.id,
            Thread.contact == contact,
            Thread.channel == channel,
        )
    )
    if not row:
        row = Thread(
            clinic_id=clinic.id, contact=contact, name=name, channel=channel, state={}
        )
        db.add(row)
        db.flush()
    return row


def queue_reply(db, clinic, thread, text, key, actor="bot"):
    if db.scalar(select(Job.id).where(Job.key == key)):
        return text
    msg = Message(
        thread_id=thread.id,
        direction="out",
        text=text,
        status="demo" if thread.channel == "demo" else "queued",
    )
    db.add(msg)
    db.flush()
    if thread.channel == "whatsapp":
        db.add(
            Job(
                clinic_id=clinic.id,
                thread_id=thread.id,
                key=key,
                kind="outbound",
                due=int(time.time()),
                message_id=msg.id,
                payload={"text": text, "actor": actor, "generation": thread.generation},
            )
        )
    return text


def takeover(db, thread, reason, actor="system"):
    thread.mode = "human"
    thread.attention = reason
    thread.state = {}
    thread.generation += 1
    for job in db.scalars(
        select(Job).where(
            Job.thread_id == thread.id,
            Job.kind == "outbound",
            Job.status.in_(["pending", "retry", "dispatching"]),
        )
    ):
        if job.payload.get("actor") == "bot":
            job.status = "cancelled"
            if job.message_id:
                db.get(Message, job.message_id).status = "cancelled"
    audit(db, actor, "conversation.takeover", thread.id, thread.clinic_id)


def day_from_text(text, clinic):
    today = local_time(clinic, time.time()).date()
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            raise HTTPException(422, "Invalid date; use YYYY-MM-DD")
    if re.search(r"tomorrow|repu|రేపు|कल", text, re.I):
        return today + timedelta(days=1)
    return today


def offer(db, clinic, thread, text, appointment_id=None):
    try:
        target = day_from_text(text, clinic)
    except HTTPException as e:
        return str(e.detail)
    if re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week)\b",
        text,
        re.I,
    ) and not re.search(r"\d{4}-\d{2}-\d{2}", text):
        return (
            "Please send your preferred date as YYYY-MM-DD so I offer the correct day."
        )
    try:
        slots = available(
            db, clinic, target, 3, exclude=appointment_id, demo=thread.channel == "demo"
        )
    except HTTPException as e:
        return str(e.detail)
    if not slots:
        return "No slots are available for that date. Send another date as YYYY-MM-DD, or reply STAFF."
    thread.state = {
        "step": "choose",
        "slots": slots,
        "expires": int(time.time()) + 900,
        "appointment_id": appointment_id,
    }
    choices = "\n".join(f"{i+1}. {readable(clinic,s)}" for i, s in enumerate(slots))
    return f"Available with {clinic.doctor} ({clinic.timezone}):\n{choices}\nReply 1, 2 or 3 to choose. Nothing is booked yet. Or send another date as YYYY-MM-DD."


def reception(db, clinic, thread, text, event_key):
    t = text.strip()
    lower = t.casefold()
    yes = lower in {"yes", "confirm", "అవును", "అవునూ", "हाँ", "haan"}
    if lower in {"stop", "unsubscribe", "ఆపు", "बंद"}:
        thread.opted_out = True
        thread.consent = False
        thread.state = {}
        thread.generation += 1
        for job in db.scalars(
            select(Job).where(
                Job.thread_id == thread.id,
                Job.status.in_(["pending", "retry", "dispatching"]),
            )
        ):
            if job.kind != "inbound":
                job.status = "cancelled"
        audit(db, "patient", "consent.withdrawn", thread.id, clinic.id)
        return "Appointment messages are stopped. Reply START to resume, or call the clinic for help."
    if lower == "start":
        thread.opted_out = False
        return "Messages resumed. Reply BOOK to request an appointment. We will ask permission before sending reminders."
    if thread.opted_out:
        return None
    if thread.mode == "human":
        return None
    if clinic.pilot_end and clinic.pilot_end < time.time():
        takeover(db, thread, "Plan expired; staff review required")
        return None
    if not clinic.enabled:
        takeover(db, thread, "Automation disabled for this clinic")
        return "Your message is with reception. Please call the clinic if you need an immediate response."
    if MEDICAL.search(t):
        takeover(db, thread, "Medical or urgent question requires staff")
        return f"I am an automated receptionist and cannot give medical advice. For an emergency, seek immediate emergency care; do not wait for a WhatsApp reply. For clinic assistance call {clinic.phone}."
    if HUMAN.search(t):
        takeover(db, thread, "Patient requested staff")
        return f"I have placed your message in the reception queue. For immediate help, call {clinic.phone}."
    if lower.startswith("name "):
        thread.name = t[5:].strip()[:120] or thread.name
        return f"Your booking name is now {thread.name}. Reply BOOK or STAFF."
    state = thread.state or {}
    if state and state.get("expires", 0) < time.time():
        thread.state = {}
        state = {}
    upcoming = db.scalar(
        select(Appointment)
        .where(
            Appointment.clinic_id == clinic.id,
            Appointment.contact == thread.contact,
            (
                Appointment.source == "demo"
                if thread.channel == "demo"
                else Appointment.source != "demo"
            ),
            Appointment.status == "confirmed",
            Appointment.starts > time.time(),
        )
        .order_by(Appointment.starts)
    )
    if lower in {"cancel", "cancel appointment", "రద్దు", "रद्द"}:
        if not upcoming:
            return "No upcoming appointment was found for this contact. Reply STAFF for help."
        thread.state = {
            "step": "cancel_confirm",
            "appointment_id": upcoming.id,
            "expires": int(time.time()) + 900,
        }
        return f"Cancel appointment #{upcoming.id} on {readable(clinic,upcoming.starts)}? Reply YES to cancel or NO to keep it."
    if re.search(r"\b(reschedule|postpone|change appointment)\b", lower):
        if not upcoming:
            return "No upcoming appointment was found. Reply BOOK or STAFF."
        return offer(db, clinic, thread, t, upcoming.id)
    if lower == "no" and state:
        thread.state = {}
        return "No change made. Reply BOOK to see availability or STAFF for help."
    if state.get("step") == "cancel_confirm" and yes:
        appt = db.get(Appointment, state["appointment_id"])
        if not appt or appt.clinic_id != clinic.id or appt.contact != thread.contact:
            thread.state = {}
            return "Appointment could not be verified. Reply STAFF."
        appt.status = "cancelled"
        cancel_reminders(db, appt)
        thread.state = {}
        audit(db, "patient", "appointment.cancelled", appt.id, clinic.id)
        return f"Appointment #{appt.id} cancelled. Reply BOOK for a new appointment."
    if state.get("step") == "choose" and t.isdigit():
        idx = int(t) - 1
        if idx < 0 or idx >= len(state["slots"]):
            return "Please choose a displayed slot number."
        starts = state["slots"][idx]
        thread.state = {**state, "step": "confirm", "starts": starts}
        return f"Confirm for {thread.name}: {readable(clinic,starts)} with {clinic.doctor}? Consultation fee: Rs {clinic.fee}. Reply YES to confirm or NO. Your name/contact are used for booking coordination. Appointment reminders will be sent on WhatsApp; reply STOP to stop messages. No medical records are requested."
    if state.get("step") == "confirm" and yes:
        starts = state["starts"]
        old_id = state.get("appointment_id")
        try:
            thread.consent = True
            if old_id:
                appt = db.get(Appointment, old_id)
                if (
                    not appt
                    or appt.clinic_id != clinic.id
                    or appt.contact != thread.contact
                    or appt.status != "confirmed"
                ):
                    raise HTTPException(409, "Original appointment is no longer active")
                appt.consent = True
                reschedule(db, clinic, appt, starts)
                audit(db, "patient", "appointment.rescheduled", appt.id, clinic.id)
            else:
                appt = book(
                    db,
                    clinic,
                    thread,
                    starts,
                    f"confirm:{clinic.id}:{event_key}",
                    source="demo" if thread.channel == "demo" else "whatsapp",
                )
        except HTTPException:
            thread.state = {}
            return "That slot is no longer available. Reply BOOK to choose again. Nothing new was booked."
        thread.state = {}
        return f"Confirmed: appointment #{appt.id}, {readable(clinic,appt.starts)}, {clinic.doctor}. Address: {clinic.address}. Reply CANCEL or RESCHEDULE if needed."
    if BOOKING.search(t) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        if upcoming and not state.get("appointment_id"):
            return f"You already have appointment #{upcoming.id} on {readable(clinic,upcoming.starts)}. Reply RESCHEDULE, CANCEL or STAFF."
        return offer(db, clinic, thread, t, state.get("appointment_id"))
    if state.get("step") == "confirm":
        return "Reply YES to confirm the displayed appointment, NO to keep it unbooked, or STAFF."
    if state.get("step") == "choose":
        return "Reply with a displayed slot number, send a date as YYYY-MM-DD, or reply STAFF."
    if re.search(r"fee|cost|price|entha|ఫీజు|ధర|शुल्क|कीमत", lower):
        return f"Consultation fee is Rs {clinic.fee}. Treatment prices require clinic confirmation. Reply BOOK or STAFF."
    if re.search(r"address|location|where|ekkada|చిరునామా|ఎక్కడ|पता|कहाँ", lower):
        return f"{clinic.name}: {clinic.address}. Phone: {clinic.phone}."
    if re.search(r"timing|hours|open|close|eppudu|సమయం|समय", lower):
        s = clinic.schedule
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f'{clinic.name}: {s["open"]}–{s["close"]} ({clinic.timezone}). Closed: {", ".join(days[d] for d in s["closed_days"]) or "none"}. Reply BOOK for actual availability.'
    if re.search(r"service|treatment|సేవ|सेवा", lower):
        return (
            "Clinic-approved services: "
            + ", ".join(clinic.services)
            + ". Reply STAFF for treatment questions."
        )
    if lower in {"hi", "hello", "hey", "నమస్కారం", "నమస్తే", "नमस्ते"}:
        return f"Hello! I am {clinic.name}’s automated receptionist. Reply BOOK for appointments, FEES, HOURS, ADDRESS or STAFF. For date selection send YYYY-MM-DD."
    # Optional semantic matching returns only a clinic-approved answer, never generated medical advice.
    from .ai import approved_faq

    answer = approved_faq(clinic, t)
    if answer:
        return answer
    takeover(db, thread, "Question outside approved clinic information")
    return f"I have placed this question in the reception queue. Call {clinic.phone} for immediate help."


def inbound(
    db,
    clinic,
    contact,
    name,
    text,
    event_key,
    channel="whatsapp",
    provider_id=None,
    received_at=None,
):
    thread = get_thread(db, clinic, contact, name, channel)
    if provider_id and db.scalar(
        select(Message.id).where(Message.provider_id == provider_id)
    ):
        return thread, None
    # Replay-old requests do not reopen the service window.
    thread.last_inbound = max(
        thread.last_inbound, min(received_at or int(time.time()), int(time.time()))
    )
    thread.updated = int(time.time())
    db.add(
        Message(
            thread_id=thread.id,
            direction="in",
            text=text,
            provider_id=provider_id,
            status="received",
        )
    )
    reply = reception(db, clinic, thread, text, event_key)
    if reply:
        queue_reply(
            db,
            clinic,
            thread,
            reply,
            f"reply:{clinic.id}:{event_key}",
            actor="system" if thread.mode == "human" else "bot",
        )
    return thread, reply

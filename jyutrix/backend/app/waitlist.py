"""Consent-based cancellation waitlist with one-at-a-time, expiring offers."""

from datetime import timedelta
import time
from fastapi import HTTPException
from sqlalchemy import select
from .database import WaitlistEntry, Thread, Doctor, Service, Job, Appointment, audit
from .booking import check_slot, readable, book
from .entitlements import limits_for

OFFER_SECONDS = 20 * 60
SERVICE_WINDOW_SECONDS = 24 * 3600


def join_waitlist(db, clinic, thread):
    if not limits_for(clinic)["waitlist"]:
        return None
    existing = db.scalar(
        select(WaitlistEntry)
        .where(
            WaitlistEntry.clinic_id == clinic.id,
            WaitlistEntry.contact == thread.contact,
            WaitlistEntry.status.in_(["waiting", "offered"]),
        )
        .order_by(WaitlistEntry.created.desc())
        .limit(1)
    )
    if existing:
        return existing
    entry = WaitlistEntry(
        clinic_id=clinic.id,
        thread_id=thread.id,
        name=thread.name,
        contact=thread.contact,
        consent=True,
        status="waiting",
    )
    db.add(entry)
    db.flush()
    audit(db, "patient", "waitlist.joined", entry.id, clinic.id)
    return entry


def offer_waitlist_for_slot(db, clinic, starts, doctor_id, service_id, demo=False):
    if demo or not clinic.enabled or not limits_for(clinic)["waitlist"]:
        return None
    active_offer = db.scalar(
        select(WaitlistEntry.id).where(
            WaitlistEntry.clinic_id == clinic.id,
            WaitlistEntry.status == "offered",
            WaitlistEntry.offered_starts == starts,
            WaitlistEntry.offered_doctor_id == doctor_id,
        ).limit(1)
    )
    if active_offer:
        return active_offer
    try:
        check_slot(db, clinic, starts, doctor_id=doctor_id, service_id=service_id)
    except HTTPException:
        return None
    doctor = db.get(Doctor, doctor_id) if doctor_id else None
    service = db.get(Service, service_id) if service_id else None
    if not doctor or not service:
        return None
    candidates = list(
        db.scalars(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.clinic_id == clinic.id,
                WaitlistEntry.status == "waiting",
                WaitlistEntry.consent.is_(True),
                (WaitlistEntry.doctor_id.is_(None) | (WaitlistEntry.doctor_id == doctor_id)),
                (WaitlistEntry.service_id.is_(None) | (WaitlistEntry.service_id == service_id)),
            )
            .order_by(WaitlistEntry.created, WaitlistEntry.id)
        )
    )
    now = int(time.time())
    for entry in candidates:
        thread = db.get(Thread, entry.thread_id)
        if (
            not thread
            or thread.channel != "whatsapp"
            or thread.opted_out
            or thread.mode != "bot"
            or thread.last_inbound < now - SERVICE_WINDOW_SECONDS
        ):
            continue
        state = thread.state or {}
        if state and state.get("expires", now + 1) > now:
            # Never overwrite an in-progress booking, consent or staff-directed flow.
            continue
        entry.status = "offered"
        entry.offered_starts = starts
        entry.offered_doctor_id = doctor_id
        entry.offered_service_id = service_id
        entry.expires = now + OFFER_SECONDS
        thread.state = {
            "step": "waitlist_offer",
            "waitlist_id": entry.id,
            "starts": starts,
            "doctor_id": doctor_id,
            "service_id": service_id,
            "expires": entry.expires,
        }
        message = (
            f"A cancellation slot is available: {readable(clinic, starts)} with "
            f"{doctor.name} for {service.name}. Reply YES within 20 minutes to request it; "
            "it is not reserved until confirmed. Reply NO to pass."
        )
        from .receptionist import queue_reply

        queue_reply(db, clinic, thread, message, f"waitlist-offer:{entry.id}:{starts}", actor="bot")
        audit(db, "system", "waitlist.slot_offered", entry.id, clinic.id)
        return entry.id
    return None


def accept_waitlist_offer(db, clinic, thread, event_key):
    state = thread.state or {}
    entry = db.get(WaitlistEntry, state.get("waitlist_id"))
    now = int(time.time())
    if (
        not entry
        or entry.clinic_id != clinic.id
        or entry.thread_id != thread.id
        or entry.status != "offered"
        or not entry.expires
        or entry.expires <= now
        or entry.offered_starts != state.get("starts")
    ):
        thread.state = {}
        return "That waitlist offer has expired or is no longer available. Reply WAITLIST to join again."
    try:
        appt = book(
            db,
            clinic,
            thread,
            entry.offered_starts,
            f"waitlist:{entry.id}:{event_key}",
            source="whatsapp",
            name=thread.name,
            contact=thread.contact,
            consent=thread.consent,
            doctor_id=entry.offered_doctor_id,
            service_id=entry.offered_service_id,
        )
    except HTTPException:
        entry.status = "expired"
        thread.state = {}
        return "Sorry, that slot was just filled. Reply WAITLIST to join again or STAFF for help."
    entry.status = "booked"
    entry.expires = None
    thread.state = {}
    audit(db, "patient", "waitlist.offer_accepted", entry.id, clinic.id)
    return f"Confirmed: appointment #{appt.id}, {readable(clinic, appt.starts)} with the clinic. Reception can help with any changes."


def decline_waitlist_offer(db, clinic, thread):
    state = thread.state or {}
    entry = db.get(WaitlistEntry, state.get("waitlist_id"))
    if entry and entry.clinic_id == clinic.id and entry.thread_id == thread.id and entry.status == "offered":
        entry.status = "declined"
        entry.expires = None
        audit(db, "patient", "waitlist.offer_declined", entry.id, clinic.id)
        offer_waitlist_for_slot(
            db,
            clinic,
            entry.offered_starts,
            entry.offered_doctor_id,
            entry.offered_service_id,
        )
    thread.state = {}
    return "No problem. This offer is released. Reply WAITLIST if you would like to join again."


def expire_waitlist_offer(db, clinic, entry):
    if entry.status != "offered" or not entry.expires or entry.expires > time.time():
        return False
    thread = db.get(Thread, entry.thread_id)
    if (
        thread
        and thread.mode == "bot"
        and (thread.state or {}).get("waitlist_id") == entry.id
    ):
        thread.state = {}
        from .receptionist import queue_reply

        queue_reply(
            db,
            clinic,
            thread,
            "The cancellation slot offer has expired. Reply WAITLIST if you would like to join again.",
            f"waitlist-expired:{entry.id}",
            actor="bot",
        )
    starts, doctor_id, service_id = (
        entry.offered_starts,
        entry.offered_doctor_id,
        entry.offered_service_id,
    )
    entry.status = "expired"
    entry.expires = None
    audit(db, "system", "waitlist.offer_expired", entry.id, clinic.id)
    offer_waitlist_for_slot(db, clinic, starts, doctor_id, service_id)
    return True

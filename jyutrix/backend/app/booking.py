from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import time
from fastapi import HTTPException
from sqlalchemy import select
from .database import Appointment, Job, Thread, audit

ACTIVE = ("confirmed",)


def local_time(clinic, stamp):
    return datetime.fromtimestamp(stamp, ZoneInfo(clinic.timezone))


def readable(clinic, stamp):
    return local_time(clinic, stamp).strftime("%a %d %b, %I:%M %p")


def as_stamp(value, clinic):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(clinic.timezone))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        raise HTTPException(422, "Use an ISO date and time")


def check_slot(db, clinic, starts, exclude=None, demo=False):
    if starts <= int(time.time()):
        raise HTTPException(409, "Choose a future appointment")
    dt = local_time(clinic, starts)
    schedule = clinic.schedule
    if dt.weekday() in schedule["closed_days"] or dt.date().isoformat() in schedule.get(
        "leave_dates", []
    ):
        raise HTTPException(409, "Clinic closed on this date")
    minutes = dt.hour * 60 + dt.minute

    def mins(x):
        h, m = map(int, x.split(":"))
        return h * 60 + m

    opening, closing = mins(schedule["open"]), mins(schedule["close"])
    duration = schedule["duration"]
    if (
        dt.second
        or minutes < opening
        or minutes + duration > closing
        or (minutes - opening) % duration
    ):
        raise HTTPException(
            409, "Choose a slot aligned with clinic hours and appointment duration"
        )
    for gap in schedule.get("breaks", []):
        if minutes < mins(gap["end"]) and minutes + duration > mins(gap["start"]):
            raise HTTPException(409, "Appointment overlaps clinic break")
    ends = starts + duration * 60
    query = select(Appointment.id).where(
        Appointment.clinic_id == clinic.id,
        Appointment.status.in_(ACTIVE),
        (Appointment.source == "demo" if demo else Appointment.source != "demo"),
        Appointment.starts < ends,
        Appointment.ends > starts,
    )
    if exclude:
        query = query.where(Appointment.id != exclude)
    if db.scalar(query.limit(1)):
        raise HTTPException(409, "Slot just filled. Choose another time.")
    return ends


def available(db, clinic, target=None, count=6, exclude=None, demo=False):
    today = local_time(clinic, time.time()).date()
    target = target or today
    if target < today or target > today + timedelta(days=60):
        raise HTTPException(422, "Choose a date within the next 60 days")
    result = []
    for day_offset in range(14 if target == today else 1):
        day = target + timedelta(days=day_offset)
        h, m = map(int, clinic.schedule["open"].split(":"))
        slot = datetime.combine(
            day, datetime.min.time(), tzinfo=ZoneInfo(clinic.timezone)
        ).replace(hour=h, minute=m)
        while slot.date() == day:
            stamp = int(slot.timestamp())
            try:
                check_slot(db, clinic, stamp, exclude, demo=demo)
                result.append(stamp)
            except HTTPException:
                pass
            if len(result) >= count:
                return result
            slot += timedelta(minutes=clinic.schedule["duration"])
            if slot.strftime("%H:%M") >= clinic.schedule["close"]:
                break
    return result


def reminder_jobs(db, clinic, appt, thread):
    if not thread or thread.channel != "whatsapp" or not appt.consent:
        return
    for hours in (24, 2):
        due = appt.starts - hours * 3600
        if due <= time.time():
            continue
        db.add(
            Job(
                clinic_id=clinic.id,
                thread_id=thread.id,
                appointment_id=appt.id,
                key=f"reminder:{appt.id}:{appt.starts}:{hours}",
                kind="reminder",
                due=due,
                payload={"starts": appt.starts},
            )
        )


def book(
    db,
    clinic,
    thread,
    starts,
    request_key,
    source="whatsapp",
    name=None,
    contact=None,
    consent=False,
):
    existing = db.scalar(
        select(Appointment).where(
            Appointment.request_key == request_key, Appointment.clinic_id == clinic.id
        )
    )
    if existing:
        return existing
    ends = check_slot(db, clinic, starts, demo=source == "demo")
    appt = Appointment(
        clinic_id=clinic.id,
        thread_id=thread.id if thread else None,
        name=name or thread.name,
        contact=contact or thread.contact,
        starts=starts,
        ends=ends,
        source=source,
        request_key=request_key,
        consent=consent or bool(thread and thread.consent),
    )
    db.add(appt)
    db.flush()
    reminder_jobs(db, clinic, appt, thread)
    audit(db, source, "appointment.created", appt.id, clinic.id)
    return appt


def cancel_reminders(db, appt):
    for job in db.scalars(
        select(Job).where(
            Job.appointment_id == appt.id,
            Job.status.in_(["pending", "retry", "dispatching"]),
        )
    ):
        job.status = "cancelled"


def reschedule(db, clinic, appt, starts):
    ends = check_slot(db, clinic, starts, appt.id, demo=appt.source == "demo")
    cancel_reminders(db, appt)
    appt.starts = starts
    appt.ends = ends
    reminder_jobs(
        db, clinic, appt, db.get(Thread, appt.thread_id) if appt.thread_id else None
    )

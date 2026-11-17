from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import time
from fastapi import HTTPException
from sqlalchemy import select
from .database import Appointment, Job, Thread, Doctor, Service, audit
from .entitlements import limits_for

ACTIVE = ("confirmed",)


def ensure_catalog(db, clinic):
    doctors = list(
        db.scalars(
            select(Doctor).where(Doctor.clinic_id == clinic.id).order_by(Doctor.id)
        )
    )
    if not doctors:
        doctors = [
            Doctor(clinic_id=clinic.id, name=clinic.doctor, schedule=clinic.schedule)
        ]
        db.add(doctors[0])
        db.flush()
    services = list(
        db.scalars(
            select(Service).where(Service.clinic_id == clinic.id).order_by(Service.id)
        )
    )
    if not services:
        services = [
            Service(
                clinic_id=clinic.id,
                name=(clinic.services or ["General consultation"])[0][:120],
                duration=clinic.schedule.get("duration", 30),
                fee=clinic.fee,
            )
        ]
        db.add(services[0])
        db.flush()
    return doctors, services


def default_doctor(db, clinic):
    doctors, _ = ensure_catalog(db, clinic)
    return next((doctor for doctor in doctors if doctor.active), None)


def default_service(db, clinic):
    _, services = ensure_catalog(db, clinic)
    return next((service for service in services if service.active), None)


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


def check_slot(db, clinic, starts, exclude=None, demo=False, doctor_id=None, service_id=None):
    if starts <= int(time.time()):
        raise HTTPException(409, "Choose a future appointment")
    doctors, services = ensure_catalog(db, clinic)
    doctor = db.get(Doctor, doctor_id) if doctor_id else next((d for d in doctors if d.active), None)
    service = db.get(Service, service_id) if service_id else next((s for s in services if s.active), None)
    if not doctor or doctor.clinic_id != clinic.id or not doctor.active:
        raise HTTPException(422, "Choose an active doctor for this clinic")
    if not service or service.clinic_id != clinic.id or not service.active:
        raise HTTPException(422, "Choose an active service for this clinic")
    dt = local_time(clinic, starts)
    schedule = doctor.schedule
    if dt.weekday() in schedule["closed_days"] or dt.date().isoformat() in schedule.get(
        "leave_dates", []
    ):
        raise HTTPException(409, f"{doctor.name} is unavailable on this date")
    minutes = dt.hour * 60 + dt.minute

    def mins(x):
        h, m = map(int, x.split(":"))
        return h * 60 + m

    opening, closing = mins(schedule["open"]), mins(schedule["close"])
    duration = service.duration
    if (
        dt.second
        or minutes < opening
        or minutes + duration > closing
        or (minutes - opening) % duration
    ):
        raise HTTPException(
            409, "Choose a slot aligned with doctor hours and service duration"
        )
    for gap in schedule.get("breaks", []):
        if minutes < mins(gap["end"]) and minutes + duration > mins(gap["start"]):
            raise HTTPException(409, "Appointment overlaps doctor break")
    ends = starts + duration * 60
    query = select(Appointment.id).where(
        Appointment.clinic_id == clinic.id,
        Appointment.doctor_id == doctor.id,
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


def available(db, clinic, target=None, count=6, exclude=None, demo=False, doctor_id=None, service_id=None):
    doctors, services = ensure_catalog(db, clinic)
    doctor = db.get(Doctor, doctor_id) if doctor_id else next((d for d in doctors if d.active), None)
    service = db.get(Service, service_id) if service_id else next((s for s in services if s.active), None)
    if not doctor or doctor.clinic_id != clinic.id or not doctor.active:
        raise HTTPException(422, "Choose an active doctor for this clinic")
    if not service or service.clinic_id != clinic.id or not service.active:
        raise HTTPException(422, "Choose an active service for this clinic")
    today = local_time(clinic, time.time()).date()
    target = target or today
    if target < today or target > today + timedelta(days=60):
        raise HTTPException(422, "Choose a date within the next 60 days")
    result = []
    for day_offset in range(14 if target == today else 1):
        day = target + timedelta(days=day_offset)
        h, m = map(int, doctor.schedule["open"].split(":"))
        slot = datetime.combine(
            day, datetime.min.time(), tzinfo=ZoneInfo(clinic.timezone)
        ).replace(hour=h, minute=m)
        while slot.date() == day:
            stamp = int(slot.timestamp())
            try:
                check_slot(db, clinic, stamp, exclude, demo=demo, doctor_id=doctor.id, service_id=service.id)
                result.append(stamp)
            except HTTPException:
                pass
            if len(result) >= count:
                return result
            slot += timedelta(minutes=service.duration)
            if slot.strftime("%H:%M") >= doctor.schedule["close"]:
                break
    return result


def reminder_jobs(db, clinic, appt, thread):
    if not thread or thread.channel != "whatsapp" or not appt.consent:
        return
    for hours in limits_for(clinic)["reminder_offsets_hours"]:
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
    doctor_id=None,
    service_id=None,
):
    existing = db.scalar(
        select(Appointment).where(
            Appointment.request_key == request_key, Appointment.clinic_id == clinic.id
        )
    )
    if existing:
        return existing
    doctor = db.get(Doctor, doctor_id) if doctor_id else default_doctor(db, clinic)
    service = db.get(Service, service_id) if service_id else default_service(db, clinic)
    if not doctor or not service:
        raise HTTPException(409, "Clinic needs an active doctor and service")
    ends = check_slot(
        db,
        clinic,
        starts,
        demo=source == "demo",
        doctor_id=doctor.id,
        service_id=service.id,
    )
    appt = Appointment(
        clinic_id=clinic.id,
        doctor_id=doctor.id,
        service_id=service.id,
        service_fee=service.fee,
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


def reschedule(db, clinic, appt, starts, doctor_id=None, service_id=None):
    target_doctor = doctor_id if doctor_id is not None else appt.doctor_id
    target_service = service_id if service_id is not None else appt.service_id
    ends = check_slot(
        db,
        clinic,
        starts,
        appt.id,
        demo=appt.source == "demo",
        doctor_id=target_doctor,
        service_id=target_service,
    )
    cancel_reminders(db, appt)
    if target_service != appt.service_id:
        selected_service = db.get(Service, target_service)
        appt.service_fee = selected_service.fee if selected_service else clinic.fee
    appt.starts = starts
    appt.ends = ends
    appt.reminder_response = "pending"
    appt.reminder_response_at = None
    appt.doctor_id = target_doctor
    appt.service_id = target_service
    reminder_jobs(
        db, clinic, appt, db.get(Thread, appt.thread_id) if appt.thread_id else None
    )

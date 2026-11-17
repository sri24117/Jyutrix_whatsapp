# Jyutrix — Clinic Reception MVP

A clinic reception workspace built from the uploaded Jyutrix prototype: confirmed appointments, clinic-approved information, a staff inbox, Kapso WhatsApp messaging, consent-aware reminders and honest operational reporting.

**Release status:** deployable MVP candidate. Local automated checks are included. Production activation requires your Kapso credentials, a clinic-owned number, an approved reminder template, PostgreSQL deployment tests and the live acceptance checklist. No real clinic traffic or paid-pilot results have been verified by this build.

## Run locally on Windows

From this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** for the public landing page, and **http://localhost:8000/app** for the clinic workspace. Local demo credentials: **admin / demo-reception-2026**. These credentials are local-demo only; production explicitly rejects this password. A fictional dental clinic is created only in demo mode.

The landing page includes a demo-request form. Requests are saved in the workspace under **Sales enquiries** (administrator only), where you can track follow-up stages or delete the contact record. Do not submit patient or health information through this form.

The **Try the demo** screen uses separate conversation records and never sends real WhatsApp messages. Send `BOOK`, then `1`, then `YES`. Try `RESCHEDULE`, `CANCEL` then `YES`, `STAFF`, and `STOP`. Use Conversations → Resume automation to release staff control.

For WhatsApp testing, run the durable worker in a second terminal:

```powershell
.\.venv\Scripts\python.exe -m backend.app.worker
```

Mac/Linux equivalents:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
# second terminal
.venv/bin/python -m backend.app.worker
```

## What is included

- Responsive public marketing site with consent-based demo requests and an administrator-only enquiry follow-up board.
- Responsive admin, owner and receptionist workspace.
- Persistent cookie sessions, CSRF protection, password hashing, login throttling and revocation.
- Clinic isolation on all patient, appointment, job and report APIs.
- Timezone-aware appointments, schedule breaks, closed days, leave dates and duration validation.
- Choose a slot and explicitly confirm before booking; confirm cancellation; atomic rescheduling.
- Clinic-level transaction locks and PostgreSQL exclusion constraints against overlapping confirmed bookings.
- Kapso **Meta-format** webhooks, HMAC verification, durable inbox, provider-message and delivery deduplication.
- Outbox with bounded retry for known transient errors, delivery receipts and operator review for uncertain sends.
- Staff takeover cancels pending automated replies; incoming messages remain visible.
- Reminder template jobs with permission, opt-out, cancellation and reschedule checks.
- Manual phone/walk-in bookings in the same schedule.
- Demo-excluded reports, staff-recorded attendance and payments, message allowance and plan expiry.
- Audit log API, patient deletion, retention task, credential recovery, Docker Compose and HTTPS proxy.
- Optional OpenAI semantic matching that returns only approved FAQ answers.
- Optional authenticated voice tools for information, availability and staff-reviewed requests.

## Deliberate scope

One doctor and one schedule per clinic. Reception staff must enter phone/walk-in bookings in this schedule. External calendars are not synchronized. Billing is manually tracked, not payment collection. The deterministic flow recognizes selected Telugu/Hindi expressions but responses are English; full local-language conversational quality is not claimed. Media and unsupported questions go to staff. SMS, voice telephony, WhatsApp Calling media, prescriptions, diagnosis, EHR and bulk marketing are not implemented.

Voice endpoint support is a server integration boundary, not a connected phone agent. It cannot authenticate callers or automatically cancel/confirm phone appointments. See `docs/VOICE.md`.

## Production deployment

Read **docs/DEPLOYMENT.md**, **docs/KAPSO.md** and **docs/LIVE_ACCEPTANCE.md** in that order. Use PostgreSQL; production refuses SQLite and weak/default credentials. New production databases start empty. Add your real clinic and staff accounts through the dashboard.

The original prototype’s schema is not compatible with this version. This package does not auto-import its sample clinics, fake revenue or patient data. To import genuine historical records, take a backup and use a reviewed migration with clinic ownership and consent mapping; do not point this app at the old SQLite database.

Run tests:

```bash
python -m pytest tests -q
node --check frontend/app.js
# With a disposable PostgreSQL database:
TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/test_db python -m pytest tests -q
```

The test suite DROPS its selected test database tables. Never set `TEST_DATABASE_URL` to a live database. CI provides a disposable PostgreSQL service and runs both database paths.

## Selling the pilot

Use the scoped pilot offer and onboarding process in **docs/PILOT_OFFER.md**. A paid pilot is a commercial experiment, not evidence of product-market fit. Track attended appointments and staff workload, and review renewal willingness before expanding scope.

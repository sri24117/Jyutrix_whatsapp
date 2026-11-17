# Release verification — 14 October 2026

## Website and enquiry workflow follow-up — 17 November 2026

- Added the public marketing page at `/` and moved the clinic workspace to `/app`.
- Added a public, consent-checked demo request form with IP rate limiting and a honeypot; enquiries are administrator-only, stage-tracked, deletable, and included in the configured retention task.
- Fixed the production configuration so it rejects the public local-demo admin password.
- Updated Hostinger KVM / `jyutrix.cloud` deployment instructions and canonical `www` redirect.
- Local verification after these changes: **56 backend tests passed** on SQLite; DOM UI smoke passed; both frontend scripts passed `node --check`. Docker/PostgreSQL and real deployment remain unverified in this environment.

## Completed in the original build

- **53 automated backend tests passed** on a disposable SQLite database, Python 3.12.
- **DOM integration smoke passed** against an actual disposable FastAPI HTTP server: 50 requests covering login, overview, slot choice/confirmation, settings save, injected HTML rendered as text, appointment list, inbox takeover, staff reply, delivery view and logout.
- JavaScript syntax check passed.
- Python compilation/import succeeded.
- Compose and CI YAML parsed successfully.
- Local SQLite backup/restore passed integrity and appointment count checks.
- Runtime dependencies are pinned in requirements.lock.txt; application and UI source formatted for review.

Backend tests include authentication/CSRF, session persistence and revocation, clinic isolation, login throttling, explicit appointment confirmation, overlap/grid/break validation, eight simultaneous booking attempts with one winner, cancellation/rescheduling, webhook signature checks, duplicate delivery/message IDs, media/BSUID normalization, staff takeover, uncertain sends, worker-crash quarantine, known-transient retry, service-window checks, template/consent reminder handling, STOP, message allowance, production fail-closed configuration, demo/live report and slot separation, patient deletion and restricted voice requests.

## Not yet verified

- Live Kapso sends/receives, template approval, billing, number health or WhatsApp coexistence.
- Real audio/telephony, call transfers, voice-agent configuration or caller verification.
- PostgreSQL execution and its exclusion constraint in this environment. PostgreSQL CI service and commands are included, but CI has not been run remotely.
- Docker image/Compose startup, HTTPS issuance, or PostgreSQL backup restore on a deployment host. Docker/PostgreSQL services were unavailable here.
- Real-browser layout, keyboard behavior and mobile screenshots. Browser binary download was unavailable; DOM checks do not prove visual layout or browser CSP behavior.
- Live OpenAI matching or comprehensive local-language quality.
- Security penetration testing, provider-specific onboarding, clinic policy/legal approval, or paid customer outcomes.

One installed test-client dependency emitted an httpx deprecation warning; tests completed successfully. It is not evidence of a runtime failure.

## Release decision

This is a deployable, locally tested MVP candidate, not a production-certified system. Complete LIVE_ACCEPTANCE.md in staging and with the pilot clinic before accepting routine patient traffic. Do not advertise unverified language, voice, revenue recovery or compliance claims.

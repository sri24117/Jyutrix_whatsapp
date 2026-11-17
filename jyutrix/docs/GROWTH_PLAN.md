# Growth plan — staged product scope

This document separates capabilities implemented in the current code from proposed commercial packaging. Prices are hypotheses from the pilot offer, not market-validated rates. Do not sell features marked future.

## Shipped in this Growth foundation

- Doctor records with individual opening hours, breaks, closed days and leave dates.
- Service records with their own duration and fee.
- Patient booking flow asks for a service and doctor when there is more than one, then requires an explicit slot choice and YES confirmation.
- Overlap prevention is per doctor, so two doctors can take appointments at the same time while one doctor cannot be double-booked.
- Staff booking form chooses doctor and service and displays them in appointment records.
- Server-side plan limits: Starter/Pilot permits one active doctor and ten services; Growth permits five active doctors and thirty services.
- Growth schedules 24-hour and three-hour reminders when patient permission, WhatsApp consent and the clinic's approved template all permit it. Starter preserves the existing 24-hour/two-hour schedule.
- Growth cancellation waitlist: patients explicitly consent on WhatsApp; eligible, opted-in threads can receive one suitable freed-slot offer at a time with a 20-minute expiry. YES is checked through the existing booking conflict rules; NO, STOP and expiry release the offer. Only sends inside WhatsApp's 24-hour service window are automated; otherwise reception must follow its approved-template process. The dashboard exposes waiting/offered/booked/cancelled states and staff removal.
- Optional interactive reminder template with signed Confirm/Reschedule/Cancel quick replies. Confirm is recorded; Reschedule uses the normal slot and explicit-confirmation flow; Cancel requires a second YES. Clinics must separately approve and enable the button template; the existing reminder template remains the default. Local mocks/tests do not verify live Meta/Kapso template behavior.
- Clinic owners can configure catalog items and schedules. The admin alone assigns a paid plan in Clinic settings. This is an entitlement gate, not subscription billing.

## Proposed packaging — do not treat as a live price list

| Plan | Proposed positioning | Proposed price | Current entitlement status |
|---|---|---:|---|
| Starter | One doctor, one schedule, core WhatsApp reception | ₹4,999/month | One active doctor; up to ten services |
| Growth | Up to five doctors, service-specific durations, expanded reminder timing | ₹8,999/month | Five active doctors; up to thirty services |
| Pro | Multiple branches and integrations | ₹14,999–₹19,999/month | Not released; do not assign/sell yet |

Message-provider usage, taxes, onboarding and support terms must be agreed separately. Current code has no subscription payment collection or automated billing.

## Not shipped yet

- Razorpay deposits or payment links.
- Branches, cross-branch isolation, calendar sync or clinic-software integrations.
- Post-visit follow-up campaigns, Google review workflow or full Telugu/Hindi conversations.
- Growth conversion attribution. Existing reports are operational counts; they do not establish causal revenue lift.
- Self-serve plan upgrades, subscription billing or payment reconciliation.

## Remaining build sequence

1. Verify the locally implemented waitlist in staging with consent, offer expiry, decline, STOP, staff removal, race/conflict checks, approved-template rules and patient-data retention. Live WhatsApp delivery is not verified here.
2. Verify both reminder templates and signed Confirm/Reschedule/Cancel button payloads with an approved Meta/Kapso template and a real clinic number. Local mocks do not verify live provider behavior.
3. Add Razorpay deposits/payment links only with signed webhook verification, idempotency, payment status reconciliation, staff-led refunds and tested failure paths. Credentials can be added after local implementation.
4. Add conversion funnel analytics with reviewed event definitions and distinguish association from causation.
5. Add Pro/multi-location only with branch and role models, tenant-boundary tests and owner views. Keep Pro unavailable until those controls are verified.
6. Before any wider pilot, verify multi-doctor scheduling with staff and fictional patients, then measure setup time, booking completion, attendance and receptionist workload.

## Database rollout

The app adds the doctor/service tables and appointment foreign-key columns idempotently on startup, then backfills legacy appointments to each clinic's first doctor and service. This database migration has been exercised on SQLite in local tests; run a backup-first staging test on the actual PostgreSQL version before production rollout. The PostgreSQL exclusion constraint is updated to serialize overlap by doctor.

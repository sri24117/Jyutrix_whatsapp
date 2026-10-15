# Connect Kapso WhatsApp

Use a clinic-owned WhatsApp account. Keep KAPSO_API_KEY and KAPSO_WEBHOOK_SECRET on the server; never paste them into patient messages or frontend code.

1. Connect your clinic number in Kapso and obtain its WhatsApp phone number ID.
2. Set that ID in Clinic settings. Leave automation paused initially.
3. Set a random shared webhook secret (32+ characters) on your app/worker deployment.
4. Register a **kind=meta** webhook to `https://YOUR_DOMAIN/webhooks/kapso/CLINIC_ID`.

From the deployment:

```bash
docker compose exec app python -m scripts.register_kapso --clinic-id 1 --number-id YOUR_NUMBER_ID
```

The helper uses Kapso's documented Platform endpoint and registers the chosen secret. Only one Meta webhook per number is supported; update an existing receiver through Kapso rather than creating duplicates. The receiver deliberately rejects the default Kapso v2 event payload: use **Meta-format**, not Kapso-format, for this release. This provides incoming messages and delivery status updates through one documented payload shape.

Webhook verification uses the exact raw body and hex HMAC-SHA256 from `X-Webhook-Signature`; the clinic number inside the signed body must match its configured number ID. Both `X-Idempotency-Key` and provider message IDs are deduplicated in persistent storage.

## Reminder template

Create an approved UTILITY template named `jyutrix_appointment_reminder` (or set REMINDER_TEMPLATE), language `en_US` (or your exact approved language). Use numbered body parameters in this order:

> Hello {{1}}, reminder from {{2}}: your appointment is {{3}}. Reference {{4}}. Reply RESCHEDULE or CANCEL to change it, or STOP to stop appointment messages.

Parameters: patient name, clinic name, clinic-local appointment time, appointment reference. Confirm approval in Kapso, then enable “Reminder template approved” in clinic settings. Jobs are scheduled 24 hours and 2 hours before the appointment when those times are still in the future. Reminder messages always use this template.

Normal replies are sent only inside the latest inbound 24-hour service window. A closed window is surfaced as a staff issue; this release does not invent a generic template to reopen it. STOP withdraws reminder permission and cancels pending notifications; START permits conversation to resume but does not automatically restore reminder consent. A later booking confirmation explicitly captures permission.

If using WhatsApp Business App coexistence, staff must use this application's takeover controls before replying. This release does not detect messages typed directly in the phone app as a takeover signal. For a reliable initial pilot, route staff replies through the Jyutrix inbox and disable other automatic responders on the same number.

Media messages are routed to staff rather than transcribed or interpreted. BSUID-only contact identities are preserved and passed using the documented recipient field; test this route with your account as it becomes relevant. We never fabricate a phone number.

## Source contracts checked during implementation

- https://docs.kapso.ai/docs/platform/webhooks/overview
- https://docs.kapso.ai/docs/platform/webhooks/security
- https://docs.kapso.ai/docs/whatsapp/send-messages/text
- https://docs.kapso.ai/docs/whatsapp/templates/simple-text
- https://docs.kapso.ai/api/platform/v1/webhooks/create-webhook

API version defaults to v24.0, matching the retrieved examples. Revalidate provider contracts before changing it. Sending and receiving have been tested with local fixtures and mocks; no live Kapso account was connected in this build.

# Optional voice integration boundary

The release includes `POST /api/voice/tools`, protected by an optional server-only VOICE_TOOL_KEY. Do not expose it to browsers. Configure your chosen voice runtime to call it with `Authorization: Bearer YOUR_KEY` and a fixed clinic_id for that agent.

Actions:
- `info`: returns approved clinic facts and schedule.
- `availability`: optional date (YYYY-MM-DD), returns real available slots.
- `request`: requires contact/name/call_id; optionally scheduled_at. Creates a staff-review request and returns `pending_staff_confirmation`.

Example body:

```json
{"clinic_id":1,"action":"request","contact":"+919999000001","name":"Demo caller","call_id":"call-unique-reference","scheduled_at":"2026-11-02T10:00:00+05:30"}
```

Tell the caller the appointment is a request awaiting staff confirmation. Caller ID alone is not authentication; phone cancellations, automatic confirmation and patient-specific appointment retrieval are intentionally unavailable here. Appointment records remain controlled by Jyutrix's booking engine.

No Retell agent, phone number, SIP trunk, telephony transfer, recording pipeline or WhatsApp Calling media runtime is connected in this package. Those require provider setup, number eligibility, consent and real audio acceptance tests. Do not sell voice as an activated feature until that separate work is completed. The optional endpoint is ready for a supervised integration test.

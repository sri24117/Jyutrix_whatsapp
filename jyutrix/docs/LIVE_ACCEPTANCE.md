# Clinic go-live acceptance

Record clinic, operator, date and evidence for each check. Use fictional patient details until the clinic approves real use. This is an execution checklist, not a claim that the tests already passed.

## Infrastructure

- HTTPS and certificate work; database reports PostgreSQL.
- No publicly accessible PostgreSQL port; app keys absent from browser responses.
- Worker heartbeat stays healthy; restart app/worker and verify persistent records/sessions.
- Create a second clinic and confirm each staff account sees only its assigned clinic.
- Take a backup and successfully restore into staging.

## Real messaging

- Incoming WhatsApp enquiry appears with correct clinic/name/contact.
- Correct fee/address/hours replies arrive on the patient phone.
- Bad webhook signature is rejected; repeated inbound message yields one response workflow.
- BOOK offers actual slots, choice does not book, YES confirms exactly one appointment.
- Two phones competing for one slot produce one winner and a fresh-slot prompt for the other.
- Walk-in/staff bookings remove the slot from WhatsApp availability.
- Requested date, clinic timezone, breaks, leave and closed days are accurate.
- CANCEL requires confirmation; RESCHEDULE updates the existing appointment.
- Staff takeover prevents further automated replies; staff reply is delivered; resume works.
- Unsupported media and medical questions enter the staff queue.
- STOP cancels reminders and future automatic replies; START does not silently restore reminder consent.
- An approved real reminder arrives. Reschedule/cancel prevents the old reminder.
- Delivered/read receipts update status; failures appear in the delivery queue.
- A closed 24-hour window does not send free-form text.
- Simulate a provider interruption in staging; uncertain sends require manual reconciliation.

## Clinic operations

- Reception is trained to enter all phone and walk-in bookings here.
- Staff coverage and urgent-question routing are agreed. Patients are told not to wait for WhatsApp during emergencies.
- Privacy notice, consent wording, retention, deletion and backup access are approved by the clinic under its applicable requirements.
- Pilot scope, allowance, support hours, subscription and renewal are written down.
- Demo figures are excluded from live reports; completed visits and payments are entered accurately.
- Monitoring and an owner contact exist for failures. Have a fallback: pause automation and use reception while incidents are resolved.

Do not call the pilot production-verified until all applicable items pass. Provider signup, number eligibility, templates and billing can delay activation independently of the application code.

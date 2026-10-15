# Implementation notes

The original dashboard and clinic workflow informed this release; the unsafe prototype backend was replaced with an explicit production/demo separation. The frontend remains plain HTML/CSS/JavaScript, avoiding an unnecessary framework migration.

## Storage and transactions

SQLAlchemy uses SQLite for local demo/tests and psycopg PostgreSQL for production. Production schema initialization installs btree_gist and a conditional GiST exclusion constraint on clinic + demo/live bucket + appointment range. Application bookings lock the clinic row before checking/inserting/rescheduling. SQLite uses BEGIN IMMEDIATE for test/demo mutation serialization and an exact-start unique expression index. New bookings align with slot duration and cannot span breaks/closing time.

Sessions contain hashed random tokens and persistent expiry; passwords use scrypt with random salt. Cookies are HttpOnly, SameSite=Strict and Secure in production; state-changing staff APIs require a per-session CSRF token. Authentication, role restrictions and tenant checks happen on the backend. Login throttling is database-backed; PostgreSQL advisory locks serialize rate buckets.

## Inbound messages

A per-clinic, HMAC-verified Meta-format webhook verifies number ownership, persists delivery IDs and normalized incoming message jobs, then acknowledges. A separate worker processes incoming jobs in clinic order. The conversation state, incoming message, booking and outgoing reply are committed together. Provider message IDs prevent repeated processing even if delivery keys change. Completed inbound job payloads are cleared.

## Outbound messages

Each reply/reminder has a durable job key. The worker commits a dispatch claim before sending, then locks the clinic while checking takeover, consent, window, allowance and booking status. Provider calls are bounded. Known pre-send connectivity failures and HTTP 429 can retry with backoff; unknown outcomes are quarantined. Stale dispatch claims after process termination become uncertain, not automatic retries. Accepted is separate from sent/delivered/read. Exactly-once external delivery is not promised.

Staff takeover uses the same clinic lock, increments a conversation generation and cancels old queued bot messages. Resume starts fresh conversation state. Staff messages can be sent while automated replies are paused. If staff also use the WhatsApp phone app, they must explicitly take over in Jyutrix first; this release does not infer phone-app takeover.

## AI boundary

The default receptionist is deterministic. Optional OpenAI matching selects an index from approved administrative FAQs; it never writes generated medical answers, invents fees or controls bookings. Unknown answers and selected urgent/medical terms go to staff. Keyword escalation is a conservative fallback, not a validated clinical triage system. Add a reviewed evaluation set before expanding language or medical-message handling.

## Data and operations

Patient deletion removes local conversation/message/job content and anonymizes appointment contact fields; it leaves non-content audit records. Retention handles aged messages/jobs and inactive threads. Backups and provider-held data require separate policies. Reports exclude demo records and describe operational totals, not causal revenue attribution. Payments and attendance are staff-entered.

The global voice tool key is a trusted server-to-server integration credential with access to all configured clinic tools; do not give it to clinic users. Configure fixed clinic IDs in trusted agent definitions and issue clinic-specific credentials before offering self-service voice onboarding.

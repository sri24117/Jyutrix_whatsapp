# Deploy Jyutrix

## Prerequisites

A Linux host with Docker Compose, a DNS A record for your chosen domain, available ports 80/443, a connected Kapso project/number and clinic-approved appointment information. Docker Compose includes PostgreSQL, application, worker and Caddy HTTPS proxy.

If Coolify already owns ports 80/443, use its proxy for the `app` service and omit the `proxy` service; do not launch a second proxy on those ports. Keep the worker and PostgreSQL services. Give Coolify your Docker Compose source and assign the domain to app port 8000; leave PostgreSQL private. Set all required variables in that deployment.

## Hostinger KVM and jyutrix.cloud

This setup is for a single Ubuntu KVM server. It is a single-node deployment: it does not provide high availability, and a VPS outage interrupts the service until recovered.

1. In Hostinger, point the apex `jyutrix.cloud` A record to the KVM's public IPv4 address. Point `www.jyutrix.cloud` to the same address (A record) or make it a CNAME to the apex. Remove stale conflicting records. Allow inbound TCP 22 (restricted to your admin IP if practical), 80 and 443 in both the Hostinger firewall and the server firewall. Do not open PostgreSQL port 5432.
2. Install a supported Ubuntu LTS image, apply security updates, and install Docker Engine plus the Docker Compose plugin using Docker's official Ubuntu instructions. Keep SSH key authentication enabled and disable password SSH login after verifying key access.
3. Put the project on the server, for example at `/opt/jyutrix`. Copy `.env.example` to `.env`, set permissions to `600`, and replace demo values before starting production. Set `SITE_DOMAIN=jyutrix.cloud` and `PUBLIC_URL=https://jyutrix.cloud`. Caddy serves the apex and redirects `www` to the apex.
4. Generate unique admin and PostgreSQL passwords and provide the Kapso API key and webhook secret. The production app intentionally refuses to start without valid production secrets and PostgreSQL. Do not enable clinic automation merely because the landing page is online.
5. Run Compose from the project directory using the first-deployment commands below. Caddy obtains HTTPS certificates when DNS is correct and ports 80/443 are reachable. The public marketing page is `/`; the clinic workspace is `/app`. Public demo requests are visible under **Sales enquiries** after an administrator signs in.
6. Test `https://jyutrix.cloud`, `https://jyutrix.cloud/app`, `/health`, a demo-request submission, admin lead tracking/deletion, and the clinic go-live checklist before sharing the domain publicly.

Keep the KVM patched. Configure off-server encrypted backups and test restoration; a persistent Docker volume on the same VPS is not a backup. The bundled Compose file keeps the database private and only binds the app to loopback behind Caddy.

## First deployment

1. Extract the source folder on the server, for example `/opt/jyutrix`.
2. Copy `.env.example` to `.env` and set:
   - `APP_MODE=production`.
   - `PUBLIC_URL=https://YOUR_DOMAIN` and `SITE_DOMAIN=YOUR_DOMAIN`.
   - A unique `ADMIN_PASSWORD` of at least 16 characters.
   - A random `POSTGRES_PASSWORD` (prefer URL-safe hex characters).
   - `KAPSO_API_KEY` and `KAPSO_WEBHOOK_SECRET` (32+ characters).
   - Approved `REMINDER_TEMPLATE` and its exact `TEMPLATE_LANGUAGE`.
3. Keep `.env` private (`chmod 600 .env`). Never commit it. Generate random values with `python3 -c "import secrets; print(secrets.token_hex(32))"`.
4. Run:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app worker
curl -fsS https://YOUR_DOMAIN/health
```

Expected health: status `ok`, database `postgresql`, mode `production`. Verify the worker online indicator after signing in. The first administrator is `admin`; the password comes from your environment only at first initialization. Later changes to the environment do not overwrite the stored password. Use the password form or `docker compose exec app python -m scripts.reset_admin` for a trusted recovery.

Database initialization requires permission to create tables and the `btree_gist` extension/exclusion constraint. The supplied PostgreSQL container provisions a database owner able to do that. Managed providers may require their administrator to install `btree_gist` first. Do not silently remove booking constraints to work around a permissions problem.

## Clinic activation

Add the clinic with automation paused. Set hours, leave, fee, services, approved FAQs and contact details. Create an owner/receptionist account bound to this clinic. Connect the number and register its webhook as described in KAPSO.md. Enable automation only for supervised acceptance testing. Mark the reminder template approved only after Meta/Kapso approval. Complete LIVE_ACCEPTANCE.md before opening real patient traffic.

The same backend supports multiple clinics, each with one doctor/schedule. Map each WhatsApp phone number ID to exactly one clinic. Keys stay on the server.

## Backups and recovery

On the Linux host:

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U jyutrix -Fc jyutrix > backups/jyutrix-$(date +%F-%H%M).dump
```

Encrypt backups, store a copy off the host, and apply clinic-approved retention. Run daily through your host scheduler. Test restoration into a separate staging database:

```bash
docker compose exec db createdb -U jyutrix jyutrix_restore_test
docker compose exec -T db pg_restore -U jyutrix -d jyutrix_restore_test < backups/CHOSEN.dump
```

Do not restore into live `jyutrix` during a test. Check row counts, sessions and appointment ranges in staging, then delete the staging database when finished. Backups contain patient data and must be controlled separately from patient deletion in the app.

## Retention and monitoring

Run daily: `docker compose exec -T app python -m scripts.retention`. The default is 90 days for old message/job content, inactive contact anonymization and unconverted public sales enquiries. Confirm the period with each clinic and update `RETENTION_DAYS` to the approved policy. Provider-held content, audit logs, financial records and backups have separate retention requirements; the task does not promise to delete them everywhere.

Monitor HTTPS `/health`, worker health, failed/uncertain jobs, reminder failures, database backups and disk space. Failed incoming events are retained in the job queue. Inspect logs and fix the cause before retrying. Inbound events are kept in order per clinic while retries are pending.

Updates: back up, review changes, run tests in staging, then `docker compose up -d --build app worker`. Initial schema creation is idempotent; future schema changes require an explicit reviewed migration. This release does not implement automatic schema evolution.

## Operational boundaries

The clinic lock serializes mutations and bounded provider requests for a clinic. This intentionally favors correctness for small clinic workloads over maximum throughput. Do not run a bulk broadcast through this outbox. Outbound sends whose result is unknown are quarantined, including dispatches interrupted by a worker crash. Exactly-once external delivery cannot be guaranteed across a database/provider boundary; staff must reconcile uncertain outcomes in Kapso.

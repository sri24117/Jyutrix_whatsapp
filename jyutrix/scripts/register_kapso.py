"""Register a Meta-format Kapso number webhook; credentials are read from .env."""

import argparse
import httpx
from backend.app.config import settings

parser = argparse.ArgumentParser()
parser.add_argument("--clinic-id", type=int, required=True)
parser.add_argument("--number-id", required=True)
args = parser.parse_args()
if (
    not settings.public_url.startswith("https://")
    or not settings.kapso_key
    or len(settings.webhook_secret) < 32
):
    raise SystemExit(
        "Set HTTPS PUBLIC_URL, KAPSO_API_KEY and a 32+ character KAPSO_WEBHOOK_SECRET first"
    )
if not args.number_id.isdigit():
    raise SystemExit("number-id must contain digits")
response = httpx.post(
    f"https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/{args.number_id}/webhooks",
    headers={"X-API-Key": settings.kapso_key},
    timeout=20,
    json={
        "whatsapp_webhook": {
            "kind": "meta",
            "url": f"{settings.public_url}/webhooks/kapso/{args.clinic_id}",
            "secret_key": settings.webhook_secret,
            "active": True,
        }
    },
)
if response.status_code >= 400:
    raise SystemExit(
        f"Kapso returned HTTP {response.status_code}. Check credentials and existing webhook. Only one Meta webhook per number is supported; update an existing receiver in Kapso if necessary."
    )
print(
    "Webhook registered. Set this same number ID in clinic settings and run the live acceptance checklist."
)

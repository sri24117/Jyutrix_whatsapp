import hashlib
import hmac
import httpx
from .config import settings


class ProviderError(Exception):
    def __init__(self, message, retry=False, uncertain=False):
        self.retry = retry
        self.uncertain = uncertain
        super().__init__(message)


def verify(raw, signature):
    return bool(
        settings.webhook_secret
        and signature
        and hmac.compare_digest(
            hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
            signature,
        )
    )


def send(number_id, contact, payload):
    if not settings.kapso_key or not number_id:
        raise ProviderError("Kapso key or clinic number missing")
    # BSUID-only identities are retained; never replace them with a fabricated phone number.
    recipient = (
        {"recipient_type": "individual", "recipient": contact}
        if not contact.lstrip("+").isdigit()
        else {"to": contact.lstrip("+")}
    )
    body = {"messaging_product": "whatsapp", **recipient, **payload}
    try:
        response = httpx.post(
            f"https://api.kapso.ai/meta/whatsapp/{settings.kapso_version}/{number_id}/messages",
            headers={"X-API-Key": settings.kapso_key},
            json=body,
            timeout=httpx.Timeout(8, connect=3),
        )
    except httpx.ConnectError:
        raise ProviderError("Could not connect to Kapso", retry=True)
    except httpx.HTTPError:
        raise ProviderError(
            "Send outcome unknown; review Kapso logs before retry", uncertain=True
        )
    if response.status_code == 429:
        raise ProviderError("Kapso rate limit", retry=True)
    if response.status_code >= 500:
        raise ProviderError(
            "Provider outcome unknown; review before retry", uncertain=True
        )
    if response.status_code >= 400:
        raise ProviderError(
            f"Kapso rejected request (HTTP {response.status_code}); check templates, number health and billing"
        )
    try:
        return response.json()["messages"][0]["id"]
    except (KeyError, IndexError, ValueError):
        raise ProviderError(
            "Provider accepted request without message id; review before retry",
            uncertain=True,
        )


def normalize_meta(body):
    records = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            number = str(value.get("metadata", {}).get("phone_number_id", ""))
            names = {
                str(
                    c.get("wa_id")
                    or c.get("business_scoped_user_id")
                    or c.get("parent_business_scoped_user_id")
                ): c.get("profile", {}).get("name", "Patient")
                for c in value.get("contacts", [])
            }
            for m in value.get("messages", []):
                contact = str(
                    m.get("from")
                    or m.get("business_scoped_user_id")
                    or m.get("parent_business_scoped_user_id")
                    or ""
                )
                content = m.get("text", {}).get("body")
                if content is None:
                    interactive = m.get("interactive", {})
                    content = (
                        interactive.get("button_reply")
                        or interactive.get("list_reply")
                        or {}
                    ).get("title") or m.get("button", {}).get("text")
                if content is None:
                    content = "[Unsupported media: staff review required]"
                records.append(
                    {
                        "type": "inbound",
                        "number_id": number,
                        "provider_id": str(m.get("id", "")),
                        "contact": contact,
                        "name": str(names.get(contact, "Patient"))[:120],
                        "text": str(content)[:4000],
                        "timestamp": int(m.get("timestamp", 0) or 0),
                    }
                )
            for s in value.get("statuses", []):
                records.append(
                    {
                        "type": "status",
                        "number_id": number,
                        "provider_id": str(s.get("id", "")),
                        "status": str(s.get("status", "unknown")),
                        "timestamp": int(s.get("timestamp", 0) or 0),
                    }
                )
    return records

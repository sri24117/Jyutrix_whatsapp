import hashlib
import hmac
import time
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


REMINDER_ACTIONS = {"confirm", "reschedule", "cancel"}


def make_reminder_action_token(clinic_id, appointment_id, starts, action):
    if action not in REMINDER_ACTIONS or not settings.webhook_secret:
        raise ValueError("Reminder action signing is not configured")
    fields = ["jx1", str(clinic_id), str(appointment_id), str(starts), action, str(starts)]
    body = ".".join(fields)
    signature = hmac.new(
        settings.webhook_secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()[:24]
    return f"{body}.{signature}"


def verify_reminder_action_token(token, now=None):
    if not isinstance(token, str) or not settings.webhook_secret or len(token) > 200:
        return None
    parts = token.split(".")
    if len(parts) != 7 or parts[0] != "jx1" or parts[4] not in REMINDER_ACTIONS:
        return None
    body, signature = ".".join(parts[:-1]), parts[-1]
    expected = hmac.new(
        settings.webhook_secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()[:24]
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        clinic_id, appointment_id, starts, expires = map(
            int, (parts[1], parts[2], parts[3], parts[5])
        )
    except ValueError:
        return None
    if starts != expires or expires <= int(now if now is not None else time.time()):
        return None
    return {
        "clinic_id": clinic_id,
        "appointment_id": appointment_id,
        "starts": starts,
        "action": parts[4],
    }


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
                button_payload = None
                if content is None:
                    interactive = m.get("interactive", {})
                    button_reply = interactive.get("button_reply") or {}
                    button_payload = button_reply.get("payload")
                    reply_id = button_reply.get("id")
                    if not button_payload and isinstance(reply_id, str) and reply_id.startswith("jx1."):
                        button_payload = reply_id
                    content = (
                        button_reply
                        or interactive.get("list_reply")
                        or {}
                    ).get("title") or m.get("button", {}).get("text")
                    if button_payload is None:
                        button_payload = m.get("button", {}).get("payload")
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
                        "button_payload": str(button_payload)[:200] if button_payload else None,
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

"""Enforced plan caps for capabilities currently shipped in this release."""

PLAN_LIMITS = {
    "Starter": {
        "max_doctors": 1,
        "max_services": 10,
        "reminder_offsets_hours": [24, 2],
        "waitlist": False,
        "payments": False,
        "analytics": "basic",
    },
    "Growth": {
        "max_doctors": 5,
        "max_services": 30,
        "reminder_offsets_hours": [24, 3],
        "waitlist": True,
        "payments": False,
        "analytics": "basic",
    },
    # Pro is deliberately not sold/enabled from the UI until branch management,
    # payments and integrations are implemented and tested.
    "Pro": {
        "max_doctors": 25,
        "max_services": 100,
        "reminder_offsets_hours": [24, 3],
        "waitlist": True,
        "payments": False,
        "analytics": "basic",
    },
}


def plan_name(value):
    normalized = (value or "Starter").strip().casefold()
    if normalized in {"pilot", "starter", "basic"}:
        return "Starter"
    if normalized == "growth":
        return "Growth"
    if normalized == "pro":
        return "Pro"
    return "Starter"


def limits_for(clinic):
    return PLAN_LIMITS[plan_name(clinic.plan)]

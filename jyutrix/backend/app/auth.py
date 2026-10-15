import hashlib
import hmac
import secrets
import time
from fastapi import HTTPException, Request
from sqlalchemy import select, delete
from .database import User, LoginSession, RateBucket, transaction
from .config import settings


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=16384, r=8, p=1
    ).hex()
    return f"{salt}${digest}"


def verify_password(password, value):
    try:
        salt, digest = value.split("$")
        return hmac.compare_digest(
            hashlib.scrypt(
                password.encode(), salt=salt.encode(), n=16384, r=8, p=1
            ).hex(),
            digest,
        )
    except (ValueError, TypeError):
        return False


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def limit(key, maximum=10, window=300):
    now = int(time.time())
    with transaction() as db:
        # PostgreSQL advisory lock protects creation/increment of a bucket across workers.
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy import text

            lock = int.from_bytes(
                hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
            )
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        row = db.get(RateBucket, key)
        if not row:
            row = RateBucket(key=key, count=0, reset_at=now + window)
            db.add(row)
        elif row.reset_at <= now:
            row.count = 0
            row.reset_at = now + window
        row.count += 1
        exceeded = row.count > maximum
    if exceeded:
        raise HTTPException(429, "Too many attempts. Try again later.")


def principal(request: Request):
    token = request.cookies.get("jyutrix_session", "")
    with transaction() as db:
        session = db.get(LoginSession, digest_token(token)) if token else None
        if not session or session.expires <= time.time():
            raise HTTPException(401, "Please sign in")
        user = db.get(User, session.user_id)
        if not user or not user.active:
            raise HTTPException(401, "Account inactive")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(
                request.headers.get("X-CSRF-Token", ""), session.csrf
            ):
                raise HTTPException(403, "Invalid request token")
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "clinic_id": user.clinic_id,
            "csrf": session.csrf,
        }


def check_scope(user, clinic_id):
    if user["role"] != "admin" and user["clinic_id"] != clinic_id:
        raise HTTPException(404, "Clinic not found")


def admin_only(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required")


def bootstrap():
    with transaction() as db:
        if not db.scalar(select(User).where(User.username == "admin")):
            password = settings.admin_password or (
                "demo-reception-2026" if settings.mode == "demo" else ""
            )
            if not password:
                raise RuntimeError("ADMIN_PASSWORD is required")
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password(password),
                    role="admin",
                    clinic_id=None,
                )
            )

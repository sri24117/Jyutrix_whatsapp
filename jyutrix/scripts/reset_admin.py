"""Operator-only password reset. Run on the trusted server, never through HTTP."""

from getpass import getpass
from sqlalchemy import select, delete
from backend.app.database import User, LoginSession, transaction
from backend.app.auth import hash_password

password = getpass("New admin password (16+ characters): ")
if len(password) < 16:
    raise SystemExit("Password too short")
with transaction() as db:
    user = db.scalar(select(User).where(User.username == "admin"))
    if not user:
        raise SystemExit("Start the app once to initialize admin")
    user.password_hash = hash_password(password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
print("Password reset and all admin sessions revoked.")

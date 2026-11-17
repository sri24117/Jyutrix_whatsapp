from contextlib import contextmanager
from pathlib import Path
import time
from sqlalchemy import (
    create_engine,
    event,
    select,
    text,
    ForeignKey,
    UniqueConstraint,
    Index,
    String,
    Text,
    Integer,
    Boolean,
    JSON,
    CheckConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from .config import settings


class Base(DeclarativeBase):
    pass


class Clinic(Base):
    __tablename__ = "clinics"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    doctor: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(24))
    address: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    fee: Mapped[int] = mapped_column(Integer, default=500)
    schedule: Mapped[dict] = mapped_column(JSON)
    services: Mapped[list] = mapped_column(JSON, default=list)
    faqs: Mapped[list] = mapped_column(JSON, default=list)
    number_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    template_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(30), default="Pilot")
    monthly_fee: Mapped[int] = mapped_column(Integer, default=4999)
    monthly_limit: Mapped[int] = mapped_column(Integer, default=2000)
    pilot_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    clinic_id: Mapped[int | None] = mapped_column(
        ForeignKey("clinics.id"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires: Mapped[int] = mapped_column(Integer)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(150), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    reset_at: Mapped[int] = mapped_column(Integer)


class SalesLead(Base):
    __tablename__ = "sales_leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    clinic_name: Mapped[str] = mapped_column(String(160))
    city: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    consent: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("clinic_id", "contact", "channel"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"))
    contact: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp")
    mode: Mapped[str] = mapped_column(String(20), default="bot")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    last_inbound: Mapped[int] = mapped_column(Integer, default=0)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    attention: Mapped[str] = mapped_column(Text, default="")
    updated: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str | None] = mapped_column(
        String(200), unique=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="received")
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("ends > starts", name="valid_appointment_range"),
        CheckConstraint("paid_amount >= 0", name="positive_payment"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"), index=True)
    thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    contact: Mapped[str] = mapped_column(String(100))
    starts: Mapped[int] = mapped_column(Integer, index=True)
    ends: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    source: Mapped[str] = mapped_column(String(20))
    request_key: Mapped[str] = mapped_column(String(200), unique=True)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_amount: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"), index=True)
    thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=True
    )
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(240), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due: Mapped[int] = mapped_column(Integer, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Delivery(Base):
    __tablename__ = "deliveries"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"))
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Audit(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    created: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    seen: Mapped[int] = mapped_column(Integer)


url = settings.database_url
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
if url.startswith("sqlite:///"):
    Path(url[10:]).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    url,
    pool_pre_ping=True,
    connect_args=(
        {"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {}
    ),
)
if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def sqlite_setup(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")


@contextmanager
def transaction(clinic_id=None):
    with Session(engine, expire_on_commit=False) as db:
        try:
            if engine.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            if clinic_id and engine.dialect.name != "sqlite":
                db.execute(
                    select(Clinic.id).where(Clinic.id == clinic_id).with_for_update()
                ).first()
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def audit(db, actor, action, target, clinic_id=None):
    db.add(Audit(actor=actor, action=action, target=str(target), clinic_id=clinic_id))


def init_db():
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(text("""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_no_overlap') THEN
                    ALTER TABLE appointments ADD CONSTRAINT appointments_no_overlap
                    EXCLUDE USING gist (clinic_id WITH =, (source = 'demo') WITH =, int8range(starts::bigint, ends::bigint, '[)') WITH &&)
                    WHERE (status = 'confirmed');
                END IF;
            END $$"""))
    else:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS appointments_live_start ON appointments(clinic_id, starts, (source = 'demo')) WHERE status='confirmed'"
                )
            )

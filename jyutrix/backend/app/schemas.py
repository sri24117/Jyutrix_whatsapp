from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Break(Strict):
    start: str
    end: str


class Schedule(Strict):
    open: str = "09:00"
    close: str = "19:00"
    duration: int = Field(default=30, ge=5, le=120)
    closed_days: list[int] = Field(default_factory=lambda: [6])
    leave_dates: list[str] = Field(default_factory=list)
    breaks: list[Break] = Field(
        default_factory=lambda: [Break(start="13:00", end="14:00")]
    )

    @model_validator(mode="after")
    def valid(self):
        times = [self.open, self.close] + [
            v for b in self.breaks for v in [b.start, b.end]
        ]
        for t in times:
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError("Use HH:MM times")
        if self.open >= self.close:
            raise ValueError("Closing time must be later than opening time")
        if (
            any(d < 0 or d > 6 for d in self.closed_days)
            or len(set(self.closed_days)) == 7
        ):
            raise ValueError("At least one clinic day must be open")
        for d in self.leave_dates:
            date.fromisoformat(d)
        for b in self.breaks:
            if not self.open <= b.start < b.end <= self.close:
                raise ValueError("Break must fall inside opening hours")
        return self


class FAQ(Strict):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=2, max_length=1200)


class ClinicInput(Strict):
    name: str = Field(min_length=2, max_length=120)
    doctor: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=r"^\+?[1-9]\d{7,14}$")
    address: str = Field(min_length=3, max_length=600)
    timezone: str = "Asia/Kolkata"
    fee: int = Field(default=500, ge=0, le=100000)
    schedule: Schedule = Field(default_factory=Schedule)
    services: list[str] = Field(
        default_factory=lambda: ["Dental consultation"], max_length=30
    )
    faqs: list[FAQ] = Field(default_factory=list, max_length=100)
    number_id: str | None = Field(default=None, pattern=r"^\d{5,30}$")
    enabled: bool = False
    template_approved: bool = False
    plan: str = Field(default="Pilot", max_length=30)
    monthly_fee: int = Field(default=4999, ge=0, le=100000)
    monthly_limit: int = Field(default=2000, ge=1, le=100000)
    pilot_end: int | None = None

    @field_validator("timezone")
    @classmethod
    def zone(cls, v):
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone")
        return v

    @field_validator("services")
    @classmethod
    def service_size(cls, v):
        if any(not s.strip() or len(s) > 120 for s in v):
            raise ValueError("Services must be 1–120 characters")
        return v


class Login(Strict):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class UserInput(Strict):
    username: str = Field(pattern=r"^[a-zA-Z0-9@._-]{3,100}$")
    password: str = Field(min_length=16, max_length=128)
    clinic_id: int
    role: Literal["owner", "receptionist"] = "receptionist"


class DemoInput(Strict):
    clinic_id: int
    contact: str = Field(default="919999000001", min_length=8, max_length=30)
    name: str = Field(default="Demo patient", min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)


class ReplyInput(Strict):
    text: str = Field(min_length=1, max_length=4000)


class AppointmentInput(Strict):
    clinic_id: int
    name: str = Field(min_length=1, max_length=120)
    contact: str = Field(pattern=r"^\+?[1-9]\d{7,14}$")
    scheduled_at: str
    request_key: str = Field(min_length=8, max_length=100)
    consent: bool = False


class AppointmentUpdate(Strict):
    status: Literal["confirmed", "completed", "no_show", "cancelled"] | None = None
    scheduled_at: str | None = None
    paid_amount: int | None = Field(default=None, ge=0, le=10000000)


class PasswordInput(Strict):
    current: str = Field(max_length=128)
    new: str = Field(min_length=16, max_length=128)


class RetryInput(Strict):
    reviewed_provider_logs: bool = False


class VoiceRequest(Strict):
    clinic_id: int
    action: Literal["info", "availability", "request"]
    contact: str | None = Field(default=None, pattern=r"^\+?[1-9]\d{7,14}$")
    name: str = Field(default="Caller", max_length=120)
    date: str | None = None
    scheduled_at: str | None = None
    call_id: str = Field(min_length=3, max_length=100)

from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("APP_MODE", "demo")
    database_url: str = os.getenv("DATABASE_URL", f'sqlite:///{ROOT / "data/app.db"}')
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    kapso_key: str = os.getenv("KAPSO_API_KEY", "")
    webhook_secret: str = os.getenv("KAPSO_WEBHOOK_SECRET", "")
    kapso_version: str = os.getenv("KAPSO_API_VERSION", "v24.0")
    openai_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    reminder_template: str = os.getenv(
        "REMINDER_TEMPLATE", "jyutrix_appointment_reminder"
    )
    template_language: str = os.getenv("TEMPLATE_LANGUAGE", "en_US")
    voice_key: str = os.getenv("VOICE_TOOL_KEY", "")
    retention_days: int = int(os.getenv("RETENTION_DAYS", "90"))

    def validate(self):
        if self.mode not in {"demo", "production", "test"}:
            raise RuntimeError("APP_MODE must be demo, production or test")
        if self.mode == "production":
            if len(self.admin_password) < 16 or self.admin_password in {
                "change-this-password",
                "jyutrix-admin",
            }:
                raise RuntimeError(
                    "Production requires a unique ADMIN_PASSWORD of at least 16 characters"
                )
            if not self.public_url.startswith("https://"):
                raise RuntimeError("Production requires an HTTPS PUBLIC_URL")
            if len(self.webhook_secret) < 32 or not self.kapso_key:
                raise RuntimeError(
                    "Production requires KAPSO_API_KEY and KAPSO_WEBHOOK_SECRET (32+ characters)"
                )
            if not self.database_url.startswith(
                ("postgresql://", "postgresql+psycopg://")
            ):
                raise RuntimeError(
                    "Production requires PostgreSQL; demo SQLite is not a production database"
                )


settings = Settings()

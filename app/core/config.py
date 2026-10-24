from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from ipaddress import ip_address
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_name: str = 'Invoice Keeper'
    database_url: str = 'sqlite:///./data/invoice-keeper.db'
    admin_login: str = 'admin'
    admin_password: str = 'change-me-before-use'
    app_timezone: str = 'Europe/Moscow'
    frontend_origin: str = 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080'
    cookie_secure: bool = False
    public_frontend_url: str = 'http://localhost:5173'
    public_invoice_view_enabled: bool = False
    telegram_bot_token: str = ''
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_username: str = ''
    smtp_password: str = ''
    smtp_from: str = 'invoice-keeper@localhost'
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    worker_interval_seconds: int = 30
    allow_private_webhooks: bool = False
    data_dir: Path = Path('data')

    @field_validator('public_frontend_url')
    @classmethod
    def validate_public_frontend_url(cls, value: str):
        parsed = urlsplit(value)
        try:
            loopback = parsed.hostname == 'localhost' or ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = False
        if (any(char.isspace() for char in value) or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or parsed.scheme not in ('http', 'https') or (parsed.scheme == 'http' and not loopback)):
            raise ValueError('PUBLIC_FRONTEND_URL: нужен HTTPS origin без пути, параметров и учётных данных; HTTP разрешён только для localhost')
        parsed.port  # Validate an explicitly supplied port.
        return value.rstrip('/')

    @property
    def allowed_origins(self):
        return [origin.strip().rstrip('/') for origin in self.frontend_origin.split(',') if origin.strip()]


settings = Settings()


def today():
    return datetime.now(ZoneInfo(settings.app_timezone)).date()

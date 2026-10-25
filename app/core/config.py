from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from ipaddress import ip_address
from urllib.parse import urlsplit

from pydantic import model_validator, field_validator
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
    password_change_enabled: bool = True
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
    public_app_url: str = 'http://localhost:8000'
    integrations_encryption_key: str = ''
    oidc_allowed_origins: str = ''
    b2b_enabled: bool = False
    b2b_api_key_pepper: str = ''
    b2b_document_max_bytes: int = 10 * 1024 * 1024
    b2b_document_max_files: int = 5
    b2b_document_max_total_bytes: int = 30 * 1024 * 1024
    b2b_storage_path: Path | None = None
    b2b_clamav_enabled: bool = False
    b2b_clamav_host: str = ''
    b2b_clamav_port: int = 3310
    b2b_idempotency_ttl_days: int = 7
    b2b_rate_limit_per_minute: int = 120
    rate_limit_max_keys: int = 10_000

    @model_validator(mode='after')
    def validate_b2b_settings(self):
        if self.b2b_enabled and not self.b2b_api_key_pepper:
            raise ValueError('B2B_API_KEY_PEPPER is required when B2B_ENABLED=true')
        if self.b2b_enabled and self.b2b_clamav_enabled and not self.b2b_clamav_host:
            raise ValueError('B2B_CLAMAV_HOST is required when ClamAV is enabled')
        if self.b2b_clamav_enabled and not 1 <= self.b2b_clamav_port <= 65535:
            raise ValueError('B2B_CLAMAV_PORT must be between 1 and 65535')
        if self.b2b_enabled and (self.b2b_document_max_bytes <= 0 or self.b2b_document_max_files <= 0
                                 or self.b2b_document_max_total_bytes <= 0 or self.b2b_idempotency_ttl_days <= 0):
            raise ValueError('B2B document and idempotency limits must be positive when B2B is enabled')
        if self.b2b_rate_limit_per_minute <= 0 or self.rate_limit_max_keys <= 0:
            raise ValueError('Rate-limit settings must be positive')
        return self

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

    @property
    def incoming_invoice_storage_dir(self):
        return self.b2b_storage_path or self.data_dir / 'incoming-invoices'


settings = Settings()


def today():
    return datetime.now(ZoneInfo(settings.app_timezone)).date()

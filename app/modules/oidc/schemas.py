from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    discovery_url: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: SecretStr | None = Field(default=None, max_length=4096)
    enabled: bool
    auto_provision: bool
    session_minutes: int = Field(ge=5, le=1440)
    expected_revision: int = Field(ge=0)


class ProviderOut(BaseModel):
    configured: bool
    name: str
    discovery_url: str
    client_id: str
    has_client_secret: bool
    enabled: bool
    auto_provision: bool
    session_minutes: int
    revision: int
    redirect_uri: str
    encryption_ready: bool
    allowed_origins: list[str]


class HandoffIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(pattern=r'^[A-Za-z0-9_-]{64}$', max_length=64)
    code_verifier: str = Field(pattern=r'^[A-Za-z0-9._~-]{43,128}$', max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=128)

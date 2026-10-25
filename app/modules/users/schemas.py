from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=120)

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class UserIn(BaseModel):
    login: str = Field(min_length=3, max_length=80, pattern=r'^[a-zA-Z0-9_.@-]+$')
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    active: bool | None = None

from pydantic import BaseModel, Field


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=400)
    channel_ids: list[str] = Field(default_factory=list, max_length=100)

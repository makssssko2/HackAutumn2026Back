from pydantic import BaseModel, Field


class LinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=2000)

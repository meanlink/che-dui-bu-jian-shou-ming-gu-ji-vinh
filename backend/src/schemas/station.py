from typing import Literal
from pydantic import BaseModel, Field


class StationInput(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=2, max_length=120)


class PublishInput(BaseModel):
    job_id: int
    station_ids: list[int] = Field(min_length=1, max_length=200)


class PublishOneInput(BaseModel):
    job_id: int


class RevokeInput(BaseModel):
    version: int | None = Field(default=None, ge=1)


class SimulateInput(BaseModel):
    online: bool | None = None
    drop_receipt: bool | None = None
    hold: bool | None = None


class ReleaseInput(BaseModel):
    order: Literal['fifo', 'lifo'] = 'fifo'
    directive_ids: list[int] | None = None

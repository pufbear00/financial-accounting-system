from pydantic import BaseModel
from datetime import datetime


class CategoryCreate(BaseModel):
    name: str
    description: str = ""
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None


class CategoryResponse(BaseModel):
    id: int
    name: str
    description: str
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}

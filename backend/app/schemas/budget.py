from pydantic import BaseModel
from datetime import datetime


class BudgetCreate(BaseModel):
    year_month: str  # "2026-04"
    category_id: int
    amount: float
    note: str = ""


class BudgetUpdate(BaseModel):
    amount: float | None = None
    note: str | None = None


class BudgetResponse(BaseModel):
    id: int
    year_month: str
    category_id: int
    amount: float
    note: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BudgetWithSpent(BaseModel):
    id: int
    year_month: str
    category_id: int
    category_name: str
    amount: float
    spent: float
    usage_pct: float
    note: str

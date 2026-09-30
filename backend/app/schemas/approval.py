from pydantic import BaseModel
from datetime import date, datetime


class ApprovalResponse(BaseModel):
    id: int
    instance_code: str
    task_id: str | None
    definition_code: str
    definition_name: str | None
    applicant_id: str | None
    applicant_name: str | None
    status: int | None
    amount: float | None
    currency: str | None
    exchange_rate: float | None
    amount_cny: float | None
    category_name: str | None
    category_id: int | None
    expense_date: date | None
    submit_date: date | None
    description: str | None
    topic: int | None
    synced_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalListResponse(BaseModel):
    items: list[ApprovalResponse]
    total: int
    page: int
    page_size: int
    sum_amount: float


class ApprovalCategoryUpdate(BaseModel):
    category_id: int

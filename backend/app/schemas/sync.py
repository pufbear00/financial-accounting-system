from pydantic import BaseModel
from datetime import datetime


class SyncTriggerRequest(BaseModel):
    topics: list[int] = [1, 2, 3]  # 默认拉取待审批+已审批+我发起


class SyncTriggerResponse(BaseModel):
    sync_log_id: int
    message: str


class SyncLogResponse(BaseModel):
    id: int
    sync_type: str
    total_fetched: int
    new_records: int
    updated_records: int
    status: str
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}

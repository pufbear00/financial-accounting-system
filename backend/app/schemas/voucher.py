from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class VoucherBatchCreate(BaseModel):
    year_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    sync_feishu: bool = False


class VoucherBatchResponse(BaseModel):
    id: int
    year_month: str
    status: str
    summary: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VoucherItemResponse(BaseModel):
    id: int
    batch_id: int
    approval_id: int
    instance_code: str
    definition_name: str | None = None
    applicant_name: str | None = None
    amount: float | None = None
    currency: str | None = None
    amount_cny: float | None = None
    category_id: int | None = None
    expense_date: date | None = None
    submit_date: date | None = None
    description: str | None = None
    include_approval_in_monthly_pdf: bool = False

    model_config = {"from_attributes": True}


class VoucherLinkCreate(BaseModel):
    document_id: int
    link_type: str


class VoucherRequirementUpdate(BaseModel):
    requires_voucher: bool
    reason: str | None = None


class VoucherMonthlyApprovalUpdate(BaseModel):
    include_approval: bool


class VoucherInvoicePrintedUpdate(BaseModel):
    printed: bool


class VoucherInvoiceBulkPrintedUpdate(BaseModel):
    printed: bool


class VoucherReceiptPoolUpdate(BaseModel):
    category: Literal["pending", "income", "other"]


class VoucherReceiptPoolExport(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=200)


class VoucherReceiptInvoiceReferenceCreate(BaseModel):
    document_ids: list[int] = Field(min_length=1, max_length=50)


class VoucherDocumentResponse(BaseModel):
    id: int
    uuid: str
    document_type: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    amount: float | None = None
    payee: str | None = None
    transaction_date: date | None = None
    created_at: datetime
    deduplicated: bool = False

    model_config = {"from_attributes": True}

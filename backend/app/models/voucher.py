from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Boolean,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)

from app.database import Base


class VoucherBatch(Base):
    """A frozen monthly view of approvals used for paper voucher binding."""

    __tablename__ = "voucher_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year_month = Column(String(7), nullable=False, unique=True, index=True)
    status = Column(String(20), nullable=False, default="draft")
    item_count = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VoucherItem(Base):
    """Approval snapshot. Source approval rows are never changed by this module."""

    __tablename__ = "voucher_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(
        Integer,
        ForeignKey("voucher_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_id = Column(Integer, ForeignKey("approvals.id"), nullable=False)
    source_line_key = Column(String(80), nullable=False, default="main")
    source_line_label = Column(String(120))
    sequence = Column(Integer, nullable=False, default=0)
    instance_code = Column(String, nullable=False)
    definition_name = Column(String)
    applicant_name = Column(String)
    approval_status = Column(Integer, nullable=False)
    payee_name_snapshot = Column(String)
    amount = Column(Float)
    currency = Column(String, default="CNY")
    amount_cny = Column(Float)
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=True)
    expense_date = Column(Date)
    submit_date = Column(Date)
    description = Column(Text)
    raw_summaries_snapshot = Column(Text)
    requires_voucher = Column(Boolean, nullable=False, default=True)
    voucher_exclusion_reason = Column(String)
    requirement_overridden = Column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    include_approval_in_monthly_pdf = Column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "batch_id", "approval_id", name="uq_voucher_item_batch_approval"
        ),
        UniqueConstraint(
            "batch_id", "sequence", name="uq_voucher_item_batch_sequence"
        ),
        Index("ix_voucher_item_source_line", "batch_id", "approval_id", "source_line_key"),
    )


class VoucherDocument(Base):
    __tablename__ = "voucher_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(
        Integer,
        ForeignKey("voucher_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uuid = Column(String(36), nullable=False, unique=True, index=True)
    document_type = Column(String(30), nullable=False)
    original_filename = Column(String, nullable=False)
    storage_path = Column(String, nullable=False, unique=True)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    amount = Column(Float)
    payee = Column(String)
    bank_reference = Column(String)
    transaction_date = Column(Date)
    receipt_direction = Column(String(20))
    receipt_category = Column(String(20))
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "document_type",
            "sha256",
            name="uq_voucher_document_batch_type_sha256",
        ),
    )


class VoucherDocumentLink(Base):
    __tablename__ = "voucher_document_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(
        Integer,
        ForeignKey("voucher_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        Integer,
        ForeignKey("voucher_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    link_type = Column(String(30), nullable=False)
    invoice_printed = Column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    match_score = Column(Float)
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "item_id", "document_id", name="uq_voucher_item_document"
        ),
        Index(
            "uq_voucher_bank_receipt_single_item",
            "document_id",
            unique=True,
            sqlite_where=text("link_type = 'bank_receipt'"),
        ),
    )


class VoucherReceiptInvoiceLink(Base):
    """Keep optional invoices together with a receipt while it is in the pool."""

    __tablename__ = "voucher_receipt_invoice_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    receipt_document_id = Column(
        Integer,
        ForeignKey("voucher_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_document_id = Column(
        Integer,
        ForeignKey("voucher_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    position = Column(Integer, nullable=False, default=0)
    owns_invoice = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "receipt_document_id",
            "invoice_document_id",
            name="uq_voucher_receipt_invoice_pair",
        ),
        Index(
            "ix_voucher_receipt_invoice_order",
            "receipt_document_id",
            "position",
            "id",
        ),
    )


class VoucherFeishuCommentSync(Base):
    """Persistent delivery record for a receipt-owned invoice comment."""

    __tablename__ = "voucher_feishu_comment_syncs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(120), nullable=False, unique=True, index=True)
    item_id = Column(
        Integer,
        ForeignKey("voucher_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    receipt_document_id = Column(
        Integer,
        ForeignKey("voucher_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_document_id = Column(
        Integer,
        ForeignKey("voucher_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    instance_code = Column(String, nullable=False, index=True)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_open_id = Column(String, nullable=True)
    status = Column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    claim_token = Column(String(36), nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    feishu_file_url = Column(Text, nullable=True)
    feishu_file_uploaded_at = Column(DateTime, nullable=True)
    feishu_comment_id = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    sent_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "receipt_document_id",
            "invoice_document_id",
            name="uq_voucher_feishu_comment_delivery",
        ),
    )


class VoucherExport(Base):
    __tablename__ = "voucher_exports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    export_type = Column(String(30), nullable=False)
    batch_id = Column(
        Integer,
        ForeignKey("voucher_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    item_id = Column(
        Integer,
        ForeignKey("voucher_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    uuid = Column(String(36), nullable=False, unique=True)
    filename = Column(String, nullable=False)
    storage_path = Column(String, nullable=False, unique=True)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

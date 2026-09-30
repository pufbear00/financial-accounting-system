from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.approval import Approval
from app.models.sync_log import SyncLog
from app.models.user import User
from app.models.voucher import (
    VoucherBatch,
    VoucherDocument,
    VoucherDocumentLink,
    VoucherFeishuCommentSync,
    VoucherReceiptInvoiceLink,
    VoucherExport,
    VoucherItem,
)
from app.schemas.voucher import (
    VoucherBatchCreate,
    VoucherInvoiceBulkPrintedUpdate,
    VoucherInvoicePrintedUpdate,
    VoucherLinkCreate,
    VoucherMonthlyApprovalUpdate,
    VoucherReceiptPoolExport,
    VoucherReceiptInvoiceReferenceCreate,
    VoucherReceiptPoolUpdate,
    VoucherRequirementUpdate,
)
from app.services.voucher_service import (
    build_item_pdf,
    build_receipt_packet_pdf,
    build_receipt_pool_pdf,
    completeness,
    extract_approval_form_rows,
    extract_payee_name,
    extract_receipt_metadata,
    merge_pdfs,
    merge_monthly_print_pdfs,
    parse_source_attachment_metadata,
    safe_resolve,
    score_receipt,
    sha256_bytes,
    split_pdf_pages,
    validate_upload,
)
from app.services.feishu_sync import _resolve_user_name, run_sync
from app.services.voucher_feishu_import import (
    FeishuImportError,
    MAX_ATTACHMENTS_PER_IMPORT,
    MAX_TOTAL_IMPORT_BYTES,
    decode_feishu_base64_attachment,
    download_feishu_attachment,
    fetch_feishu_instance_detail,
    fetch_feishu_instance_detail_with_token,
    fetch_feishu_instance_comments_with_token,
    fetch_feishu_tenant_token,
    filename_with_content_extension,
    parse_feishu_instance_attachments,
    safe_attachment_filename,
)
from app.services.voucher_feishu_comment import (
    FeishuCommentSyncError,
    create_feishu_approval_comment,
    find_feishu_comment_by_event,
    upload_feishu_approval_attachment,
)


router = APIRouter(prefix="/vouchers", tags=["vouchers"])
VOUCHER_START_MONTH = "2026-07"
MAX_BATCH_FEISHU_IMPORT_ITEMS = 50
MAX_BATCH_FEISHU_IMPORT_BYTES = 200 * 1024 * 1024
BATCH_FEISHU_IMPORT_TTL_SECONDS = 30.0
FEISHU_COMMENT_SEND_LEASE_SECONDS = 15 * 60
FEISHU_ATTACHMENT_URL_REUSE_SECONDS = 60 * 60
MAX_RECEIPT_BATCH_UPLOAD_BYTES = 200 * 1024 * 1024
AUTO_RECEIPT_AMOUNT_TOLERANCE = 0.01
AUTO_RECEIPT_SCORE_THRESHOLD = 95.0
AUTO_RECEIPT_SCORE_LEAD = 5.0
NO_VOUCHER_KEYWORDS = (
    "入账",
    "无需凭证",
    "无需提供凭证",
    "不用凭证",
    "不需要凭证",
    "无需回单",
    "不需装订",
)


def _admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可使用凭证装订模块")


def _validate_voucher_month(year_month: str) -> None:
    if year_month < VOUCHER_START_MONTH:
        raise HTTPException(
            status_code=422,
            detail="凭证装订仅支持 2026-07 及以后月份",
        )


async def _get_batch(batch_id: int, db: AsyncSession) -> VoucherBatch:
    batch = await db.get(VoucherBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="凭证批次不存在")
    return batch


async def _get_item(item_id: int, db: AsyncSession) -> VoucherItem:
    item = await db.get(VoucherItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="凭证条目不存在")
    return item


async def _auto_confirm_receipt_by_amount(
    document: VoucherDocument,
    user: User,
    db: AsyncSession,
) -> VoucherDocumentLink | None:
    if (
        document.document_type != "bank_receipt"
        or document.amount is None
        or document.receipt_direction == "income"
        or document.receipt_category in {"income", "other"}
    ):
        return None
    if await db.scalar(
        select(VoucherDocumentLink.id)
        .where(
            VoucherDocumentLink.document_id == document.id,
            VoucherDocumentLink.link_type == "bank_receipt",
        )
        .limit(1)
    ):
        return None
    if await db.scalar(
        select(VoucherReceiptInvoiceLink.id)
        .where(
            VoucherReceiptInvoiceLink.receipt_document_id == document.id,
            VoucherReceiptInvoiceLink.owns_invoice.is_(False),
        )
        .limit(1)
    ):
        # A historical invoice reference belongs to its original approval.
        # Keep this receipt in the pool until an administrator deliberately
        # chooses the matching approval, so batch auto-match cannot abort or
        # attach the receipt to a different item by amount alone.
        return None
    candidates = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(
                    VoucherItem.batch_id == document.batch_id,
                    VoucherItem.requires_voucher.is_(True),
                )
                .order_by(VoucherItem.sequence, VoucherItem.id)
            )
        ).all()
    )
    linked_receipt_rows = (
        await db.execute(
            select(VoucherDocumentLink.item_id, VoucherDocument.amount)
            .join(
                VoucherDocument,
                VoucherDocument.id == VoucherDocumentLink.document_id,
            )
            .where(
                VoucherDocument.batch_id == document.batch_id,
                VoucherDocumentLink.link_type == "bank_receipt",
            )
        )
    ).all()
    linked_amounts: dict[int, float] = defaultdict(float)
    unknown_linked_amounts: set[int] = set()
    for item_id, linked_amount in linked_receipt_rows:
        if linked_amount is None:
            unknown_linked_amounts.add(int(item_id))
        else:
            linked_amounts[int(item_id)] += abs(float(linked_amount))
    amount_matches: list[tuple[VoucherItem, float]] = []
    high_score_matches: list[tuple[VoucherItem, float]] = []
    for item in candidates:
        if item.id in unknown_linked_amounts:
            # Remaining balance cannot be proved when an existing receipt has
            # no extractable/manual amount. Keep this case for manual review.
            continue
        item_amount = item.amount if item.amount is not None else item.amount_cny
        if item_amount is None:
            continue
        remaining_amount = abs(float(item_amount)) - linked_amounts[item.id]
        if remaining_amount <= AUTO_RECEIPT_AMOUNT_TOLERANCE:
            continue
        amount_difference = abs(remaining_amount - abs(float(document.amount)))
        scoring = score_receipt(item, document)
        original_amount_score = float(scoring["breakdown"]["amount"]["score"])
        denominator = max(remaining_amount, 1.0)
        remaining_amount_score = 70.0 * max(
            0.0, 1.0 - amount_difference / denominator
        )
        if amount_difference <= AUTO_RECEIPT_AMOUNT_TOLERANCE:
            remaining_amount_score = 70.0
        score = float(scoring["score"]) - original_amount_score + remaining_amount_score
        if amount_difference <= AUTO_RECEIPT_AMOUNT_TOLERANCE:
            amount_matches.append((item, score))
        if score >= AUTO_RECEIPT_SCORE_THRESHOLD:
            high_score_matches.append((item, score))
    if len(amount_matches) == 1:
        return await _confirm_document(amount_matches[0][0], document, user, db)
    scored_matches = high_score_matches or amount_matches
    if not scored_matches:
        return None
    scored_matches.sort(key=lambda row: (-row[1], row[0].sequence, row[0].id))
    best_item, best_score = scored_matches[0]
    second_score = scored_matches[1][1] if len(scored_matches) > 1 else None
    if best_score < AUTO_RECEIPT_SCORE_THRESHOLD:
        return None
    if second_score is not None and best_score - second_score < AUTO_RECEIPT_SCORE_LEAD:
        return None
    return await _confirm_document(best_item, document, user, db)


async def _auto_confirm_batch_receipts_by_amount(
    batch_id: int,
    user: User,
    db: AsyncSession,
) -> dict[str, int]:
    all_documents = list(
        (
            await db.scalars(
                select(VoucherDocument)
                .where(
                    VoucherDocument.batch_id == batch_id,
                    VoucherDocument.document_type == "bank_receipt",
                )
                .order_by(VoucherDocument.id)
            )
        ).all()
    )
    documents = [
        document
        for document in all_documents
        if document.receipt_direction != "income"
        and document.receipt_category in (None, "pending")
    ]
    scanned = matched = pending = 0
    for document in documents:
        scanned += 1
        link = await _auto_confirm_receipt_by_amount(document, user, db)
        if link:
            matched += 1
        else:
            already_linked = await db.scalar(
                select(VoucherDocumentLink.id)
                .where(
                    VoucherDocumentLink.document_id == document.id,
                    VoucherDocumentLink.link_type == "bank_receipt",
                )
                .limit(1)
            )
            if not already_linked:
                pending += 1
    await _refresh_batch_status(batch_id, db)
    return {"scanned": scanned, "matched": matched, "pending_confirmation": pending}


def _infer_requires_voucher(approval: Approval) -> tuple[bool, str | None]:
    text = " ".join(
        str(value or "")
        for value in (
            approval.definition_name,
            approval.description,
            approval.raw_summaries,
        )
    )
    for keyword in NO_VOUCHER_KEYWORDS:
        if keyword in text:
            return False, f"自动识别：{keyword}"
    return True, None


def _approval_month(approval: Approval) -> str | None:
    assigned_date = approval.submit_date or approval.expense_date
    return assigned_date.isoformat()[:7] if assigned_date else None


def _refresh_item_snapshot(
    item: VoucherItem,
    approval: Approval,
    year_month: str,
) -> None:
    """Refresh source-owned fields while preserving explicit voucher choices."""
    fields = {
        "instance_code": approval.instance_code,
        "definition_name": approval.definition_name,
        "applicant_name": approval.applicant_name,
        "approval_status": approval.status,
        "payee_name_snapshot": extract_payee_name(approval.raw_summaries),
        "amount": approval.amount,
        "currency": approval.currency,
        "amount_cny": approval.amount_cny,
        "category_id": approval.category_id,
        "expense_date": approval.expense_date,
        "submit_date": approval.submit_date,
        "description": approval.description,
        "raw_summaries_snapshot": approval.raw_summaries,
    }
    changed = False
    for field, value in fields.items():
        if getattr(item, field) != value:
            setattr(item, field, value)
            changed = True

    reason = (item.voucher_exclusion_reason or "").strip()
    manually_overridden = bool(
        item.requirement_overridden
        or (
            reason
            and not reason.startswith("自动识别：")
            and not reason.startswith("自动同步：")
        )
    )
    if approval.status != 2:
        item.requires_voucher = False
        item.voucher_exclusion_reason = "自动同步：审批状态已变更"
    elif _approval_month(approval) != year_month:
        item.requires_voucher = False
        item.voucher_exclusion_reason = "自动同步：审批归属月份已变更"
    elif not manually_overridden:
        requires_voucher, exclusion_reason = _infer_requires_voucher(approval)
        item.requires_voucher = requires_voucher
        item.voucher_exclusion_reason = exclusion_reason
    if changed:
        item.updated_at = datetime.now()


async def _sync_feishu_for_voucher_month(year_month: str, db: AsyncSession) -> None:
    raise HTTPException(status_code=403, detail="本地空数据版不连接飞书，请使用本地刷新")


@dataclass
class BatchContext:
    documents: dict[int, VoucherDocument]
    links_by_item: dict[int, list[VoucherDocumentLink]]
    linked_item_by_document: dict[int, VoucherItem]
    linked_document_ids: set[int]
    unlinked_receipts: list[VoucherDocument]
    pending_receipts: list[VoucherDocument]


async def _load_context(
    batch_id: int, items: list[VoucherItem], db: AsyncSession
) -> BatchContext:
    documents = list(
        (
            await db.scalars(
                select(VoucherDocument)
                .where(VoucherDocument.batch_id == batch_id)
                .order_by(VoucherDocument.id)
            )
        ).all()
    )
    item_map = {item.id: item for item in items}
    links: list[VoucherDocumentLink] = []
    document_ids = [document.id for document in documents]
    if item_map or document_ids:
        conditions = []
        if item_map:
            conditions.append(VoucherDocumentLink.item_id.in_(item_map))
        if document_ids:
            conditions.append(VoucherDocumentLink.document_id.in_(document_ids))
        links = list(
            (
                await db.scalars(
                    select(VoucherDocumentLink)
                    .where(or_(*conditions))
                    .order_by(VoucherDocumentLink.id)
                )
            ).all()
        )
    return _context_from_loaded(items, documents, links)


async def _repair_batch_receipt_metadata(
    batch_id: int,
    db: AsyncSession,
) -> dict[str, int]:
    """Classify legacy receipts once and undo impossible income matches."""
    documents = list(
        (
            await db.scalars(
                select(VoucherDocument).where(
                    VoucherDocument.batch_id == batch_id,
                    VoucherDocument.document_type == "bank_receipt",
                    (
                        VoucherDocument.receipt_direction.is_(None)
                        | VoucherDocument.receipt_category.is_(None)
                    ),
                )
            )
        ).all()
    )
    classified = unlinked_income = 0
    root = Path(settings.VOUCHER_FILE_ROOT)
    for document in documents:
        try:
            path = safe_resolve(root, document.storage_path)
            data = await run_in_threadpool(path.read_bytes)
            metadata = await run_in_threadpool(
                extract_receipt_metadata,
                document.original_filename,
                data,
                document.mime_type,
            )
        except (OSError, ValueError):
            metadata = {
                "receipt_direction": "unknown",
                "receipt_category": "pending",
            }
        document.receipt_direction = metadata.get(
            "receipt_direction", "unknown"
        )
        document.receipt_category = metadata.get(
            "receipt_category", "pending"
        )
        if metadata.get("payee"):
            document.payee = metadata["payee"]
        if document.amount is None and metadata.get("amount") is not None:
            document.amount = metadata["amount"]
        if document.transaction_date is None and metadata.get("transaction_date"):
            document.transaction_date = metadata["transaction_date"]
        if not document.bank_reference and metadata.get("bank_reference"):
            document.bank_reference = metadata["bank_reference"]
        classified += 1

        if document.receipt_category == "income":
            links = list(
                (
                    await db.scalars(
                        select(VoucherDocumentLink).where(
                            VoucherDocumentLink.document_id == document.id,
                            VoucherDocumentLink.link_type == "bank_receipt",
                        )
                    )
                ).all()
            )
            for link in links:
                await _unlink_receipt_invoices_from_item(
                    link.item_id,
                    [document.id],
                    db,
                )
                await db.delete(link)
                unlinked_income += 1
    if classified:
        await db.flush()
    return {"classified": classified, "unlinked_income": unlinked_income}


def _context_from_loaded(
    items: list[VoucherItem],
    documents: list[VoucherDocument],
    links: list[VoucherDocumentLink],
) -> BatchContext:
    doc_map = {document.id: document for document in documents}
    item_map = {item.id: item for item in items}
    links_by_item: dict[int, list[VoucherDocumentLink]] = defaultdict(list)
    linked_item_by_document: dict[int, VoucherItem] = {}
    linked_document_ids: set[int] = set()
    for link in links:
        if link.item_id in item_map:
            links_by_item[link.item_id].append(link)
        if link.document_id in doc_map:
            linked_document_ids.add(link.document_id)
        if link.document_id in doc_map and link.item_id in item_map:
            linked_item_by_document[link.document_id] = item_map[link.item_id]
    unlinked_receipts = [
        document
        for document in documents
        if document.document_type == "bank_receipt"
        and document.id not in linked_document_ids
    ]
    pending_receipts = [
        document
        for document in unlinked_receipts
        if document.receipt_direction != "income"
        and document.receipt_category in (None, "pending")
    ]
    return BatchContext(
        documents=doc_map,
        links_by_item=links_by_item,
        linked_item_by_document=linked_item_by_document,
        linked_document_ids=linked_document_ids,
        unlinked_receipts=unlinked_receipts,
        pending_receipts=pending_receipts,
    )


def _attachment_payload(
    document: VoucherDocument | None,
    link: VoucherDocumentLink | None = None,
) -> dict[str, Any] | None:
    if not document:
        return None
    return {
        "id": document.id,
        "file_name": document.original_filename,
        "content_type": document.mime_type,
        "size": document.size_bytes,
        "download_url": f"/api/v1/vouchers/documents/{document.id}/download",
        "uploaded_at": document.created_at,
        "link_id": link.id if link else None,
        "match_confidence": link.match_score if link else None,
        "printed": bool(link.invoice_printed) if link and link.link_type == "invoice" else False,
    }


async def _receipt_invoice_rows(
    receipt_document_ids: list[int],
    db: AsyncSession,
) -> dict[int, list[tuple[VoucherReceiptInvoiceLink, VoucherDocument]]]:
    grouped: dict[
        int,
        list[tuple[VoucherReceiptInvoiceLink, VoucherDocument]],
    ] = defaultdict(list)
    if not receipt_document_ids:
        return grouped
    rows = (
        await db.execute(
            select(VoucherReceiptInvoiceLink, VoucherDocument)
            .join(
                VoucherDocument,
                VoucherDocument.id
                == VoucherReceiptInvoiceLink.invoice_document_id,
            )
            .where(
                VoucherReceiptInvoiceLink.receipt_document_id.in_(
                    receipt_document_ids
                )
            )
            .order_by(
                VoucherReceiptInvoiceLink.receipt_document_id,
                VoucherReceiptInvoiceLink.position,
                VoucherReceiptInvoiceLink.id,
            )
        )
    ).all()
    for association, document in rows:
        grouped[association.receipt_document_id].append((association, document))
    return grouped


async def _apply_feishu_comment_states(
    payloads: list[dict[str, Any]],
    db: AsyncSession,
) -> None:
    """Annotate receipt-owned invoices with their Feishu delivery state."""
    item_ids = [int(row["id"]) for row in payloads]
    if not item_ids:
        return
    rows = (
        await db.execute(
            select(
                VoucherDocumentLink.item_id,
                VoucherReceiptInvoiceLink.invoice_document_id,
                VoucherFeishuCommentSync.status,
                VoucherFeishuCommentSync.last_error,
            )
            .join(
                VoucherReceiptInvoiceLink,
                VoucherReceiptInvoiceLink.receipt_document_id
                == VoucherDocumentLink.document_id,
            )
            .outerjoin(
                VoucherFeishuCommentSync,
                and_(
                    VoucherFeishuCommentSync.item_id
                    == VoucherDocumentLink.item_id,
                    VoucherFeishuCommentSync.receipt_document_id
                    == VoucherReceiptInvoiceLink.receipt_document_id,
                    VoucherFeishuCommentSync.invoice_document_id
                    == VoucherReceiptInvoiceLink.invoice_document_id,
                ),
            )
            .where(
                VoucherDocumentLink.item_id.in_(item_ids),
                VoucherDocumentLink.link_type == "bank_receipt",
                VoucherReceiptInvoiceLink.owns_invoice.is_(True),
            )
        )
    ).all()
    states = {
        (int(item_id), int(invoice_id)): {
            "feishu_comment_status": status or "pending",
            "feishu_comment_error": last_error,
        }
        for item_id, invoice_id, status, last_error in rows
    }
    for payload in payloads:
        for attachment in payload.get("invoice_attachments") or []:
            state = states.get((int(payload["id"]), int(attachment["id"])))
            if state:
                attachment["from_receipt"] = True
                attachment.update(state)


def _state(item: VoucherItem, context: BatchContext) -> dict[str, Any]:
    if not item.requires_voucher:
        return {
            "status": "no_voucher_required",
            "has_invoice": True,
            "has_receipt": True,
            "complete": True,
            "pending_confirmation": False,
        }
    links = context.links_by_item.get(item.id, [])
    link_types = {link.link_type for link in links}
    has_invoice = "invoice" in link_types
    has_receipt = "bank_receipt" in link_types
    pending_confirmation = False
    if not has_receipt:
        pending_confirmation = any(
            score_receipt(item, document)["score"] >= 70
            for document in context.pending_receipts
        )
    if has_invoice and has_receipt:
        status = "complete"
    elif not has_invoice:
        status = "missing_invoice"
    elif pending_confirmation:
        status = "pending_confirmation"
    else:
        status = "missing_receipt"
    return {
        "status": status,
        "has_invoice": has_invoice,
        "has_receipt": has_receipt,
        "complete": has_invoice and has_receipt,
        "pending_confirmation": pending_confirmation,
    }


def _item_payload(
    item: VoucherItem, batch: VoucherBatch, context: BatchContext
) -> dict[str, Any]:
    links = context.links_by_item.get(item.id, [])
    invoices = [
        (context.documents[link.document_id], link)
        for link in links
        if link.link_type == "invoice" and link.document_id in context.documents
    ]
    receipt_links = [link for link in links if link.link_type == "bank_receipt"]
    receipts = [
        (context.documents[link.document_id], link)
        for link in receipt_links
        if link.document_id in context.documents
    ]
    state = _state(item, context)
    approval_details = [
        {"label": label, "value": value}
        for label, value in extract_approval_form_rows(
            item.raw_summaries_snapshot
        )
    ]
    source = parse_source_attachment_metadata(item.raw_summaries_snapshot)
    candidates = [
        {
            "file_name": row.get("name") or row.get("token") or "飞书附件",
            "source": f"feishu_{row.get('control_type') or 'attachment'}",
            "raw_summary": json.dumps(row, ensure_ascii=False),
        }
        for row in source
    ]
    match_confidence = receipt_links[0].match_score if receipt_links else None
    return {
        "id": item.id,
        "voucher_no": f"{batch.year_month.replace('-', '')}-{item.sequence:04d}",
        "approval_id": item.approval_id,
        "instance_code": item.instance_code,
        "source_line_key": item.source_line_key,
        "source_line_label": item.source_line_label,
        "definition_name": item.definition_name,
        "applicant_name": item.applicant_name,
        "payee_name": item.payee_name_snapshot,
        "expense_date": item.submit_date or item.expense_date,
        "submit_date": item.submit_date,
        "description": item.description,
        "approval_details": approval_details,
        "amount": item.amount if item.amount is not None else (item.amount_cny or 0),
        "amount_cny": item.amount_cny,
        "currency": item.currency or "CNY",
        "requires_voucher": bool(item.requires_voucher),
        "voucher_exclusion_reason": item.voucher_exclusion_reason,
        "include_approval_in_monthly_pdf": bool(
            item.include_approval_in_monthly_pdf
        ),
        "status": state["status"],
        "invoice_attachment": _attachment_payload(*invoices[0]) if invoices else None,
        "invoice_attachments": [
            _attachment_payload(document, link) for document, link in invoices
        ],
        "receipt_attachment": _attachment_payload(*receipts[0]) if receipts else None,
        "receipt_attachments": [
            _attachment_payload(document, link) for document, link in receipts
        ],
        "source_attachment_candidates": candidates,
        "source_attachment_candidate_count": len(candidates),
        "match_confidence": match_confidence,
        "updated_at": item.updated_at,
    }


def _summary(items: list[VoucherItem], context: BatchContext) -> dict[str, int]:
    states = [_state(item, context) for item in items]
    required_states = [
        state for item, state in zip(items, states) if item.requires_voucher
    ]
    return {
        "total": len(items),
        "required_total": len(required_states),
        "no_voucher_required": sum(not item.requires_voucher for item in items),
        "complete": sum(state["complete"] for state in required_states),
        "missing_invoice": sum(not state["has_invoice"] for state in required_states),
        "missing_receipt": sum(not state["has_receipt"] for state in required_states),
        "pending_confirmation": sum(state["pending_confirmation"] for state in required_states),
        "receipt_total": sum(
            document.document_type == "bank_receipt"
            for document in context.documents.values()
        ),
        "unmatched_receipts": len(context.pending_receipts),
    }


def _batch_payload_from_context(
    batch: VoucherBatch, items: list[VoucherItem], context: BatchContext
) -> dict[str, Any]:
    return {
        "id": batch.id,
        "year_month": batch.year_month,
        "status": batch.status,
        "item_count": len(items),
        "summary": _summary(items, context),
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


async def _batch_payload(batch: VoucherBatch, db: AsyncSession) -> dict[str, Any]:
    items = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(VoucherItem.batch_id == batch.id)
                .order_by(VoucherItem.sequence, VoucherItem.id)
            )
        ).all()
    )
    context = await _load_context(batch.id, items, db)
    return _batch_payload_from_context(batch, items, context)


async def _refresh_batch_status(batch_id: int, db: AsyncSession) -> None:
    batch = await db.get(VoucherBatch, batch_id)
    if not batch or batch.status == "archived":
        return
    items = list(
        (await db.scalars(select(VoucherItem).where(VoucherItem.batch_id == batch_id))).all()
    )
    context = await _load_context(batch_id, items, db)
    batch.status = "ready" if items and all(_state(i, context)["complete"] for i in items) else "processing"
    batch.item_count = len(items)


@router.post("/batches")
async def create_batch(
    data: VoucherBatchCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a month, append new approvals, and refresh source snapshots."""
    _admin(user)
    _validate_voucher_month(data.year_month)
    if data.sync_feishu:
        await _sync_feishu_for_voucher_month(data.year_month, db)
    batch = await db.scalar(
        select(VoucherBatch).where(VoucherBatch.year_month == data.year_month)
    )
    if not batch:
        try:
            batch = VoucherBatch(
                year_month=data.year_month,
                status="draft",
                item_count=0,
                created_by=user.id,
            )
            db.add(batch)
            await db.flush()
        except IntegrityError:
            # Two tabs may open a new month at the same time. Reuse the row
            # that won the unique year_month race instead of returning 500.
            await db.rollback()
            batch = await db.scalar(
                select(VoucherBatch).where(
                    VoucherBatch.year_month == data.year_month
                )
            )
            if not batch:
                raise
    if batch.status == "archived":
        return await _batch_payload(batch, db)

    approvals = list(
        (
            await db.scalars(
                select(Approval)
                .where(
                    Approval.status == 2,
                    func.substr(
                        func.coalesce(Approval.submit_date, Approval.expense_date), 1, 7
                    )
                    == data.year_month,
                )
                .order_by(
                    func.coalesce(Approval.submit_date, Approval.expense_date),
                    Approval.id,
                )
            )
        ).all()
    )
    existing_items = list(
        (
            await db.scalars(
                select(VoucherItem).where(VoucherItem.batch_id == batch.id)
            )
        ).all()
    )
    existing_approval_ids = {item.approval_id for item in existing_items}
    existing_sources: dict[int, Approval] = {}
    if existing_approval_ids:
        existing_sources = {
            approval.id: approval
            for approval in (
                await db.scalars(
                    select(Approval).where(Approval.id.in_(existing_approval_ids))
                )
            ).all()
        }

    # Existing voucher rows are stable identities, but their source-owned
    # snapshots must follow the latest approval content and status.
    for item in existing_items:
        approval = existing_sources.get(item.approval_id)
        if approval:
            _refresh_item_snapshot(item, approval, data.year_month)
    next_sequence = (
        await db.scalar(
            select(func.coalesce(func.max(VoucherItem.sequence), 0)).where(
                VoucherItem.batch_id == batch.id
            )
        )
        or 0
    )
    added_count = 0
    for approval in approvals:
        if approval.id in existing_approval_ids:
            continue
        requires_voucher, exclusion_reason = _infer_requires_voucher(approval)
        next_sequence += 1
        added_count += 1
        db.add(
            VoucherItem(
                batch_id=batch.id,
                approval_id=approval.id,
                source_line_key="main",
                source_line_label=None,
                sequence=next_sequence,
                instance_code=approval.instance_code,
                definition_name=approval.definition_name,
                applicant_name=approval.applicant_name,
                approval_status=approval.status,
                payee_name_snapshot=extract_payee_name(approval.raw_summaries),
                amount=approval.amount,
                currency=approval.currency,
                amount_cny=approval.amount_cny,
                category_id=approval.category_id,
                expense_date=approval.expense_date,
                submit_date=approval.submit_date,
                description=approval.description,
                raw_summaries_snapshot=approval.raw_summaries,
                requires_voucher=requires_voucher,
                voucher_exclusion_reason=exclusion_reason,
                # Approval forms are normally printed before voucher binding.
                # Monthly exports therefore include them only by exception.
                include_approval_in_monthly_pdf=False,
            )
        )
        existing_approval_ids.add(approval.id)
    if added_count:
        batch.item_count = len(existing_items) + added_count
        batch.status = "processing"
    repair_result = await _repair_batch_receipt_metadata(batch.id, db)
    if repair_result["unlinked_income"]:
        batch.status = "processing"
    await _refresh_batch_status(batch.id, db)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent page loads may append the same newly approved rows. The
        # database uniqueness rule wins; return the already-created batch.
        await db.rollback()
        batch = await db.scalar(
            select(VoucherBatch).where(VoucherBatch.year_month == data.year_month)
        )
        if not batch:
            raise
    await db.refresh(batch)
    return await _batch_payload(batch, db)


@router.get("/batches")
async def list_batches(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batches = list(
        (
            await db.scalars(
                select(VoucherBatch)
                .where(VoucherBatch.year_month >= VOUCHER_START_MONTH)
                .order_by(VoucherBatch.year_month.desc())
            )
        ).all()
    )
    if not batches:
        return []
    batch_ids = [batch.id for batch in batches]
    items = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(VoucherItem.batch_id.in_(batch_ids))
                .order_by(VoucherItem.batch_id, VoucherItem.sequence, VoucherItem.id)
            )
        ).all()
    )
    documents = list(
        (
            await db.scalars(
                select(VoucherDocument)
                .where(VoucherDocument.batch_id.in_(batch_ids))
                .order_by(VoucherDocument.id)
            )
        ).all()
    )
    item_ids = [item.id for item in items]
    document_ids = [document.id for document in documents]
    link_conditions = []
    if item_ids:
        link_conditions.append(VoucherDocumentLink.item_id.in_(item_ids))
    if document_ids:
        link_conditions.append(VoucherDocumentLink.document_id.in_(document_ids))
    links = (
        list(
            (
                await db.scalars(
                    select(VoucherDocumentLink)
                    .where(or_(*link_conditions))
                    .order_by(VoucherDocumentLink.id)
                )
            ).all()
        )
        if link_conditions
        else []
    )
    items_by_batch: dict[int, list[VoucherItem]] = defaultdict(list)
    docs_by_batch: dict[int, list[VoucherDocument]] = defaultdict(list)
    links_by_batch: dict[int, list[VoucherDocumentLink]] = defaultdict(list)
    item_batch = {item.id: item.batch_id for item in items}
    document_batch = {document.id: document.batch_id for document in documents}
    for item in items:
        items_by_batch[item.batch_id].append(item)
    for document in documents:
        docs_by_batch[document.batch_id].append(document)
    for link in links:
        if link.item_id in item_batch:
            links_by_batch[item_batch[link.item_id]].append(link)
        elif link.document_id in document_batch:
            links_by_batch[document_batch[link.document_id]].append(link)
    return [
        _batch_payload_from_context(
            batch,
            items_by_batch[batch.id],
            _context_from_loaded(
                items_by_batch[batch.id],
                docs_by_batch[batch.id],
                links_by_batch[batch.id],
            ),
        )
        for batch in batches
    ]


@router.get("/incomplete")
async def list_incomplete_vouchers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return every still-incomplete approval across all voucher months."""
    _admin(user)
    has_invoice = exists(
        select(1).where(
            VoucherDocumentLink.item_id == VoucherItem.id,
            VoucherDocumentLink.link_type == "invoice",
        )
    )
    has_receipt = exists(
        select(1).where(
            VoucherDocumentLink.item_id == VoucherItem.id,
            VoucherDocumentLink.link_type == "bank_receipt",
        )
    )
    rows = list(
        (
            await db.execute(
                select(VoucherItem, VoucherBatch)
                .join(VoucherBatch, VoucherBatch.id == VoucherItem.batch_id)
                .where(
                    VoucherBatch.year_month >= VOUCHER_START_MONTH,
                    VoucherItem.requires_voucher.is_(True),
                    or_(~has_invoice, ~has_receipt),
                )
                .order_by(
                    VoucherBatch.year_month.desc(),
                    VoucherItem.sequence,
                    VoucherItem.id,
                )
            )
        ).all()
    )

    item_ids = [item.id for item, _batch in rows]
    link_types_by_item: dict[int, set[str]] = defaultdict(set)
    if item_ids:
        link_rows = (
            await db.execute(
                select(
                    VoucherDocumentLink.item_id,
                    VoucherDocumentLink.link_type,
                ).where(VoucherDocumentLink.item_id.in_(item_ids))
            )
        ).all()
        for item_id, link_type in link_rows:
            link_types_by_item[int(item_id)].add(str(link_type))

    months: dict[str, dict[str, Any]] = {}
    missing_invoice_total = 0
    missing_receipt_total = 0
    for item, batch in rows:
        link_types = link_types_by_item.get(item.id, set())
        missing_invoice = "invoice" not in link_types
        missing_receipt = "bank_receipt" not in link_types
        missing_invoice_total += int(missing_invoice)
        missing_receipt_total += int(missing_receipt)
        month = months.setdefault(
            batch.year_month,
            {
                "batch_id": batch.id,
                "batch_status": batch.status,
                "year_month": batch.year_month,
                "count": 0,
                "missing_invoice": 0,
                "missing_receipt": 0,
                "items": [],
            },
        )
        month["count"] += 1
        month["missing_invoice"] += int(missing_invoice)
        month["missing_receipt"] += int(missing_receipt)
        month["items"].append(
            {
                "item_id": item.id,
                "voucher_no": (
                    f"{batch.year_month.replace('-', '')}-{item.sequence:04d}"
                ),
                "approval_id": item.approval_id,
                "instance_code": item.instance_code,
                "definition_name": item.definition_name,
                "applicant_name": item.applicant_name,
                "payee_name": item.payee_name_snapshot,
                "description": item.description,
                "amount": item.amount,
                "amount_cny": item.amount_cny,
                "currency": item.currency or "CNY",
                "date": item.expense_date or item.submit_date,
                "missing_invoice": missing_invoice,
                "missing_receipt": missing_receipt,
            }
        )
    return {
        "total": len(rows),
        "missing_invoice": missing_invoice_total,
        "missing_receipt": missing_receipt_total,
        "months": list(months.values()),
    }


@router.get("/batches/by-month")
async def get_batch_by_month(
    year_month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    _validate_voucher_month(year_month)
    batch = await db.scalar(
        select(VoucherBatch).where(VoucherBatch.year_month == year_month)
    )
    if not batch:
        raise HTTPException(status_code=404, detail="该月尚未创建凭证批次")
    return await _batch_payload(batch, db)


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    return await _batch_payload(await _get_batch(batch_id, db), db)


@router.get("/batches/{batch_id}/items")
async def list_batch_items(
    batch_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    keyword: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    items = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(VoucherItem.batch_id == batch_id)
                .order_by(VoucherItem.sequence, VoucherItem.id)
            )
        ).all()
    )
    context = await _load_context(batch_id, items, db)
    payloads = [_item_payload(item, batch, context) for item in items]
    await _apply_feishu_comment_states(payloads, db)
    if keyword:
        needle = keyword.strip().lower()
        payloads = [
            row
            for row in payloads
            if any(
                needle in str(row.get(field) or "").lower()
                for field in (
                    "voucher_no", "approval_id", "applicant_name", "payee_name", "description"
                )
            )
        ]
    allowed_statuses = {
        "complete",
        "missing_invoice",
        "missing_receipt",
        "pending_confirmation",
        "no_voucher_required",
    }
    if status and status != "all":
        if status not in allowed_statuses:
            raise HTTPException(status_code=422, detail="无效的凭证状态筛选")
        if status == "missing_invoice":
            payloads = [
                row for row in payloads
                if row["requires_voucher"] and not row["invoice_attachments"]
            ]
        elif status == "missing_receipt":
            payloads = [
                row for row in payloads
                if row["requires_voucher"] and not row["receipt_attachments"]
            ]
        elif status == "no_voucher_required":
            payloads = [row for row in payloads if not row["requires_voucher"]]
        else:
            payloads = [row for row in payloads if row["status"] == status]
    total = len(payloads)
    start = (page - 1) * page_size
    return {
        "items": payloads[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def _store_document(
    upload: UploadFile,
    document_type: str,
    batch_id: int,
    user: User,
    db: AsyncSession,
    amount: float | None = None,
    payee: str | None = None,
    transaction_date: date | None = None,
    bank_reference: str | None = None,
) -> tuple[VoucherDocument, bool]:
    limit = settings.VOUCHER_MAX_FILE_SIZE
    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {limit // (1024 * 1024)}MB 限制",
        )
    if not content:
        raise HTTPException(status_code=400, detail="不能上传空文件")
    clean_name = Path(upload.filename or "unnamed").name
    try:
        suffix, mime = await run_in_threadpool(validate_upload, clean_name, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    extracted: dict[str, Any] = {}
    if document_type == "bank_receipt":
        extracted = await run_in_threadpool(
            extract_receipt_metadata, clean_name, content, mime
        )
    digest = await run_in_threadpool(sha256_bytes, content)
    resolved_bank_reference = (
        bank_reference
        if bank_reference is not None
        else extracted.get("bank_reference")
    )
    if isinstance(resolved_bank_reference, str):
        resolved_bank_reference = resolved_bank_reference.strip() or None
    existing = await db.scalar(
        select(VoucherDocument).where(
            VoucherDocument.batch_id == batch_id,
            VoucherDocument.document_type == document_type,
            VoucherDocument.sha256 == digest,
        )
    )
    if not existing and document_type == "bank_receipt" and resolved_bank_reference:
        existing = await db.scalar(
            select(VoucherDocument).where(
                VoucherDocument.batch_id == batch_id,
                VoucherDocument.document_type == "bank_receipt",
                VoucherDocument.bank_reference == resolved_bank_reference,
            )
        )
    if existing:
        # Re-uploading the same receipt with explicit fields corrects extraction.
        legacy_receipt = bool(
            document_type == "bank_receipt"
            and (
                existing.receipt_direction is None
                or existing.receipt_category is None
            )
        )
        if amount is not None:
            existing.amount = amount
        elif existing.amount is None and extracted.get("amount") is not None:
            existing.amount = extracted["amount"]
        if payee is not None:
            existing.payee = payee
        elif extracted.get("payee") and (
            existing.payee is None or legacy_receipt
        ):
            existing.payee = extracted["payee"]
        if transaction_date is not None:
            existing.transaction_date = transaction_date
        elif (
            existing.transaction_date is None
            and extracted.get("transaction_date") is not None
        ):
            existing.transaction_date = extracted["transaction_date"]
        if bank_reference is not None:
            existing.bank_reference = bank_reference
        elif not existing.bank_reference and extracted.get("bank_reference"):
            existing.bank_reference = extracted["bank_reference"]
        if document_type == "bank_receipt":
            existing.receipt_direction = extracted.get("receipt_direction")
            if existing.receipt_category in (None, "pending"):
                existing.receipt_category = extracted.get(
                    "receipt_category", "pending"
                )
        await db.flush()
        return existing, True

    file_uuid = str(uuid4())
    relative = f"documents/{batch_id}/{file_uuid}.{suffix}"
    root = Path(settings.VOUCHER_FILE_ROOT)
    try:
        target = safe_resolve(root, relative)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def write_file() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    await run_in_threadpool(write_file)
    document = VoucherDocument(
        batch_id=batch_id,
        uuid=file_uuid,
        document_type=document_type,
        original_filename=clean_name,
        storage_path=relative,
        mime_type=mime,
        size_bytes=len(content),
        sha256=digest,
        amount=amount if amount is not None else extracted.get("amount"),
        payee=payee if payee is not None else extracted.get("payee"),
        bank_reference=resolved_bank_reference,
        transaction_date=(
            transaction_date
            if transaction_date is not None
            else extracted.get("transaction_date")
        ),
        receipt_direction=(
            extracted.get("receipt_direction")
            if document_type == "bank_receipt"
            else None
        ),
        receipt_category=(
            extracted.get("receipt_category", "pending")
            if document_type == "bank_receipt"
            else None
        ),
        uploaded_by=user.id,
    )
    db.add(document)
    try:
        await db.flush()
    except Exception:
        await run_in_threadpool(target.unlink, True)
        raise
    return document, False


@router.post("/batches/{batch_id}/receipts")
async def upload_receipts(
    batch_id: int,
    files: list[UploadFile] = File(...),
    amount: float | None = Form(None),
    payee: str | None = Form(None),
    transaction_date: date | None = Form(None),
    metadata_json: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="metadata_json 不是有效 JSON") from exc
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata_json 必须是对象")

    uploaded = duplicates = matched = pending_confirmation = split_pages = 0
    failed: list[dict[str, str]] = []
    for upload in files:
        original_name = Path(upload.filename or "unnamed").name
        per_file = metadata.get(upload.filename or "", {})
        if not isinstance(per_file, dict):
            failed.append({"file_name": original_name, "reason": "元数据无效"})
            continue
        try:
            content = await upload.read(MAX_RECEIPT_BATCH_UPLOAD_BYTES + 1)
            if len(content) > MAX_RECEIPT_BATCH_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="批量回单文件超过 200MB 限制")
            if not content:
                raise HTTPException(status_code=400, detail="不能上传空文件")
            receipt_files = (
                await run_in_threadpool(split_pdf_pages, original_name, content)
                if content.startswith(b"%PDF")
                else [(original_name, content)]
            )
            split_pages += max(0, len(receipt_files) - 1)
            # A single form row describes the uploaded file, not every page of
            # a bank batch PDF. Let each split page extract its own metadata.
            use_manual_metadata = len(receipt_files) == 1
            raw_date = per_file.get("transaction_date") if use_manual_metadata else None
            tx_date = (
                date.fromisoformat(raw_date)
                if raw_date
                else transaction_date if use_manual_metadata else None
            )
            for receipt_name, receipt_content in receipt_files:
                doc, deduplicated = await _store_document(
                    UploadFile(file=io.BytesIO(receipt_content), filename=receipt_name),
                    "bank_receipt",
                    batch_id,
                    user,
                    db,
                    amount=(per_file.get("amount", amount) if use_manual_metadata else None),
                    payee=(per_file.get("payee", payee) if use_manual_metadata else None),
                    transaction_date=tx_date,
                    bank_reference=(
                        per_file.get("bank_reference")
                        if use_manual_metadata
                        else None
                    ),
                )
                if deduplicated:
                    duplicates += 1
                else:
                    uploaded += 1
                link = await _auto_confirm_receipt_by_amount(doc, user, db)
                if link:
                    matched += 1
                else:
                    pending_confirmation += 1
        except HTTPException as exc:
            failed.append(
                {"file_name": original_name, "reason": str(exc.detail)}
            )
        except (TypeError, ValueError):
            failed.append(
                {"file_name": original_name, "reason": "元数据无效"}
            )
    if uploaded and batch.status == "draft":
        batch.status = "processing"
    await db.commit()
    return {
        "uploaded": uploaded,
        "matched": matched,
        "pending_confirmation": pending_confirmation,
        "duplicates": duplicates,
        "split_pages": split_pages,
        "failed": failed,
    }


@router.post("/batches/{batch_id}/receipts/auto-match")
async def auto_match_batch_receipts(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    result = await _auto_confirm_batch_receipts_by_amount(batch_id, user, db)
    await db.commit()
    # Amount/scoring based matches are intentionally local-only. Receipt-owned
    # invoices are queued by ``_confirm_document`` but external comments are sent
    # only after an administrator explicitly confirms or retries an item.
    return result


@router.get("/batches/{batch_id}/receipts")
async def list_batch_receipts(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    items = list(
        (await db.scalars(select(VoucherItem).where(VoucherItem.batch_id == batch_id))).all()
    )
    context = await _load_context(batch_id, items, db)
    receipts = context.unlinked_receipts
    invoice_rows_by_receipt = await _receipt_invoice_rows(
        [document.id for document in receipts],
        db,
    )
    rows = []
    for document in receipts:
        linked_item = context.linked_item_by_document.get(document.id)
        rows.append(
            {
                "id": document.id,
                "file_name": document.original_filename,
                "content_type": document.mime_type,
                "size": document.size_bytes,
                "amount": document.amount,
                "payee_name": document.payee,
                "transaction_date": document.transaction_date,
                "bank_reference": document.bank_reference,
                "receipt_direction": document.receipt_direction or "unknown",
                "receipt_category": document.receipt_category or "pending",
                "uploaded_at": document.created_at,
                "download_url": f"/api/v1/vouchers/documents/{document.id}/download",
                "linked_item_id": linked_item.id if linked_item else None,
                "linked_voucher_no": (
                    f"{batch.year_month.replace('-', '')}-{linked_item.sequence:04d}"
                    if linked_item
                    else None
                ),
                "invoice_attachments": [
                    {
                        **(_attachment_payload(invoice) or {}),
                        "referenced_from_approval": not bool(
                            association.owns_invoice
                        ),
                    }
                    for association, invoice in invoice_rows_by_receipt.get(
                        document.id,
                        [],
                    )
                ],
                "can_upload_invoice": batch.status != "archived",
                "can_delete": True,
            }
        )
    return rows


@router.get("/documents/{receipt_document_id}/invoice-candidates")
async def list_receipt_invoice_candidates(
    receipt_document_id: int,
    keyword: str | None = Query(None, max_length=120),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List reusable invoices already attached to voucher approvals."""
    _admin(user)
    receipt = await db.get(VoucherDocument, receipt_document_id)
    if not receipt or receipt.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="银行回单不存在")
    receipt_batch = await _get_batch(receipt.batch_id, db)
    rows = list(
        (
            await db.execute(
                select(
                    VoucherDocument,
                    VoucherDocumentLink,
                    VoucherItem,
                    VoucherBatch,
                )
                .join(
                    VoucherDocumentLink,
                    VoucherDocumentLink.document_id == VoucherDocument.id,
                )
                .join(VoucherItem, VoucherItem.id == VoucherDocumentLink.item_id)
                .join(VoucherBatch, VoucherBatch.id == VoucherItem.batch_id)
                .where(
                    VoucherDocument.document_type == "invoice",
                    VoucherDocumentLink.link_type == "invoice",
                    VoucherBatch.year_month >= VOUCHER_START_MONTH,
                    VoucherBatch.year_month <= receipt_batch.year_month,
                )
                .order_by(
                    VoucherBatch.year_month.desc(),
                    VoucherItem.sequence,
                    VoucherDocumentLink.id,
                )
                .limit(500)
            )
        ).all()
    )
    document_ids = list(dict.fromkeys(document.id for document, *_rest in rows))
    associated_invoice_ids = set(
        (
            await db.scalars(
                select(VoucherReceiptInvoiceLink.invoice_document_id).where(
                    VoucherReceiptInvoiceLink.invoice_document_id.in_(document_ids)
                )
            )
        ).all()
    ) if document_ids else set()
    needle = (keyword or "").strip().lower()
    candidates = []
    seen_document_ids: set[int] = set()
    for document, _link, item, batch in rows:
        if document.id in seen_document_ids or document.id in associated_invoice_ids:
            continue
        seen_document_ids.add(document.id)
        voucher_no = f"{batch.year_month.replace('-', '')}-{item.sequence:04d}"
        searchable = " ".join(
            str(value or "")
            for value in (
                batch.year_month,
                voucher_no,
                item.instance_code,
                item.definition_name,
                item.applicant_name,
                item.payee_name_snapshot,
                item.description,
                item.amount,
                document.original_filename,
            )
        ).lower()
        if needle and needle not in searchable:
            continue
        candidates.append(
            {
                "document_id": document.id,
                "item_id": item.id,
                "year_month": batch.year_month,
                "voucher_no": voucher_no,
                "instance_code": item.instance_code,
                "definition_name": item.definition_name,
                "applicant_name": item.applicant_name,
                "payee_name": item.payee_name_snapshot,
                "description": item.description,
                "amount": item.amount,
                "currency": item.currency or "CNY",
                "invoice_attachment": _attachment_payload(document),
            }
        )
        if len(candidates) >= 300:
            break
    return candidates


@router.post("/documents/{receipt_document_id}/invoice-references")
async def reference_receipt_invoices(
    receipt_document_id: int,
    data: VoucherReceiptInvoiceReferenceCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reference approval invoices without copying or moving their files."""
    _admin(user)
    if len(data.document_ids) != len(set(data.document_ids)):
        raise HTTPException(status_code=422, detail="引用发票不能重复选择")
    receipt = await db.get(VoucherDocument, receipt_document_id)
    if not receipt or receipt.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="银行回单不存在")
    receipt_batch = await _get_batch(receipt.batch_id, db)
    if receipt_batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    receipt_linked = await db.scalar(
        select(VoucherDocumentLink.id)
        .where(VoucherDocumentLink.document_id == receipt.id)
        .limit(1)
    )
    if receipt_linked:
        raise HTTPException(status_code=409, detail="已匹配回单不能在回单池引用发票")

    documents = list(
        (
            await db.scalars(
                select(VoucherDocument).where(
                    VoucherDocument.id.in_(data.document_ids)
                )
            )
        ).all()
    )
    document_map = {document.id: document for document in documents}
    if len(document_map) != len(data.document_ids) or any(
        document.document_type != "invoice" for document in documents
    ):
        raise HTTPException(status_code=404, detail="部分审批发票不存在")
    source_link_ids = set(
        (
            await db.scalars(
                select(VoucherDocumentLink.document_id).where(
                    VoucherDocumentLink.document_id.in_(data.document_ids),
                    VoucherDocumentLink.link_type == "invoice",
                )
            )
        ).all()
    )
    if source_link_ids != set(data.document_ids):
        raise HTTPException(status_code=409, detail="只能引用已关联审批的发票")

    source_batches = {
        batch.id: batch
        for batch in (
            await db.scalars(
                select(VoucherBatch).where(
                    VoucherBatch.id.in_(
                        {document.batch_id for document in documents}
                    )
                )
            )
        ).all()
    }
    if any(
        document.batch_id not in source_batches
        or source_batches[document.batch_id].year_month > receipt_batch.year_month
        for document in documents
    ):
        raise HTTPException(status_code=409, detail="不能引用回单月份之后的发票")

    existing_associations = list(
        (
            await db.scalars(
                select(VoucherReceiptInvoiceLink).where(
                    VoucherReceiptInvoiceLink.invoice_document_id.in_(
                        data.document_ids
                    )
                )
            )
        ).all()
    )
    association_by_invoice = {
        association.invoice_document_id: association
        for association in existing_associations
    }
    conflicting = [
        document_map[document_id].original_filename
        for document_id, association in association_by_invoice.items()
        if association.receipt_document_id != receipt.id
    ]
    if conflicting:
        raise HTTPException(
            status_code=409,
            detail=f"发票已随其他回单保存：{'、'.join(conflicting[:3])}",
        )

    root = Path(settings.VOUCHER_FILE_ROOT)
    for document in documents:
        try:
            path = safe_resolve(root, document.storage_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(
                status_code=409,
                detail=f"源发票文件缺失：{document.original_filename}",
            )

    last_position = int(
        await db.scalar(
            select(func.max(VoucherReceiptInvoiceLink.position)).where(
                VoucherReceiptInvoiceLink.receipt_document_id == receipt.id
            )
        )
        or 0
    )
    linked = 0
    skipped = 0
    for document_id in data.document_ids:
        existing = association_by_invoice.get(document_id)
        if existing:
            skipped += 1
            continue
        last_position += 1
        db.add(
            VoucherReceiptInvoiceLink(
                receipt_document_id=receipt.id,
                invoice_document_id=document_id,
                position=last_position,
                owns_invoice=False,
                created_by=user.id,
            )
        )
        linked += 1
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="部分发票已被其他回单引用，请刷新后重试",
        ) from exc
    return {
        "ok": True,
        "linked": linked,
        "skipped": skipped,
        "invoice_attachments": [
            {
                **(_attachment_payload(document_map[document_id]) or {}),
                "referenced_from_approval": True,
            }
            for document_id in data.document_ids
        ],
    }


@router.post("/documents/{receipt_document_id}/invoices")
async def upload_receipt_invoice(
    receipt_document_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach an optional invoice to one receipt while it remains in the pool."""
    _admin(user)
    receipt = await db.get(VoucherDocument, receipt_document_id)
    if not receipt or receipt.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="银行回单不存在")
    batch = await _get_batch(receipt.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    receipt_linked = await db.scalar(
        select(VoucherDocumentLink.id)
        .where(VoucherDocumentLink.document_id == receipt.id)
        .limit(1)
    )
    if receipt_linked:
        raise HTTPException(status_code=409, detail="已匹配回单不能在回单池添加发票")

    invoice, deduplicated = await _store_document(
        file,
        "invoice",
        receipt.batch_id,
        user,
        db,
    )
    existing_association = await db.scalar(
        select(VoucherReceiptInvoiceLink).where(
            VoucherReceiptInvoiceLink.invoice_document_id == invoice.id
        )
    )
    if existing_association:
        if existing_association.receipt_document_id != receipt.id:
            raise HTTPException(status_code=409, detail="这份发票已随另一张回单保存")
        await db.commit()
        return {
            "ok": True,
            "deduplicated": True,
            "receipt_document_id": receipt.id,
            "invoice_attachment": _attachment_payload(invoice),
        }
    invoice_linked = await db.scalar(
        select(VoucherDocumentLink.id)
        .where(VoucherDocumentLink.document_id == invoice.id)
        .limit(1)
    )
    if invoice_linked:
        raise HTTPException(status_code=409, detail="这份发票已关联其他凭证")

    last_position = await db.scalar(
        select(func.max(VoucherReceiptInvoiceLink.position)).where(
            VoucherReceiptInvoiceLink.receipt_document_id == receipt.id
        )
    )
    association = VoucherReceiptInvoiceLink(
        receipt_document_id=receipt.id,
        invoice_document_id=invoice.id,
        position=int(last_position or 0) + 1,
        owns_invoice=True,
        created_by=user.id,
    )
    db.add(association)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="发票已经随其他回单保存，请刷新后重试",
        ) from exc
    await db.refresh(invoice)
    return {
        "ok": True,
        "deduplicated": deduplicated,
        "receipt_document_id": receipt.id,
        "invoice_attachment": _attachment_payload(invoice),
    }


@router.delete("/documents/{receipt_document_id}/invoices/{invoice_document_id}")
async def remove_receipt_invoice(
    receipt_document_id: int,
    invoice_document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    receipt = await db.get(VoucherDocument, receipt_document_id)
    if not receipt or receipt.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="银行回单不存在")
    batch = await _get_batch(receipt.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    receipt_linked = await db.scalar(
        select(VoucherDocumentLink.id)
        .where(VoucherDocumentLink.document_id == receipt.id)
        .limit(1)
    )
    if receipt_linked:
        raise HTTPException(status_code=409, detail="已匹配回单不能在回单池移除发票")
    association = await db.scalar(
        select(VoucherReceiptInvoiceLink).where(
            VoucherReceiptInvoiceLink.receipt_document_id == receipt.id,
            VoucherReceiptInvoiceLink.invoice_document_id == invoice_document_id,
        )
    )
    if not association:
        raise HTTPException(status_code=404, detail="回单随附发票不存在")
    invoice = await db.get(VoucherDocument, invoice_document_id)
    if not invoice or invoice.document_type != "invoice":
        raise HTTPException(status_code=404, detail="回单随附发票不存在")
    path: Path | None = None
    if bool(association.owns_invoice):
        invoice_linked = await db.scalar(
            select(VoucherDocumentLink.id)
            .where(VoucherDocumentLink.document_id == invoice.id)
            .limit(1)
        )
        if invoice_linked:
            raise HTTPException(status_code=409, detail="发票已关联凭证，不能从回单池移除")
        try:
            path = safe_resolve(Path(settings.VOUCHER_FILE_ROOT), invoice.storage_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.execute(
        delete(VoucherFeishuCommentSync).where(
            VoucherFeishuCommentSync.receipt_document_id == receipt.id,
            VoucherFeishuCommentSync.invoice_document_id == invoice.id,
        )
    )
    await db.delete(association)
    if bool(association.owns_invoice):
        await db.delete(invoice)
    await db.commit()
    if path is not None:
        await run_in_threadpool(path.unlink, True)
    return {"ok": True, "deleted_document": path is not None}


@router.delete("/batches/{batch_id}/receipts")
async def clear_batch_receipts(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete every bank receipt and match in one editable monthly batch."""
    _admin(user)
    batch = await _get_batch(batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")

    documents = list(
        (
            await db.scalars(
                select(VoucherDocument)
                .where(
                    VoucherDocument.batch_id == batch.id,
                    VoucherDocument.document_type == "bank_receipt",
                )
                .order_by(VoucherDocument.id)
            )
        ).all()
    )
    if not documents:
        return {
            "ok": True,
            "deleted_receipts": 0,
            "deleted_links": 0,
            "file_cleanup_failed": 0,
        }

    document_ids = [document.id for document in documents]
    receipt_invoice_associations = list(
        (
            await db.scalars(
                select(VoucherReceiptInvoiceLink).where(
                    VoucherReceiptInvoiceLink.receipt_document_id.in_(document_ids)
                )
            )
        ).all()
    )
    owned_invoice_ids = [
        association.invoice_document_id
        for association in receipt_invoice_associations
        if bool(association.owns_invoice)
    ]
    companion_invoices = (
        list(
            (
                await db.scalars(
                    select(VoucherDocument).where(
                        VoucherDocument.id.in_(owned_invoice_ids)
                    )
                )
            ).all()
        )
        if owned_invoice_ids
        else []
    )
    if len(companion_invoices) != len(set(owned_invoice_ids)) or any(
        invoice.batch_id != batch.id or invoice.document_type != "invoice"
        for invoice in companion_invoices
    ):
        raise HTTPException(
            status_code=409,
            detail="检测到异常的回单随附发票，请先联系管理员修复数据",
        )

    root = Path(settings.VOUCHER_FILE_ROOT)
    paths = []
    for document in [*documents, *companion_invoices]:
        try:
            paths.append(safe_resolve(root, document.storage_path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    all_document_ids = [*document_ids, *owned_invoice_ids]
    cross_batch_links = await db.scalar(
        select(func.count())
        .select_from(VoucherDocumentLink)
        .join(VoucherItem, VoucherItem.id == VoucherDocumentLink.item_id)
        .where(
            VoucherDocumentLink.document_id.in_(all_document_ids),
            VoucherItem.batch_id != batch.id,
        )
    )
    if cross_batch_links:
        raise HTTPException(
            status_code=409,
            detail="检测到跨月份回单关联，请先联系管理员修复数据",
        )
    links = list(
        (
            await db.scalars(
                select(VoucherDocumentLink).where(
                    VoucherDocumentLink.document_id.in_(all_document_ids)
                )
            )
        ).all()
    )
    for link in links:
        if link.link_type == "bank_receipt":
            await _cancel_unsent_receipt_comment_syncs(
                link.item_id,
                [link.document_id],
                db,
            )
    await db.execute(
        delete(VoucherFeishuCommentSync).where(
            or_(
                VoucherFeishuCommentSync.receipt_document_id.in_(document_ids),
                VoucherFeishuCommentSync.invoice_document_id.in_(owned_invoice_ids),
            )
        )
    )
    for link in links:
        await db.delete(link)
    for association in receipt_invoice_associations:
        await db.delete(association)
    for invoice in companion_invoices:
        await db.delete(invoice)
    for document in documents:
        await db.delete(document)
    await db.flush()
    await _refresh_batch_status(batch.id, db)
    await db.commit()

    cleanup_failed = 0
    for path in paths:
        try:
            await run_in_threadpool(path.unlink, True)
        except OSError:
            # The database is already clear. Keep the operation successful and
            # report any orphaned file so maintenance can remove it later.
            cleanup_failed += 1
    result = {
        "ok": True,
        "deleted_receipts": len(documents),
        "deleted_links": len(links),
        "file_cleanup_failed": cleanup_failed,
    }
    if companion_invoices:
        result["deleted_invoices"] = len(companion_invoices)
    referenced_count = sum(
        not bool(association.owns_invoice)
        for association in receipt_invoice_associations
    )
    if referenced_count:
        result["removed_invoice_references"] = referenced_count
    return result


@router.patch("/documents/{document_id}/receipt-category")
async def update_receipt_category(
    document_id: int,
    data: VoucherReceiptPoolUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    document = await db.get(VoucherDocument, document_id)
    if not document or document.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="银行回单不存在")
    batch = await _get_batch(document.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    linked = await db.scalar(
        select(VoucherDocumentLink.id)
        .where(VoucherDocumentLink.document_id == document.id)
        .limit(1)
    )
    if linked:
        raise HTTPException(status_code=409, detail="已匹配回单不能在未匹配区分类")
    document.receipt_category = data.category
    await db.commit()
    return {
        "ok": True,
        "document_id": document.id,
        "receipt_category": document.receipt_category,
    }


@router.post("/batches/{batch_id}/receipts/export")
async def export_unmatched_receipts(
    batch_id: int,
    data: VoucherReceiptPoolExport,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    if len(data.document_ids) != len(set(data.document_ids)):
        raise HTTPException(status_code=422, detail="导出回单不能重复选择")
    documents = list(
        (
            await db.scalars(
                select(VoucherDocument).where(
                    VoucherDocument.id.in_(data.document_ids)
                )
            )
        ).all()
    )
    document_map = {document.id: document for document in documents}
    if len(document_map) != len(data.document_ids):
        raise HTTPException(status_code=404, detail="部分银行回单不存在")
    ordered = [document_map[document_id] for document_id in data.document_ids]
    if any(
        document.batch_id != batch.id
        or document.document_type != "bank_receipt"
        for document in ordered
    ):
        raise HTTPException(status_code=409, detail="只能导出本月未匹配银行回单")
    if any(
        (document.receipt_category or "pending")
        not in {"pending", "income", "other"}
        for document in ordered
    ):
        raise HTTPException(status_code=409, detail="所选回单分类无效，请刷新后重试")
    linked_count = await db.scalar(
        select(func.count())
        .select_from(VoucherDocumentLink)
        .where(VoucherDocumentLink.document_id.in_(data.document_ids))
    )
    if linked_count:
        raise HTTPException(status_code=409, detail="所选回单中包含已经匹配的回单，请刷新")
    invoice_rows_by_receipt = await _receipt_invoice_rows(data.document_ids, db)
    invoices_by_receipt = {
        receipt_id: [invoice for _association, invoice in rows]
        for receipt_id, rows in invoice_rows_by_receipt.items()
    }
    companion_invoices = [
        invoice
        for document in ordered
        for invoice in invoices_by_receipt.get(document.id, [])
    ]
    if any(
        invoice.document_type != "invoice"
        or (bool(association.owns_invoice) and invoice.batch_id != batch.id)
        for rows in invoice_rows_by_receipt.values()
        for association, invoice in rows
    ):
        raise HTTPException(status_code=409, detail="回单随附发票数据异常，请刷新后重试")
    total_bytes = sum(
        int(document.size_bytes or 0)
        for document in [*ordered, *companion_invoices]
    )
    if total_bytes > MAX_RECEIPT_BATCH_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="所选回单总大小超过 200MB，请分批导出",
        )

    root = Path(settings.VOUCHER_FILE_ROOT)
    resolved: dict[int, tuple[VoucherDocument, Path]] = {}
    for document in [*ordered, *companion_invoices]:
        try:
            path = safe_resolve(root, document.storage_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(
                status_code=409,
                detail=f"源文件缺失：{document.original_filename}",
            )
        resolved[document.id] = (document, path)

    try:
        ordered_pdfs: list[bytes] = []
        receipt_only_rows: list[tuple[VoucherDocument, Path]] = []
        for receipt in ordered:
            invoices = invoices_by_receipt.get(receipt.id, [])
            if not invoices:
                receipt_only_rows.append(resolved[receipt.id])
                continue
            if receipt_only_rows:
                ordered_pdfs.append(
                    await run_in_threadpool(
                        build_receipt_pool_pdf,
                        receipt_only_rows,
                    )
                )
                receipt_only_rows = []
            packet_rows = [
                *(resolved[invoice.id] for invoice in invoices),
                resolved[receipt.id],
            ]
            ordered_pdfs.append(
                await run_in_threadpool(build_receipt_packet_pdf, packet_rows)
            )
        if receipt_only_rows:
            ordered_pdfs.append(
                await run_in_threadpool(build_receipt_pool_pdf, receipt_only_rows)
            )
        content = await run_in_threadpool(merge_pdfs, ordered_pdfs)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    filename = f"未匹配回单-{batch.year_month}.pdf"
    export = await _record_export(
        content,
        "receipt_pool_pdf",
        filename,
        user,
        db,
        batch_id=batch.id,
    )
    await db.commit()
    await db.refresh(export)
    return _download_response(
        content,
        filename,
        "application/pdf",
        export,
        {"complete": True, "missing": []},
    )


@router.post("/items/{item_id}/invoice")
async def upload_invoice(
    item_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    if not item.requires_voucher:
        raise HTTPException(status_code=409, detail="该审批已标记为无需装订")
    document, deduplicated = await _store_document(
        file, "invoice", item.batch_id, user, db
    )
    link = await db.scalar(
        select(VoucherDocumentLink).where(
            VoucherDocumentLink.item_id == item.id,
            VoucherDocumentLink.document_id == document.id,
        )
    )
    if not link:
        link = VoucherDocumentLink(
            item_id=item.id,
            document_id=document.id,
            link_type="invoice",
            confirmed_by=user.id,
        )
        db.add(link)
        item.updated_at = func.now()
    await db.flush()
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    await db.refresh(link)
    return {
        "document_id": document.id,
        "link_id": link.id,
        "deduplicated": deduplicated,
    }


@dataclass
class FeishuImportByteBudget:
    """Shared download budget for a batch import."""

    limit_bytes: int
    used_bytes: int = 0

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)


@dataclass
class BatchFeishuImportGate:
    """Process-local coalescing state for one database batch."""

    lock: asyncio.Lock
    completed_at: float | None = None
    result: dict[str, Any] | None = None


_batch_feishu_import_gates: dict[
    tuple[object, int, bool], BatchFeishuImportGate
] = {}


def _batch_feishu_import_gate(
    batch_id: int,
    db: AsyncSession,
    *,
    rescan_existing: bool,
) -> BatchFeishuImportGate:
    # Include the bind in the key so independent databases used by tests or
    # maintenance scripts never share a batch-id cache.
    # A foreground full rescan must never reuse the cached result of a recent
    # background missing-only pass (or vice versa).
    key = (db.get_bind(), batch_id, rescan_existing)
    gate = _batch_feishu_import_gates.get(key)
    if gate is None:
        gate = BatchFeishuImportGate(lock=asyncio.Lock())
        _batch_feishu_import_gates[key] = gate
    return gate


async def _item_has_invoice_link(item_id: int, db: AsyncSession) -> bool:
    return (
        await db.scalar(
            select(VoucherDocumentLink.id).where(
                VoucherDocumentLink.item_id == item_id,
                VoucherDocumentLink.link_type == "invoice",
            ).limit(1)
        )
        is not None
    )


async def _touch_attempted_voucher_item(item_id: int, db: AsyncSession) -> bool:
    """Move an attempted missing-invoice item to the back of the fair queue."""
    item = await db.get(VoucherItem, item_id)
    if item is None:
        await db.rollback()
        return False
    # Use a Python timestamp so SQLite retains microseconds.  ``func.now()`` is
    # only second-granular there and could leave 50 fast attempts tied forever.
    item.updated_at = datetime.now()
    await db.commit()
    return True


def _safe_batch_failure_reason(reason: Any) -> str:
    """Keep batch diagnostics useful without returning signed URLs or secrets."""
    text = str(reason or "未知错误")
    text = re.sub(r"https?://\S+", "[附件地址已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", text, flags=re.IGNORECASE)
    for secret in (settings.FEISHU_APP_ID, settings.FEISHU_APP_SECRET):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:200]


async def _feishu_comment_rows_for_import(
    *,
    instance_code: str,
    token: str,
    detail: dict[str, Any],
    user: User,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read the standalone comment feed without discarding detail attachments.

    Feishu normally embeds a comment snapshot in the instance detail, but its
    dedicated comment endpoint can be fresher.  A failure on this second read
    is reported alongside any successfully imported form/detail attachments
    instead of turning the whole item into an all-or-nothing operation.
    """
    actor_open_id = (user.feishu_open_id or "").strip()
    if not actor_open_id:
        actor_open_id = str(detail.get("open_id") or "").strip()
    if not actor_open_id.startswith("ou_"):
        # The instance detail still contains its comment_list, so keep the
        # established import path available when no open-id identity exists.
        return [], []
    try:
        rows = await fetch_feishu_instance_comments_with_token(
            instance_code,
            token,
            actor_open_id,
        )
    except FeishuImportError as exc:
        return [], [
            {
                "file_name": "飞书审批评论",
                "source": "comment_api",
                "reason": _safe_batch_failure_reason(exc),
            }
        ]
    return rows, []


async def _import_item_feishu_attachments(
    item: VoucherItem,
    detail: dict[str, Any],
    user: User,
    db: AsyncSession,
    *,
    comment_rows: list[dict[str, Any]] | None = None,
    source_failures: list[dict[str, str]] | None = None,
    shared_budget: FeishuImportByteBudget | None = None,
    skip_if_invoice_exists: bool = False,
) -> dict[str, Any]:
    """Import one live instance's files using the common secure save path.

    The item endpoint and the batch endpoint both call this core.  A batch can
    provide a shared byte budget and ask the helper to stand down when a manual
    (or another automatic) invoice wins the race while Feishu is being read.
    """
    candidates = parse_feishu_instance_attachments(detail, comment_rows)
    result: dict[str, Any] = {
        "scanned": len(candidates),
        "imported": 0,
        "duplicates": 0,
        "linked": 0,
        "skipped": 0,
        "failed": list(source_failures or []),
    }
    if skip_if_invoice_exists and await _item_has_invoice_link(item.id, db):
        result["skipped"] = len(candidates)
        result["invoice_already_present"] = True
        return result
    if skip_if_invoice_exists:
        # The batch caller has no pending writes at this point.  End the
        # eligibility re-check transaction before downloading potentially large
        # files so SQLite writers are not held behind network I/O.
        await db.commit()

    changed = False
    downloaded_bytes = 0
    checked_before_first_save = False
    for index, candidate in enumerate(candidates, 1):
        filename = safe_attachment_filename(candidate, index)
        if index > MAX_ATTACHMENTS_PER_IMPORT:
            result["skipped"] += 1
            continue
        if not candidate.url:
            result["skipped"] += 1
            continue
        if (
            candidate.declared_size is not None
            and candidate.declared_size > settings.VOUCHER_MAX_FILE_SIZE
        ):
            result["failed"].append(
                {
                    "file_name": filename,
                    "source": candidate.source,
                    "reason": (
                        f"文件超过 {settings.VOUCHER_MAX_FILE_SIZE // (1024 * 1024)}MB 限制"
                    ),
                }
            )
            continue

        item_remaining = MAX_TOTAL_IMPORT_BYTES - downloaded_bytes
        shared_remaining = (
            shared_budget.remaining_bytes if shared_budget is not None else item_remaining
        )
        remaining_bytes = min(item_remaining, shared_remaining)
        if remaining_bytes <= 0:
            reason = (
                "整批飞书附件累计下载超过 200MB 限制"
                if shared_budget is not None and shared_remaining <= 0
                else "单次导入累计文件大小超过 100MB 限制"
            )
            result["failed"].append(
                {"file_name": filename, "source": candidate.source, "reason": reason}
            )
            continue
        if (
            candidate.declared_size is not None
            and candidate.declared_size > remaining_bytes
        ):
            reason = (
                "整批飞书附件累计下载超过 200MB 限制"
                if shared_budget is not None and shared_remaining <= item_remaining
                else "单次导入累计文件大小超过 100MB 限制"
            )
            result["failed"].append(
                {"file_name": filename, "source": candidate.source, "reason": reason}
            )
            continue

        download_limit = min(settings.VOUCHER_MAX_FILE_SIZE, remaining_bytes)
        try:
            content = await download_feishu_attachment(candidate.url, download_limit)
            downloaded_bytes += len(content)
            if shared_budget is not None:
                shared_budget.used_bytes += len(content)
            content = decode_feishu_base64_attachment(content)
            filename = filename_with_content_extension(filename, content)

            # The batch's eligibility query happened before a network request.
            # Re-check immediately before the first save so a manual upload that
            # arrived in that window is never supplemented or overwritten.
            if skip_if_invoice_exists and not checked_before_first_save:
                checked_before_first_save = True
                if await _item_has_invoice_link(item.id, db):
                    result["skipped"] += len(candidates) - index + 1
                    result["invoice_already_present"] = True
                    break

            async with db.begin_nested():
                document, deduplicated = await _store_document(
                    UploadFile(file=io.BytesIO(content), filename=filename),
                    "invoice",
                    item.batch_id,
                    user,
                    db,
                )
                link = await db.scalar(
                    select(VoucherDocumentLink).where(
                        VoucherDocumentLink.item_id == item.id,
                        VoucherDocumentLink.document_id == document.id,
                    )
                )
                linked = link is None
                if linked:
                    db.add(
                        VoucherDocumentLink(
                            item_id=item.id,
                            document_id=document.id,
                            link_type="invoice",
                            confirmed_by=user.id,
                        )
                    )
                    item.updated_at = func.now()
                    await db.flush()
            result["duplicates"] += int(deduplicated)
            result["imported"] += int(not deduplicated)
            result["linked"] += int(linked)
            changed = changed or linked
        except (FeishuImportError, HTTPException) as exc:
            reason = exc.detail if isinstance(exc, HTTPException) else str(exc)
            if (
                isinstance(exc, FeishuImportError)
                and download_limit < settings.VOUCHER_MAX_FILE_SIZE
                and "超过" in reason
            ):
                reason = (
                    "整批飞书附件累计下载超过 200MB 限制"
                    if shared_budget is not None
                    and shared_budget.remaining_bytes <= item_remaining
                    else "单次导入累计文件大小超过 100MB 限制"
                )
            result["failed"].append(
                {
                    "file_name": filename,
                    "source": candidate.source,
                    "reason": str(reason),
                }
            )
        except (IntegrityError, OSError, ValueError):
            result["failed"].append(
                {
                    "file_name": filename,
                    "source": candidate.source,
                    "reason": "附件保存失败",
                }
            )
    result["changed"] = changed
    return result


def _batch_import_result(
    *, configured: bool, eligible_items: int, deferred_items: int
) -> dict[str, Any]:
    return {
        "configured": configured,
        "eligible_items": eligible_items,
        "attempted_items": 0,
        "items_with_attachments": 0,
        "items_without_attachments": 0,
        "failed_items": 0,
        "deferred_items": deferred_items,
        "scanned": 0,
        "imported": 0,
        "linked": 0,
        "duplicates": 0,
        "skipped": 0,
        "failed_count": 0,
        "failures": [],
    }


def _append_batch_failure(
    result: dict[str, Any], item_id: int, voucher_no: str, reason: Any
) -> None:
    if len(result["failures"]) >= 20:
        return
    result["failures"].append(
        {
            "item_id": item_id,
            "voucher_no": voucher_no,
            "reason": _safe_batch_failure_reason(reason),
        }
    )


async def _run_batch_feishu_attachment_import(
    batch: VoucherBatch,
    user: User,
    db: AsyncSession,
    *,
    rescan_existing: bool = False,
) -> dict[str, Any]:
    """Import Feishu attachments for missing items or a requested full rescan.

    Background refreshes keep the inexpensive missing-only behaviour. An
    explicit foreground sync can re-read every approval so invoices added to an
    older approval are not hidden merely because that voucher already has one
    earlier invoice.
    """
    invoice_link_exists = (
        select(VoucherDocumentLink.id)
        .where(
            VoucherDocumentLink.item_id == VoucherItem.id,
            VoucherDocumentLink.link_type == "invoice",
        )
        .exists()
    )
    eligibility_filters = [
        VoucherItem.batch_id == batch.id,
        VoucherItem.requires_voucher.is_(True),
    ]
    if not rescan_existing:
        eligibility_filters.append(~invoice_link_exists)
    eligible = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(*eligibility_filters)
                .order_by(
                    VoucherItem.updated_at.asc(),
                    VoucherItem.sequence,
                    VoucherItem.id,
                )
            )
        ).all()
    )
    eligible_rows = [
        (item.id, item.sequence, item.instance_code) for item in eligible
    ]
    batch_pk = batch.id
    batch_year_month = batch.year_month
    # End the eligibility read transaction before any potentially slow Feishu
    # network I/O.  Production uses SQLite in some deployments, where holding a
    # read transaction here would unnecessarily delay writers.
    await db.commit()
    configured = bool(
        settings.FEISHU_APP_ID.strip() and settings.FEISHU_APP_SECRET.strip()
    )
    if not configured:
        # Approval sync calls this endpoint opportunistically.  Missing Feishu
        # credentials must leave invoices pending for manual upload, not turn a
        # successful approval sync into an error.
        return _batch_import_result(
            configured=False,
            eligible_items=len(eligible_rows),
            deferred_items=len(eligible_rows),
        )

    selected = eligible_rows[:MAX_BATCH_FEISHU_IMPORT_ITEMS]
    deferred = max(0, len(eligible_rows) - len(selected))
    result = _batch_import_result(
        configured=True,
        eligible_items=len(eligible_rows),
        deferred_items=deferred,
    )
    if not selected:
        return result

    try:
        token = await fetch_feishu_tenant_token()
    except FeishuImportError as exc:
        # Token acquisition is necessarily batch-wide.  Report every selected
        # item as failed while keeping the HTTP response safe for approval sync.
        result["attempted_items"] = len(selected)
        result["failed_items"] = len(selected)
        for item_id, sequence, _instance_code in selected[:20]:
            _append_batch_failure(
                result,
                item_id,
                f"{batch_year_month.replace('-', '')}-{sequence:04d}",
                exc,
            )
        return result

    budget = FeishuImportByteBudget(MAX_BATCH_FEISHU_IMPORT_BYTES)
    for item_id, sequence, instance_code in selected:
        voucher_no = f"{batch_year_month.replace('-', '')}-{sequence:04d}"
        result["attempted_items"] += 1
        try:
            detail = await fetch_feishu_instance_detail_with_token(
                instance_code, token
            )
        except FeishuImportError as exc:
            result["failed_items"] += 1
            _append_batch_failure(result, item_id, voucher_no, exc)
            await _touch_attempted_voucher_item(item_id, db)
            continue

        comment_rows, source_failures = await _feishu_comment_rows_for_import(
            instance_code=instance_code,
            token=token,
            detail=detail,
            user=user,
        )

        item = await db.get(VoucherItem, item_id)
        if item is None:
            await db.rollback()
            result["failed_items"] += 1
            _append_batch_failure(result, item_id, voucher_no, "凭证条目已不存在")
            continue
        try:
            item_result = await _import_item_feishu_attachments(
                item,
                detail,
                user,
                db,
                comment_rows=comment_rows,
                source_failures=source_failures,
                shared_budget=budget,
                skip_if_invoice_exists=not rescan_existing,
            )
            invoice_already_present = bool(
                item_result.pop("invoice_already_present", False)
            )
            item_result.pop("changed", None)
            # Persist this item before reading the next live instance.
            item.updated_at = datetime.now()
            await db.commit()
        except (FeishuImportError, HTTPException, OSError, SQLAlchemyError, ValueError):
            await db.rollback()
            result["failed_items"] += 1
            _append_batch_failure(result, item_id, voucher_no, "附件处理或保存失败")
            await _touch_attempted_voucher_item(item_id, db)
            continue

        item_failures = item_result["failed"]
        if (
            item_result["linked"]
            or item_result["duplicates"]
            or invoice_already_present
        ):
            result["items_with_attachments"] += 1
        elif not item_failures:
            result["items_without_attachments"] += 1
        for field in ("scanned", "imported", "linked", "duplicates", "skipped"):
            result[field] += item_result[field]
        result["failed_count"] += len(item_failures)
        if item_failures:
            result["failed_items"] += 1
            reason = item_failures[0].get("reason") or "附件导入失败"
            if len(item_failures) > 1:
                reason = f"{reason}（共 {len(item_failures)} 个附件失败）"
            _append_batch_failure(result, item_id, voucher_no, reason)

    await _refresh_batch_status(batch_pk, db)
    await db.commit()
    return result


@router.post("/batches/{batch_id}/import-feishu-attachments")
async def import_batch_feishu_attachments(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    rescan_existing: bool = False,
):
    """Import live attachments and coalesce equivalent batch requests.

    Automatic refreshes skip approvals that already have an invoice. A manual
    request can set ``rescan_existing`` so later additions to existing approvals
    are also discovered. The two modes have separate short-lived result caches.
    """
    _admin(user)
    gate = _batch_feishu_import_gate(
        batch_id,
        db,
        rescan_existing=rescan_existing,
    )
    async with gate.lock:
        batch = await _get_batch(batch_id, db)
        if batch.status == "archived":
            raise HTTPException(
                status_code=409, detail="已归档凭证批次不允许修改"
            )
        if (
            not rescan_existing
            and gate.result is not None
            and gate.completed_at is not None
            and monotonic() - gate.completed_at
            < BATCH_FEISHU_IMPORT_TTL_SECONDS
        ):
            return deepcopy(gate.result)

        result = await _run_batch_feishu_attachment_import(
            batch,
            user,
            db,
            rescan_existing=rescan_existing,
        )
        if not rescan_existing:
            gate.result = deepcopy(result)
            gate.completed_at = monotonic()
        return result


@router.post("/items/{item_id}/import-feishu-attachments")
async def import_feishu_attachments(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the live approval and archive supported Feishu files as invoices.

    The endpoint is intentionally explicit and item-scoped: it never changes
    the existing approval-sync workflow, and importing the same attachment
    repeatedly is idempotent through the document SHA and item/document link.
    """
    _admin(user)
    item = await _get_item(item_id, db)
    batch = await _get_batch(item.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    if not item.requires_voucher:
        raise HTTPException(status_code=409, detail="该审批已标记为无需装订")
    try:
        token, detail = await fetch_feishu_instance_detail(item.instance_code)
    except FeishuImportError as exc:
        status_code = 503 if "未配置" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    comment_rows, source_failures = await _feishu_comment_rows_for_import(
        instance_code=item.instance_code,
        token=token,
        detail=detail,
        user=user,
    )
    result = await _import_item_feishu_attachments(
        item,
        detail,
        user,
        db,
        comment_rows=comment_rows,
        source_failures=source_failures,
    )
    if result.pop("changed"):
        await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    return result


@router.get("/items/{item_id}/matches")
async def item_matches(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    if not item.requires_voucher:
        return []
    items = list(
        (await db.scalars(select(VoucherItem).where(VoucherItem.batch_id == item.batch_id))).all()
    )
    context = await _load_context(item.batch_id, items, db)
    candidates = []
    for document in context.pending_receipts:
        scored = score_receipt(item, document)
        candidates.append(
            {
                "id": document.id,
                "receipt": document.original_filename,
                "amount": document.amount,
                "payee_name": document.payee,
                "transaction_date": document.transaction_date,
                "bank_reference": document.bank_reference,
                "score": scored["score"],
                "reason": "；".join(
                    part["reason"] for part in scored["breakdown"].values()
                ),
                "breakdown": scored["breakdown"],
                "download_url": f"/api/v1/vouchers/documents/{document.id}/download",
            }
        )
    return sorted(candidates, key=lambda row: (-row["score"], row["id"]))


@router.patch("/items/{item_id}/requirement")
async def update_voucher_requirement(
    item_id: int,
    data: VoucherRequirementUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    batch = await _get_batch(item.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    item.requires_voucher = data.requires_voucher
    item.requirement_overridden = True
    reason = (data.reason or "").strip()
    item.voucher_exclusion_reason = None if data.requires_voucher else (reason or "人工标记无需装订")
    item.updated_at = datetime.now()
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    return {"ok": True, "item_id": item.id, "requires_voucher": item.requires_voucher}


@router.patch("/items/{item_id}/monthly-approval")
async def update_monthly_approval(
    item_id: int,
    data: VoucherMonthlyApprovalUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Choose whether this approval page appears in the merged monthly PDF."""
    _admin(user)
    item = await _get_item(item_id, db)
    batch = await _get_batch(item.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    item.include_approval_in_monthly_pdf = data.include_approval
    item.updated_at = datetime.now()
    await db.commit()
    return {
        "ok": True,
        "item_id": item.id,
        "include_approval_in_monthly_pdf": bool(
            item.include_approval_in_monthly_pdf
        ),
    }


@router.patch("/items/{item_id}/links/{document_id}/printed")
async def update_invoice_printed(
    item_id: int,
    document_id: int,
    data: VoucherInvoicePrintedUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Keep an invoice on file while excluding it from future exports."""
    _admin(user)
    item = await _get_item(item_id, db)
    batch = await _get_batch(item.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    row = (
        await db.execute(
            select(VoucherDocumentLink, VoucherDocument)
            .join(
                VoucherDocument,
                VoucherDocument.id == VoucherDocumentLink.document_id,
            )
            .where(
                VoucherDocumentLink.item_id == item.id,
                VoucherDocumentLink.document_id == document_id,
            )
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="发票关联不存在")
    link, document = row
    if link.link_type != "invoice" or document.document_type != "invoice":
        raise HTTPException(status_code=409, detail="只有发票可以标记为已打印")
    if document.batch_id != item.batch_id:
        raise HTTPException(status_code=409, detail="发票与凭证不属于同一月份")
    link.invoice_printed = data.printed
    item.updated_at = datetime.now()
    await db.commit()
    return {
        "ok": True,
        "item_id": item.id,
        "document_id": document.id,
        "printed": bool(link.invoice_printed),
    }


@router.patch("/batches/{batch_id}/invoices/printed")
async def update_batch_invoices_printed(
    batch_id: int,
    data: VoucherInvoiceBulkPrintedUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark or restore every invoice attachment in one monthly batch."""
    _admin(user)
    batch = await _get_batch(batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    rows = (
        await db.execute(
            select(VoucherDocumentLink, VoucherItem)
            .join(VoucherItem, VoucherItem.id == VoucherDocumentLink.item_id)
            .join(
                VoucherDocument,
                VoucherDocument.id == VoucherDocumentLink.document_id,
            )
            .where(
                VoucherItem.batch_id == batch.id,
                VoucherDocumentLink.link_type == "invoice",
                VoucherDocument.document_type == "invoice",
                VoucherDocument.batch_id == batch.id,
            )
        )
    ).all()
    changed_items: set[int] = set()
    updated = 0
    for link, item in rows:
        if bool(link.invoice_printed) == data.printed:
            continue
        link.invoice_printed = data.printed
        changed_items.add(item.id)
        item.updated_at = datetime.now()
        updated += 1
    await db.commit()
    return {
        "ok": True,
        "batch_id": batch.id,
        "printed": data.printed,
        "updated": updated,
        "total": len(rows),
        "affected_items": len(changed_items),
    }


@router.patch("/items/{item_id}/invoices/printed")
async def update_item_invoices_printed(
    item_id: int,
    data: VoucherInvoiceBulkPrintedUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark or restore every invoice attachment belonging to one voucher item."""
    _admin(user)
    item = await _get_item(item_id, db)
    batch = await _get_batch(item.batch_id, db)
    if batch.status == "archived":
        raise HTTPException(status_code=409, detail="已归档凭证批次不允许修改")
    links = list(
        (
            await db.scalars(
                select(VoucherDocumentLink)
                .join(
                    VoucherDocument,
                    VoucherDocument.id == VoucherDocumentLink.document_id,
                )
                .where(
                    VoucherDocumentLink.item_id == item.id,
                    VoucherDocumentLink.link_type == "invoice",
                    VoucherDocument.document_type == "invoice",
                    VoucherDocument.batch_id == batch.id,
                )
            )
        ).all()
    )
    updated = 0
    for link in links:
        if bool(link.invoice_printed) == data.printed:
            continue
        link.invoice_printed = data.printed
        updated += 1
    if updated:
        item.updated_at = datetime.now()
    await db.commit()
    return {
        "ok": True,
        "item_id": item.id,
        "printed": data.printed,
        "updated": updated,
        "total": len(links),
    }


@router.get("/items/{item_id}/source-attachments")
async def source_attachment_metadata(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    attachments = parse_source_attachment_metadata(item.raw_summaries_snapshot)
    return {
        "item_id": item.id,
        "metadata_only": True,
        "attachments": [
            {
                **row,
                "file_name": row.get("name"),
                "file_token": row.get("token"),
                "type": row.get("control_type"),
            }
            for row in attachments
        ],
        "manual_upload_required": True,
    }


async def _queue_receipt_invoice_comment_syncs(
    item: VoucherItem,
    receipt: VoucherDocument,
    user: User,
    db: AsyncSession,
) -> int:
    return 0


def _empty_feishu_comment_sync_result() -> dict[str, Any]:
    return {
        "eligible": 0,
        "synced": 0,
        "already_synced": 0,
        "failed": 0,
        "uncertain": 0,
        "processing": 0,
        "errors": [],
    }


def _delivery_active_conditions() -> tuple[Any, Any, Any]:
    """Correlated guards proving the receipt and invoice still belong to the item."""
    receipt_association = exists(
        select(VoucherReceiptInvoiceLink.id)
        .where(
            VoucherReceiptInvoiceLink.receipt_document_id
            == VoucherFeishuCommentSync.receipt_document_id,
            VoucherReceiptInvoiceLink.invoice_document_id
            == VoucherFeishuCommentSync.invoice_document_id,
            VoucherReceiptInvoiceLink.owns_invoice.is_(True),
        )
        .correlate(VoucherFeishuCommentSync)
    )
    receipt_link = exists(
        select(VoucherDocumentLink.id)
        .where(
            VoucherDocumentLink.item_id == VoucherFeishuCommentSync.item_id,
            VoucherDocumentLink.document_id
            == VoucherFeishuCommentSync.receipt_document_id,
            VoucherDocumentLink.link_type == "bank_receipt",
        )
        .correlate(VoucherFeishuCommentSync)
    )
    invoice_link = exists(
        select(VoucherDocumentLink.id)
        .where(
            VoucherDocumentLink.item_id == VoucherFeishuCommentSync.item_id,
            VoucherDocumentLink.document_id
            == VoucherFeishuCommentSync.invoice_document_id,
            VoucherDocumentLink.link_type == "invoice",
        )
        .correlate(VoucherFeishuCommentSync)
    )
    return receipt_association, receipt_link, invoice_link


async def _claim_feishu_comment_delivery(
    delivery_id: int,
    db: AsyncSession,
) -> tuple[str, str, str | None] | None:
    """Atomically lease one delivery so concurrent requests cannot both post it."""
    delivery = await db.scalar(
        select(VoucherFeishuCommentSync)
        .where(VoucherFeishuCommentSync.id == delivery_id)
        .execution_options(populate_existing=True)
    )
    if not delivery or delivery.status in {"succeeded", "cancelled"}:
        return None
    now = datetime.now()
    stale_before = now - timedelta(seconds=FEISHU_COMMENT_SEND_LEASE_SECONDS)
    prior_status = str(delivery.status)
    prior_actor = delivery.actor_open_id
    if prior_status == "sending":
        if delivery.claimed_at is not None and delivery.claimed_at > stale_before:
            return None
        state_guard = and_(
            VoucherFeishuCommentSync.status == "sending",
            or_(
                VoucherFeishuCommentSync.claimed_at.is_(None),
                VoucherFeishuCommentSync.claimed_at <= stale_before,
            ),
        )
    elif prior_status in {"pending", "failed", "uncertain"}:
        state_guard = VoucherFeishuCommentSync.status == prior_status
    else:
        return None

    claim_token = str(uuid4())
    claimed = await db.execute(
        update(VoucherFeishuCommentSync)
        .where(
            VoucherFeishuCommentSync.id == delivery_id,
            state_guard,
            *_delivery_active_conditions(),
        )
        .values(
            status="sending",
            claim_token=claim_token,
            claimed_at=now,
            attempts=VoucherFeishuCommentSync.attempts + 1,
            last_error=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    await db.commit()
    if claimed.rowcount != 1:
        return None
    return claim_token, prior_status, prior_actor


async def _finish_feishu_comment_delivery(
    delivery_id: int,
    claim_token: str,
    db: AsyncSession,
    *,
    status: str,
    last_error: str | None = None,
    actor_user_id: int | None = None,
    actor_open_id: str | None = None,
    comment_id: str | None = None,
    sent_at: datetime | None = None,
    clear_file_url: bool = False,
) -> bool:
    values: dict[str, Any] = {
        "status": status,
        "last_error": last_error,
        "claim_token": None,
        "claimed_at": None,
    }
    if actor_user_id is not None:
        values["actor_user_id"] = actor_user_id
    if actor_open_id is not None:
        values["actor_open_id"] = actor_open_id
    if comment_id is not None:
        values["feishu_comment_id"] = comment_id
    if sent_at is not None:
        values["sent_at"] = sent_at
    if clear_file_url:
        values["feishu_file_url"] = None
        values["feishu_file_uploaded_at"] = None
    finished = await db.execute(
        update(VoucherFeishuCommentSync)
        .where(
            VoucherFeishuCommentSync.id == delivery_id,
            VoucherFeishuCommentSync.claim_token == claim_token,
        )
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    await db.commit()
    return finished.rowcount == 1


async def _sync_receipt_invoice_comments(
    item_id: int,
    receipt_document_ids: list[int],
    user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    return _empty_feishu_comment_sync_result()


async def _queue_and_sync_current_item_receipt_invoices(
    item: VoucherItem,
    user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    receipt_ids = list(
        (
            await db.scalars(
                select(VoucherDocumentLink.document_id).where(
                    VoucherDocumentLink.item_id == item.id,
                    VoucherDocumentLink.link_type == "bank_receipt",
                )
            )
        ).all()
    )
    for receipt_id in receipt_ids:
        receipt = await db.get(VoucherDocument, receipt_id)
        if receipt:
            await _queue_receipt_invoice_comment_syncs(item, receipt, user, db)
    await db.commit()
    return await _sync_receipt_invoice_comments(
        item.id,
        receipt_ids,
        user,
        db,
    )


async def _link_receipt_invoices_to_item(
    item: VoucherItem,
    receipt: VoucherDocument,
    user: User,
    db: AsyncSession,
) -> None:
    invoice_rows = (await _receipt_invoice_rows([receipt.id], db)).get(
        receipt.id,
        [],
    )
    for association, invoice in invoice_rows:
        links = list(
            (
                await db.scalars(
                    select(VoucherDocumentLink).where(
                        VoucherDocumentLink.document_id == invoice.id
                    )
                )
            ).all()
        )
        if not bool(association.owns_invoice):
            if not any(
                link.item_id == item.id and link.link_type == "invoice"
                for link in links
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该回单引用了其他审批的发票；如需匹配，请先移除引用",
                )
            continue
        if invoice.batch_id != item.batch_id or invoice.document_type != "invoice":
            raise HTTPException(status_code=409, detail="回单随附发票数据异常")
        if any(link.item_id != item.id for link in links):
            raise HTTPException(status_code=409, detail="回单随附发票已关联其他凭证")
        if not any(
            link.item_id == item.id and link.link_type == "invoice"
            for link in links
        ):
            db.add(
                VoucherDocumentLink(
                    item_id=item.id,
                    document_id=invoice.id,
                    link_type="invoice",
                    confirmed_by=user.id,
                )
            )


async def _cancel_unsent_receipt_comment_syncs(
    item_id: int,
    receipt_document_ids: list[int],
    db: AsyncSession,
) -> int:
    """Cancel queued work before unlinking, while protecting active deliveries."""
    receipt_ids = sorted({int(value) for value in receipt_document_ids})
    if not receipt_ids:
        return 0
    stale_before = datetime.now() - timedelta(
        seconds=FEISHU_COMMENT_SEND_LEASE_SECONDS
    )
    cancelled = await db.execute(
        update(VoucherFeishuCommentSync)
        .where(
            VoucherFeishuCommentSync.item_id == item_id,
            VoucherFeishuCommentSync.receipt_document_id.in_(receipt_ids),
            VoucherFeishuCommentSync.status.in_(
                ["pending", "failed", "cancelled"]
            ),
        )
        .values(
            status="cancelled",
            claim_token=None,
            claimed_at=None,
            last_error="本地回单匹配已解除，未继续同步飞书评论",
        )
        .execution_options(synchronize_session="fetch")
    )
    # An uncertain create, or a stale worker that may have reached Feishu,
    # must retain its ambiguity. If the receipt is linked again, retry logic
    # will reconcile the event marker before it considers another POST.
    ambiguous = await db.execute(
        update(VoucherFeishuCommentSync)
        .where(
            VoucherFeishuCommentSync.item_id == item_id,
            VoucherFeishuCommentSync.receipt_document_id.in_(receipt_ids),
            or_(
                VoucherFeishuCommentSync.status == "uncertain",
                and_(
                    VoucherFeishuCommentSync.status == "sending",
                    or_(
                        VoucherFeishuCommentSync.claimed_at.is_(None),
                        VoucherFeishuCommentSync.claimed_at <= stale_before,
                    ),
                ),
            ),
        )
        .values(
            status="uncertain",
            claim_token=None,
            claimed_at=None,
            last_error="本地回单匹配已解除；飞书发送结果待核对，重新匹配后会先查重",
        )
        .execution_options(synchronize_session="fetch")
    )
    active = await db.scalar(
        select(func.count())
        .select_from(VoucherFeishuCommentSync)
        .where(
            VoucherFeishuCommentSync.item_id == item_id,
            VoucherFeishuCommentSync.receipt_document_id.in_(receipt_ids),
            VoucherFeishuCommentSync.status == "sending",
            VoucherFeishuCommentSync.claimed_at > stale_before,
        )
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail="发票正在同步飞书评论，请稍后再解除回单匹配",
        )
    return int(cancelled.rowcount or 0) + int(ambiguous.rowcount or 0)


async def _unlink_receipt_invoices_from_item(
    item_id: int,
    receipt_document_ids: list[int],
    db: AsyncSession,
) -> int:
    if not receipt_document_ids:
        return 0
    await _cancel_unsent_receipt_comment_syncs(
        item_id,
        receipt_document_ids,
        db,
    )
    invoice_ids = list(
        (
            await db.scalars(
                select(VoucherReceiptInvoiceLink.invoice_document_id).where(
                    VoucherReceiptInvoiceLink.receipt_document_id.in_(
                        receipt_document_ids
                    ),
                    VoucherReceiptInvoiceLink.owns_invoice.is_(True),
                )
            )
        ).all()
    )
    if not invoice_ids:
        return 0
    links = list(
        (
            await db.scalars(
                select(VoucherDocumentLink).where(
                    VoucherDocumentLink.item_id == item_id,
                    VoucherDocumentLink.document_id.in_(invoice_ids),
                    VoucherDocumentLink.link_type == "invoice",
                )
            )
        ).all()
    )
    for link in links:
        await db.delete(link)
    return len(links)


async def _confirm_document(
    item: VoucherItem,
    document: VoucherDocument,
    user: User,
    db: AsyncSession,
) -> VoucherDocumentLink:
    if document.document_type != "bank_receipt":
        raise HTTPException(status_code=404, detail="回单候选不存在")
    if (
        document.receipt_direction == "income"
        or document.receipt_category in {"income", "other"}
    ):
        raise HTTPException(status_code=409, detail="收入或其他回单不能匹配费用审批")
    if document.batch_id != item.batch_id:
        raise HTTPException(status_code=409, detail="不能关联其他月份批次的银行回单")
    current_link = await db.scalar(
        select(VoucherDocumentLink).where(
            VoucherDocumentLink.item_id == item.id,
            VoucherDocumentLink.document_id == document.id,
            VoucherDocumentLink.link_type == "bank_receipt",
        )
    )
    if current_link:
        await _queue_receipt_invoice_comment_syncs(item, document, user, db)
        return current_link
    other_link = await db.scalar(
        select(VoucherDocumentLink).where(
            VoucherDocumentLink.document_id == document.id,
            VoucherDocumentLink.link_type == "bank_receipt",
        )
    )
    if other_link:
        raise HTTPException(status_code=409, detail="该回单已关联其他凭证条目")
    await _link_receipt_invoices_to_item(item, document, user, db)
    scored = score_receipt(item, document)
    link = VoucherDocumentLink(
        item_id=item.id,
        document_id=document.id,
        link_type="bank_receipt",
        match_score=scored["score"],
        confirmed_by=user.id,
    )
    db.add(link)
    item.updated_at = func.now()
    try:
        await db.flush()
        await _queue_receipt_invoice_comment_syncs(item, document, user, db)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="回单已被其他凭证占用，请刷新后重试") from exc
    return link


@router.post("/items/{item_id}/matches/{candidate_id}/confirm")
async def confirm_match(
    item_id: int,
    candidate_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await db.get(VoucherItem, item_id)
    document = await db.get(VoucherDocument, candidate_id)
    if not item or not document:
        raise HTTPException(status_code=404, detail="凭证条目或回单候选不存在")
    link = await _confirm_document(item, document, user, db)
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    await db.refresh(link)
    feishu_comment_sync = await _sync_receipt_invoice_comments(
        item.id,
        [document.id],
        user,
        db,
    )
    return {
        "ok": True,
        "link_id": link.id,
        "item_id": item.id,
        "document_id": document.id,
        "feishu_comment_sync": feishu_comment_sync,
    }


@router.delete("/items/{item_id}/match")
async def unlink_match(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    links = list(
        (
            await db.scalars(
                select(VoucherDocumentLink).where(
                    VoucherDocumentLink.item_id == item.id,
                    VoucherDocumentLink.link_type == "bank_receipt",
                )
            )
        ).all()
    )
    await _unlink_receipt_invoices_from_item(
        item.id,
        [link.document_id for link in links],
        db,
    )
    for link in links:
        await db.delete(link)
    await db.flush()
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    return {"ok": True, "removed": len(links)}


@router.post("/items/{item_id}/receipt-invoices/sync-feishu-comments")
async def sync_item_receipt_invoice_comments(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retry or backfill comments for invoices uploaded with matched receipts."""
    _admin(user)
    item = await _get_item(item_id, db)
    return await _queue_and_sync_current_item_receipt_invoices(item, user, db)


@router.delete("/items/{item_id}/links/{document_id}")
async def delete_link(
    item_id: int,
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    link = await db.scalar(
        select(VoucherDocumentLink).where(
            VoucherDocumentLink.item_id == item_id,
            VoucherDocumentLink.document_id == document_id,
        )
    )
    if not link:
        raise HTTPException(status_code=404, detail="附件关联不存在")
    if link.link_type == "bank_receipt":
        await _unlink_receipt_invoices_from_item(
            item.id,
            [link.document_id],
            db,
        )
    elif link.link_type == "invoice":
        receipt_association = await db.scalar(
            select(VoucherReceiptInvoiceLink.id)
            .join(
                VoucherDocumentLink,
                VoucherDocumentLink.document_id
                == VoucherReceiptInvoiceLink.receipt_document_id,
            )
            .where(
                VoucherReceiptInvoiceLink.invoice_document_id == document_id,
                VoucherDocumentLink.item_id == item.id,
                VoucherDocumentLink.link_type == "bank_receipt",
            )
            .limit(1)
        )
        if receipt_association:
            raise HTTPException(
                status_code=409,
                detail="随回单保存的发票不能单独移除，请先解除对应回单匹配",
            )
    await db.delete(link)
    await db.flush()
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    return {"ok": True}


@router.post("/items/{item_id}/links")
async def create_link(
    item_id: int,
    data: VoucherLinkCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    if data.link_type != "bank_receipt":
        raise HTTPException(status_code=422, detail="仅支持人工确认银行回单")
    item = await _get_item(item_id, db)
    document = await db.get(VoucherDocument, data.document_id)
    if not document:
        raise HTTPException(status_code=404, detail="回单候选不存在")
    link = await _confirm_document(item, document, user, db)
    await _refresh_batch_status(item.batch_id, db)
    await db.commit()
    await db.refresh(link)
    feishu_comment_sync = await _sync_receipt_invoice_comments(
        item.id,
        [document.id],
        user,
        db,
    )
    return {
        "ok": True,
        "link_id": link.id,
        "item_id": item.id,
        "document_id": document.id,
        "feishu_comment_sync": feishu_comment_sync,
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    document = await db.get(VoucherDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="附件不存在")
    linked = await db.scalar(
        select(func.count()).select_from(VoucherDocumentLink).where(
            VoucherDocumentLink.document_id == document.id
        )
    )
    if linked:
        raise HTTPException(status_code=409, detail="附件已关联凭证，请先解除关联")
    parent_association = await db.scalar(
        select(VoucherReceiptInvoiceLink.id)
        .where(VoucherReceiptInvoiceLink.invoice_document_id == document.id)
        .limit(1)
    )
    if parent_association:
        raise HTTPException(status_code=409, detail="请从对应回单中移除这份随附发票")
    receipt_associations = list(
        (
            await db.scalars(
                select(VoucherReceiptInvoiceLink).where(
                    VoucherReceiptInvoiceLink.receipt_document_id == document.id
                )
            )
        ).all()
    )
    companion_ids = [
        row.invoice_document_id
        for row in receipt_associations
        if bool(row.owns_invoice)
    ]
    companion_invoices = (
        list(
            (
                await db.scalars(
                    select(VoucherDocument).where(
                        VoucherDocument.id.in_(companion_ids)
                    )
                )
            ).all()
        )
        if companion_ids
        else []
    )
    if companion_ids:
        companion_linked = await db.scalar(
            select(func.count())
            .select_from(VoucherDocumentLink)
            .where(VoucherDocumentLink.document_id.in_(companion_ids))
        )
        if companion_linked:
            raise HTTPException(status_code=409, detail="随附发票已关联凭证，请先解除关联")
    root = Path(settings.VOUCHER_FILE_ROOT)
    paths: list[Path] = []
    for stored_document in [document, *companion_invoices]:
        try:
            paths.append(safe_resolve(root, stored_document.storage_path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    sync_invoice_ids = [document.id, *companion_ids]
    await db.execute(
        delete(VoucherFeishuCommentSync).where(
            or_(
                VoucherFeishuCommentSync.receipt_document_id == document.id,
                VoucherFeishuCommentSync.invoice_document_id.in_(sync_invoice_ids),
            )
        )
    )
    for association in receipt_associations:
        await db.delete(association)
    for companion_invoice in companion_invoices:
        await db.delete(companion_invoice)
    await db.delete(document)
    await db.commit()
    for path in paths:
        await run_in_threadpool(path.unlink, True)
    return {
        "ok": True,
        "deleted_invoices": len(companion_invoices),
        "removed_invoice_references": sum(
            not bool(association.owns_invoice)
            for association in receipt_associations
        ),
    }


def _include_document_in_export(link: VoucherDocumentLink) -> bool:
    return not (link.link_type == "invoice" and bool(link.invoice_printed))


async def _item_documents(
    item_id: int, db: AsyncSession
) -> list[tuple[VoucherDocument, Path]]:
    rows = list(
        (
            await db.execute(
                select(VoucherDocument, VoucherDocumentLink)
                .join(
                    VoucherDocumentLink,
                    VoucherDocumentLink.document_id == VoucherDocument.id,
                )
                .where(VoucherDocumentLink.item_id == item_id)
                .order_by(VoucherDocumentLink.id)
            )
        ).all()
    )
    printable_rows = [row for row in rows if _include_document_in_export(row[1])]
    return (await run_in_threadpool(_resolve_document_rows, printable_rows)).get(
        item_id, []
    )


async def _feishu_print_names(db: AsyncSession) -> dict[str, str]:
    rows = (
        await db.execute(
            select(User.feishu_open_id, User.display_name).where(
                User.feishu_open_id.is_not(None)
            )
        )
    ).all()
    return {
        str(feishu_id): str(display_name)
        for feishu_id, display_name in rows
        if feishu_id and display_name
    }


async def _live_approval_print_details(
    items: list[VoucherItem],
) -> tuple[
    dict[int, dict[str, Any]],
    dict[str, str],
    dict[int, list[dict[str, Any]]],
]:
    return {}, {}, {}


def _resolve_document_rows(
    rows: list[tuple[VoucherDocument, VoucherDocumentLink]],
) -> dict[int, list[tuple[VoucherDocument, Path]]]:
    """Order and resolve a preloaded document set without doing database work."""
    rows.sort(
        key=lambda row: (
            row[1].item_id,
            1
            if row[1].link_type == "invoice"
            else 2
            if row[1].link_type == "bank_receipt"
            else 9,
            row[1].id,
        )
    )
    grouped: dict[int, list[tuple[VoucherDocument, Path]]] = defaultdict(list)
    root = Path(settings.VOUCHER_FILE_ROOT)
    for document, link in rows:
        try:
            path = safe_resolve(root, document.storage_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(
                status_code=409, detail=f"源文件缺失：{document.original_filename}"
            )
        grouped[link.item_id].append((document, path))
    return grouped


async def _record_export(
    content: bytes,
    export_type: str,
    filename: str,
    user: User,
    db: AsyncSession,
    batch_id: int | None = None,
    item_id: int | None = None,
) -> VoucherExport:
    export_uuid = str(uuid4())
    suffix = Path(filename).suffix.lower() or ".bin"
    relative = f"exports/{export_uuid[:2]}/{export_uuid}{suffix}"
    target = safe_resolve(Path(settings.VOUCHER_FILE_ROOT), relative)

    def write_file() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    await run_in_threadpool(write_file)
    digest = await run_in_threadpool(sha256_bytes, content)
    record = VoucherExport(
        export_type=export_type,
        batch_id=batch_id,
        item_id=item_id,
        uuid=export_uuid,
        filename=filename,
        storage_path=relative,
        sha256=digest,
        size_bytes=len(content),
        created_by=user.id,
    )
    db.add(record)
    await db.flush()
    return record


def _download_response(
    content: bytes,
    filename: str,
    media_type: str,
    export: VoucherExport,
    status: dict[str, Any],
) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Voucher-Export-Id": str(export.id),
            "X-Voucher-Complete": str(status.get("complete", False)).lower(),
            "X-Voucher-Missing-Count": str(len(status.get("missing", []))),
        },
    )


@router.get("/items/{item_id}/completeness")
async def item_completeness(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    await _get_item(item_id, db)
    link_types = list(
        (
            await db.scalars(
                select(VoucherDocumentLink.link_type).where(
                    VoucherDocumentLink.item_id == item_id
                )
            )
        ).all()
    )
    return completeness(link_types)


@router.get("/items/{item_id}/pdf")
async def export_item_pdf(
    item_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    item = await _get_item(item_id, db)
    if not item.requires_voucher:
        raise HTTPException(status_code=409, detail="该审批已标记为无需装订")
    link_types = list(
        (
            await db.scalars(
                select(VoucherDocumentLink.link_type).where(
                    VoucherDocumentLink.item_id == item_id
                )
            )
        ).all()
    )
    documents = await _item_documents(item_id, db)
    status = completeness(link_types)
    print_names = await _feishu_print_names(db)
    # Do not hold a SQLite read transaction during live Feishu network calls.
    await db.commit()
    print_details, live_names, print_comments = await _live_approval_print_details(
        [item]
    )
    print_names.update(live_names)
    try:
        content = await run_in_threadpool(
            build_item_pdf,
            item,
            documents,
            user.display_name,
            print_details.get(item.id),
            print_names,
            approval_comments=print_comments.get(item.id),
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    filename = f"凭证-{item.instance_code}.pdf"
    export = await _record_export(
        content, "item_pdf", filename, user, db, item.batch_id, item.id
    )
    await db.commit()
    await db.refresh(export)
    return _download_response(content, filename, "application/pdf", export, status)


async def _batch_export_parts(
    batch: VoucherBatch, db: AsyncSession
) -> tuple[list[tuple[VoucherItem, list[tuple[VoucherDocument, Path]]]], dict[str, Any]]:
    items = list(
        (
            await db.scalars(
                select(VoucherItem)
                .where(VoucherItem.batch_id == batch.id)
                .where(VoucherItem.requires_voucher.is_(True))
                .order_by(VoucherItem.sequence, VoucherItem.id)
            )
        ).all()
    )
    if not items:
        raise HTTPException(status_code=409, detail="批次中没有需要装订的凭证条目")
    rows = list(
        (
            await db.execute(
                select(VoucherDocument, VoucherDocumentLink)
                .join(
                    VoucherDocumentLink,
                    VoucherDocumentLink.document_id == VoucherDocument.id,
                )
                .where(VoucherDocumentLink.item_id.in_([item.id for item in items]))
                .order_by(VoucherDocumentLink.item_id, VoucherDocumentLink.id)
            )
        ).all()
    )
    link_types_by_item: dict[int, list[str]] = defaultdict(list)
    printable_rows: list[tuple[VoucherDocument, VoucherDocumentLink]] = []
    for document, link in rows:
        link_types_by_item[link.item_id].append(link.link_type)
        if _include_document_in_export(link):
            printable_rows.append((document, link))
    documents_by_item = await run_in_threadpool(
        _resolve_document_rows, printable_rows
    )
    parts: list[tuple[VoucherItem, list[tuple[VoucherDocument, Path]]]] = []
    missing_items: list[dict[str, Any]] = []
    for item in items:
        documents = documents_by_item.get(item.id, [])
        status = completeness(link_types_by_item.get(item.id, []))
        if not status["complete"]:
            missing_items.append(
                {"item_id": item.id, "instance_code": item.instance_code, "missing": status["missing"]}
            )
        parts.append((item, documents))
    return parts, {"complete": not missing_items, "missing": missing_items}


@router.get("/batches/{batch_id}/pdf")
async def export_batch_pdf(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    parts, status = await _batch_export_parts(batch, db)
    skipped_item_ids = {
        item.id
        for item, documents in parts
        if not item.include_approval_in_monthly_pdf and not documents
    }
    monthly_parts = [
        (item, documents)
        for item, documents in parts
        if item.id not in skipped_item_ids
    ]
    if not monthly_parts:
        raise HTTPException(status_code=409, detail="本月没有可导出的审批页或附件")
    monthly_missing = [
        row for row in status["missing"] if row["item_id"] not in skipped_item_ids
    ]
    monthly_status = {
        "complete": not monthly_missing,
        "missing": monthly_missing,
    }
    print_names = await _feishu_print_names(db)
    await db.commit()
    print_details, live_names, print_comments = await _live_approval_print_details(
        [
            item
            for item, _documents in monthly_parts
            if item.include_approval_in_monthly_pdf
        ]
    )
    print_names.update(live_names)
    try:
        item_pdfs = [
            await run_in_threadpool(
                build_item_pdf,
                item,
                documents,
                user.display_name,
                print_details.get(item.id),
                print_names,
                approval_comments=print_comments.get(item.id),
                include_approval=bool(
                    item.include_approval_in_monthly_pdf
                ),
                pair_single_invoice_receipt=True,
            )
            for item, documents in monthly_parts
        ]
        content = await run_in_threadpool(merge_monthly_print_pdfs, item_pdfs)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    filename = f"凭证装订-{batch.year_month}.pdf"
    export = await _record_export(
        content, "batch_pdf", filename, user, db, batch_id=batch.id
    )
    await db.commit()
    await db.refresh(export)
    return _download_response(
        content, filename, "application/pdf", export, monthly_status
    )


@router.get("/batches/{batch_id}/zip")
async def export_batch_zip(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    batch = await _get_batch(batch_id, db)
    parts, status = await _batch_export_parts(batch, db)
    print_names = await _feishu_print_names(db)
    await db.commit()
    print_details, live_names, print_comments = await _live_approval_print_details(
        [item for item, _documents in parts]
    )
    print_names.update(live_names)
    try:
        rendered = [
            (
                item,
                await run_in_threadpool(
                    build_item_pdf,
                    item,
                    documents,
                    user.display_name,
                    print_details.get(item.id),
                    print_names,
                    approval_comments=print_comments.get(item.id),
                ),
            )
            for item, documents in parts
        ]
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def build_zip() -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, (item, item_content) in enumerate(rendered, 1):
                safe_code = "".join(
                    ch if ch.isalnum() or ch in "-_" else "_"
                    for ch in (item.instance_code or str(item.id))
                )
                archive.writestr(f"{index:04d}-{safe_code}.pdf", item_content)
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "year_month": batch.year_month,
                        "item_count": len(rendered),
                        "complete": status["complete"],
                        "missing_items": status["missing"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return output.getvalue()

    content = await run_in_threadpool(build_zip)
    filename = f"凭证装订-{batch.year_month}.zip"
    export = await _record_export(
        content, "batch_zip", filename, user, db, batch_id=batch.id
    )
    await db.commit()
    await db.refresh(export)
    return _download_response(content, filename, "application/zip", export, status)


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    export = await db.get(VoucherExport, export_id)
    if not export:
        raise HTTPException(status_code=404, detail="导出文件不存在")
    try:
        path = safe_resolve(Path(settings.VOUCHER_FILE_ROOT), export.storage_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=400, detail="导出文件已不存在")
    media_type = "application/zip" if path.suffix.lower() == ".zip" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=export.filename)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    document = await db.get(VoucherDocument, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        path = safe_resolve(Path(settings.VOUCHER_FILE_ROOT), document.storage_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=400, detail="附件源文件已不存在")
    return FileResponse(path, media_type=document.mime_type, filename=document.original_filename)

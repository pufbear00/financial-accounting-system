from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

import logging
from datetime import date

import httpx

from app.database import get_db
from app.models.approval import Approval
from app.models.airwallex import AirwallexTransaction
from app.models.comment import ApprovalComment
from app.models.category import ExpenseCategory
from app.models.user import User
from app.schemas.approval import ApprovalResponse, ApprovalListResponse, ApprovalCategoryUpdate
from app.auth import get_allowed_categories, get_current_user

logger = logging.getLogger(__name__)

# 汇率缓存: {(currency, "YYYY-MM"): rate}
_rate_cache: dict[tuple[str, str], float] = {}


async def _get_exchange_rate(currency: str, year_month: str) -> float:
    if currency == "CNY":
        return 1.0
    raise HTTPException(status_code=403, detail="离线版不查询真实汇率，不能自动折算外币")


router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    year_month: str | None = None,
    category_id: int | None = None,
    status: int | None = None,
    applicant_name: str | None = None,
    definition_name: str | None = None,
    has_feedback: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    q = select(Approval)
    count_q = select(func.count()).select_from(Approval)
    sum_q = select(func.coalesce(func.sum(func.coalesce(Approval.amount_cny, Approval.amount)), 0)).select_from(Approval)

    filters = []
    if allowed is not None:
        filters.append(Approval.category_id.in_(allowed))
    if year_month:
        filters.append(func.substr(func.coalesce(Approval.submit_date, Approval.expense_date), 1, 7) == year_month)
    if category_id is not None:
        if category_id == 0:
            filters.append(Approval.category_id.is_(None))
        else:
            filters.append(Approval.category_id == category_id)
    if status is not None:
        filters.append(Approval.status == status)
    else:
        # 默认排除已撤销(4)和已拒绝(3)
        filters.append(Approval.status.in_([1, 2]))
    if applicant_name:
        filters.append(Approval.applicant_name.contains(applicant_name))
    if definition_name:
        filters.append(Approval.definition_name == definition_name)
    if has_feedback is True:
        sub = select(ApprovalComment.approval_id).where(ApprovalComment.resolved == 0).distinct()
        filters.append(Approval.id.in_(sub))

    for f in filters:
        q = q.where(f)
        count_q = count_q.where(f)
        sum_q = sum_q.where(f)

    total = await db.scalar(count_q) or 0
    sum_amount = await db.scalar(sum_q) or 0.0

    q = q.order_by(func.coalesce(Approval.submit_date, Approval.expense_date).desc(), Approval.id.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)

    return ApprovalListResponse(
        items=result.scalars().all(),
        total=total,
        page=page,
        page_size=page_size,
        sum_amount=round(sum_amount, 2),
    )


@router.put("/{approval_id}/category", response_model=ApprovalResponse)
async def update_approval_category(
    approval_id: int,
    data: ApprovalCategoryUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改分类")
    approval = await db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    approval.category_id = data.category_id
    await db.commit()
    await db.refresh(approval)
    return approval


class CurrencyUpdate(BaseModel):
    currency: str  # "CNY" / "USD" 等


@router.put("/{approval_id}/currency", response_model=ApprovalResponse)
async def update_approval_currency(
    approval_id: int,
    data: CurrencyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改")
    approval = await db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    approval.currency = data.currency
    if data.currency != "CNY" and approval.amount and approval.expense_date:
        ym = str(approval.expense_date)[:7]
        rate = await _get_exchange_rate(data.currency, ym)
        approval.exchange_rate = round(rate, 4)
        approval.amount_cny = round(approval.amount * rate, 2)
    elif data.currency == "CNY":
        approval.exchange_rate = 1.0
        approval.amount_cny = approval.amount
    await db.commit()
    await db.refresh(approval)
    return approval


@router.post("/detect-currency")
async def detect_currency(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI 检测描述中提到 USD 的记录，自动标记并折算。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    import re
    # 找出 CNY 记录中描述提到 USD/美金/美元 的
    rows = (await db.execute(
        select(Approval).where(Approval.currency == "CNY")
    )).scalars().all()

    updated = []
    skipped = []
    for a in rows:
        desc = (a.description or "").lower()
        if not any(kw in desc for kw in ["usd", "美金", "美元", "美刀"]):
            continue
        # 判断：如果金额是整数且描述中"总计XXusd"或"费用XXusd"，且金额和描述中的数字匹配 → 是USD
        # 但如果描述含"换算人民币"、"折算"、"人民币支付" → 已经是CNY了
        if any(kw in desc for kw in ["换算人民币", "折算", "人民币支付", "人民币1:", "按照7换算"]):
            skipped.append({"id": a.id, "amount": a.amount, "reason": "已折算为CNY", "desc": (a.description or "")[:60]})
            continue
        # 大金额（>5000）且描述里没有明确说是USD原币 → 可能已折算，跳过
        amt_str = str(int(a.amount)) if a.amount else ""
        if a.amount and a.amount > 5000 and amt_str not in desc:
            skipped.append({"id": a.id, "amount": a.amount, "reason": "大额可能已折算", "desc": (a.description or "")[:60]})
            continue
        # 标记为 USD
        if a.expense_date:
            ym = str(a.expense_date)[:7]
            rate = await _get_exchange_rate("USD", ym)
            a.currency = "USD"
            a.exchange_rate = round(rate, 4)
            a.amount_cny = round(a.amount * rate, 2) if a.amount else None
            updated.append({"id": a.id, "amount": a.amount, "amount_cny": a.amount_cny, "desc": (a.description or "")[:60]})

    await db.commit()
    return {"updated": updated, "skipped": skipped}


# --- 反馈/评论 ---

class CommentCreate(BaseModel):
    content: str


class CommentResponse(BaseModel):
    id: int
    approval_id: int
    user_id: int
    user_name: str
    content: str
    resolved: int
    created_at: str

    model_config = {"from_attributes": True}


@router.get("/{approval_id}/comments", response_model=list[CommentResponse])
async def list_comments(
    approval_id: int,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApprovalComment)
        .where(ApprovalComment.approval_id == approval_id)
        .order_by(ApprovalComment.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{approval_id}/comments", response_model=CommentResponse)
async def add_comment(
    approval_id: int,
    data: CommentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    approval = await db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批记录不存在")
    comment = ApprovalComment(
        approval_id=approval_id,
        user_id=user.id,
        user_name=user.display_name,
        content=data.content.strip(),
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


@router.put("/comments/{comment_id}/resolve")
async def resolve_comment(
    comment_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可标记已处理")
    comment = await db.get(ApprovalComment, comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="评论不存在")
    comment.resolved = 1
    await db.commit()
    return {"ok": True}


# 获取未处理反馈总数（admin 用）
@router.get("/feedback/pending-count")
async def pending_feedback_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await db.scalar(
        select(func.count()).select_from(ApprovalComment).where(ApprovalComment.resolved == 0)
    ) or 0
    return {"count": count}


# --- Airwallex 交易列表 ---

class AirwallexItem(BaseModel):
    id: int
    transaction_id: str
    card_nickname: str | None
    merchant_name: str | None
    merchant_category: str | None
    amount: float | None
    currency: str | None
    exchange_rate: float | None
    amount_cny: float | None
    status: str | None
    transaction_date: str | None
    category_id: int | None
    category_name: str | None

    model_config = {"from_attributes": True}


class AirwallexListResponse(BaseModel):
    items: list[AirwallexItem]
    total: int
    page: int
    page_size: int
    sum_amount: float
    sum_amount_cny: float


@router.get("/airwallex", response_model=AirwallexListResponse)
async def list_airwallex(
    year_month: str | None = None,
    category_id: int | None = None,
    merchant_name: str | None = None,
    card_nickname: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    q = select(AirwallexTransaction, func.coalesce(ExpenseCategory.name, "未分类").label("cat_name")).outerjoin(
        ExpenseCategory, AirwallexTransaction.category_id == ExpenseCategory.id)
    count_q = select(func.count()).select_from(AirwallexTransaction)
    sum_q = select(func.coalesce(func.sum(AirwallexTransaction.amount), 0)).select_from(AirwallexTransaction)
    sum_cny_q = select(func.coalesce(func.sum(func.coalesce(AirwallexTransaction.amount_cny, AirwallexTransaction.amount)), 0)).select_from(AirwallexTransaction)

    filters = []
    if allowed is not None:
        filters.append(AirwallexTransaction.category_id.in_(allowed))
    if year_month:
        filters.append(func.substr(AirwallexTransaction.transaction_date, 1, 7) == year_month)
    if category_id is not None:
        if category_id == 0:
            filters.append(AirwallexTransaction.category_id.is_(None))
        else:
            filters.append(AirwallexTransaction.category_id == category_id)
    if merchant_name:
        filters.append(AirwallexTransaction.merchant_name.contains(merchant_name))
    if card_nickname:
        filters.append(AirwallexTransaction.card_nickname.contains(card_nickname))

    for f in filters:
        q = q.where(f)
        count_q = count_q.where(f)
        sum_q = sum_q.where(f)
        sum_cny_q = sum_cny_q.where(f)

    total = await db.scalar(count_q) or 0
    sum_amount = await db.scalar(sum_q) or 0.0
    # 折合人民币合计：直接用入库时按交易日真实汇率算好的 amount_cny
    sum_amount_cny = round(await db.scalar(sum_cny_q) or 0.0, 2)

    q = q.order_by(AirwallexTransaction.transaction_date.desc(), AirwallexTransaction.id.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).all()

    items = []
    for tx, cat_name in rows:
        # 优先用存储的折合值；老数据未回填时退回上月末汇率临时折算
        if tx.amount_cny is not None:
            rate, amt_cny = tx.exchange_rate, tx.amount_cny
        else:
            tx_ym = str(tx.transaction_date)[:7] if tx.transaction_date else (year_month or "2026-04")
            rate = round(await _get_exchange_rate(tx.currency or "USD", tx_ym), 4)
            amt_cny = round((tx.amount or 0) * rate, 2)
        items.append(AirwallexItem(
            id=tx.id, transaction_id=tx.transaction_id,
            card_nickname=tx.card_nickname, merchant_name=tx.merchant_name,
            merchant_category=tx.merchant_category, amount=tx.amount,
            currency=tx.currency, exchange_rate=rate,
            amount_cny=amt_cny, status=tx.status,
            transaction_date=str(tx.transaction_date) if tx.transaction_date else None,
            category_id=tx.category_id, category_name=cat_name,
        ))

    return AirwallexListResponse(
        items=items, total=total, page=page, page_size=page_size,
        sum_amount=round(sum_amount, 2), sum_amount_cny=sum_amount_cny,
    )


@router.put("/airwallex/{tx_id}/category")
async def update_airwallex_category(
    tx_id: int,
    data: ApprovalCategoryUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改分类")
    tx = await db.get(AirwallexTransaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    tx.category_id = data.category_id
    await db.commit()
    return {"ok": True}

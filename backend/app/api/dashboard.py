from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.approval import Approval
from app.models.airwallex import AirwallexTransaction
from app.models.budget import Budget
from app.models.category import ExpenseCategory
from app.schemas.dashboard import MonthlySummary, CategorySummary, TrendPoint, BudgetAlert, SourceBreakdown
from app.models.airwallex import AirwallexTransaction
from app.auth import get_allowed_categories, get_current_user
from app.models.user import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _ym(col):
    """提取日期列的 YYYY-MM（兼容字符串和 date 类型）。"""
    return func.substr(col, 1, 7)


# 审批的记账归属日：飞书提交日期优先，缺失回退表单付款日期
ACCT_DATE = func.coalesce(Approval.submit_date, Approval.expense_date)


@router.get("/summary", response_model=MonthlySummary)
async def monthly_summary(
    year_month: str = Query(...),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    date_filter = _ym(ACCT_DATE) == year_month
    # 待审批(1) + 已通过(2) 都计入实际支出
    perm_filters = [date_filter, Approval.status.in_([1, 2])]
    if allowed is not None:
        perm_filters.append(Approval.category_id.in_(allowed))

    # 用 amount_cny（折合人民币），没有则回退 amount
    total_amount = await db.scalar(
        select(func.coalesce(func.sum(func.coalesce(Approval.amount_cny, Approval.amount)), 0)).where(*perm_filters)
    ) or 0.0
    approved_count = await db.scalar(
        select(func.count()).select_from(Approval).where(date_filter, Approval.status == 2,
            *([Approval.category_id.in_(allowed)] if allowed is not None else []))
    ) or 0
    pending_count = await db.scalar(
        select(func.count()).select_from(Approval).where(date_filter, Approval.status == 1,
            *([Approval.category_id.in_(allowed)] if allowed is not None else []))
    ) or 0

    # 审批按分类汇总
    cat_rows = (await db.execute(
        select(Approval.category_id, func.coalesce(ExpenseCategory.name, "未分类"),
               func.sum(func.coalesce(Approval.amount_cny, Approval.amount)), func.count())
        .outerjoin(ExpenseCategory, Approval.category_id == ExpenseCategory.id)
        .where(*perm_filters).group_by(Approval.category_id)
    )).all()

    # Airwallex 按分类汇总
    awx_filters = [_ym(AirwallexTransaction.transaction_date) == year_month]
    if allowed is not None:
        awx_filters.append(AirwallexTransaction.category_id.in_(allowed))
    awx_rows = (await db.execute(
        select(AirwallexTransaction.category_id, func.coalesce(ExpenseCategory.name, "Airwallex-未分类"),
               # 支出存负数 → 取反为正（退款为正数自然抵扣）；折合人民币优先
               # 负号聚合必须 .label()：未命名 UnaryExpression 会让 SQLAlchemy 结果列
               # 映射 NoSuchColumnError（生产 2026-07 三个 dashboard 接口 500 的根因）
               (-func.sum(func.coalesce(AirwallexTransaction.amount_cny, AirwallexTransaction.amount))).label("total"),
               func.count())
        .outerjoin(ExpenseCategory, AirwallexTransaction.category_id == ExpenseCategory.id)
        .where(*awx_filters).group_by(AirwallexTransaction.category_id)
    )).all()

    # 合并
    merged: dict[int | None, dict] = {}
    for cat_id, cat_name, amount, count in cat_rows:
        merged[cat_id] = {"name": cat_name, "amount": round(amount or 0, 2), "count": count}
    for cat_id, cat_name, amount, count in awx_rows:
        if cat_id in merged:
            merged[cat_id]["amount"] = round(merged[cat_id]["amount"] + (amount or 0), 2)
            merged[cat_id]["count"] += count
        else:
            merged[cat_id] = {"name": cat_name, "amount": round(amount or 0, 2), "count": count}

    awx_total = sum(m["amount"] for m in merged.values()) - sum(round(a or 0, 2) for _, _, a, _ in cat_rows)
    total_amount = round(total_amount + awx_total, 2)
    # 预算
    budget_q = select(Budget).where(Budget.year_month == year_month)
    if allowed is not None:
        budget_q = budget_q.where(Budget.category_id.in_(allowed))
    budget_map = {b.category_id: b.amount for b in (await db.execute(budget_q)).scalars()}
    total_budget = sum(budget_map.values())

    by_category = []
    for cat_id, info in merged.items():
        budget_amt = budget_map.get(cat_id)
        by_category.append(CategorySummary(
            category_id=cat_id, category_name=info["name"],
            amount=info["amount"], count=info["count"], budget=budget_amt,
            usage_pct=round(info["amount"] / budget_amt * 100, 1) if budget_amt else None,
        ))

    # 各来源小计
    feishu_amount = sum(round(a or 0, 2) for _, _, a, _ in cat_rows)
    feishu_count = sum(c for _, _, _, c in cat_rows)
    awx_amount_total = sum(round(a or 0, 2) for _, _, a, _ in awx_rows)
    awx_count_total = sum(c for _, _, _, c in awx_rows)

    return MonthlySummary(
        year_month=year_month, total_amount=round(total_amount, 2),
        approved_count=approved_count, pending_count=pending_count,
        total_budget=round(total_budget, 2),
        budget_usage_pct=round(total_amount / total_budget * 100, 1) if total_budget > 0 else 0,
        by_category=by_category,
        by_source=SourceBreakdown(
            feishu_amount=round(feishu_amount, 2), feishu_count=feishu_count,
            airwallex_amount=round(awx_amount_total, 2), airwallex_count=awx_count_total,
        ),
    )


@router.get("/trend", response_model=list[TrendPoint])
async def expense_trend(
    months: int = Query(6, ge=1, le=24),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    # 审批趋势
    appr_q = select(_ym(ACCT_DATE).label("ym"),
                    func.sum(func.coalesce(Approval.amount_cny, Approval.amount))).where(
        Approval.status.in_([1, 2]), ACCT_DATE.isnot(None))
    if allowed is not None:
        appr_q = appr_q.where(Approval.category_id.in_(allowed))
    appr_rows = (await db.execute(appr_q.group_by("ym"))).all()

    # Airwallex 趋势
    awx_q = select(_ym(AirwallexTransaction.transaction_date).label("ym"),
                   (-func.sum(func.coalesce(AirwallexTransaction.amount_cny, AirwallexTransaction.amount))).label("total")).where(
        AirwallexTransaction.transaction_date.isnot(None))
    if allowed is not None:
        awx_q = awx_q.where(AirwallexTransaction.category_id.in_(allowed))
    awx_rows = (await db.execute(awx_q.group_by("ym"))).all()

    # 合并
    merged: dict[str, float] = {}
    for ym, total in appr_rows:
        merged[ym] = round(total or 0, 2)
    for ym, total in awx_rows:
        merged[ym] = round(merged.get(ym, 0) + (total or 0), 2)

    sorted_months = sorted(merged.keys(), reverse=True)[:months]
    return [TrendPoint(year_month=ym, total=merged[ym]) for ym in reversed(sorted_months)]


@router.get("/budget-alerts", response_model=list[BudgetAlert])
async def budget_alerts(
    year_month: str = Query(...),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    alert_q = select(Budget, ExpenseCategory.name).join(
        ExpenseCategory, Budget.category_id == ExpenseCategory.id
    ).where(Budget.year_month == year_month)
    if allowed is not None:
        alert_q = alert_q.where(Budget.category_id.in_(allowed))
    budgets = await db.execute(alert_q)

    alerts = []
    for budget, cat_name in budgets.all():
        # 审批支出
        appr_spent = await db.scalar(
            select(func.coalesce(func.sum(func.coalesce(Approval.amount_cny, Approval.amount)), 0)).where(
                Approval.category_id == budget.category_id, Approval.status.in_([1, 2]),
                _ym(ACCT_DATE) == year_month)
        ) or 0.0
        # Airwallex 支出
        awx_spent = await db.scalar(
            select((-func.coalesce(func.sum(func.coalesce(AirwallexTransaction.amount_cny, AirwallexTransaction.amount)), 0)).label("total")).where(
                AirwallexTransaction.category_id == budget.category_id,
                _ym(AirwallexTransaction.transaction_date) == year_month)
        ) or 0.0
        spent = round(appr_spent + awx_spent, 2)
        pct = round(spent / budget.amount * 100, 1) if budget.amount > 0 else 0
        if pct >= 80:
            alerts.append(BudgetAlert(
                category_id=budget.category_id, category_name=cat_name,
                budget=budget.amount, spent=spent, usage_pct=pct,
            ))
    return sorted(alerts, key=lambda a: a.usage_pct, reverse=True)


@router.get("/uncategorized")
async def uncategorized_items(
    year_month: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取未分类的审批和 Airwallex 记录。"""
    # 飞书审批未分类
    appr_q = select(Approval.id, Approval.definition_name, Approval.applicant_name,
                    Approval.amount, Approval.currency, Approval.amount_cny,
                    Approval.expense_date, Approval.description).where(
        Approval.category_id.is_(None), Approval.status.in_([1, 2]))
    if year_month:
        appr_q = appr_q.where(_ym(ACCT_DATE) == year_month)
    appr_rows = (await db.execute(appr_q.order_by(ACCT_DATE.desc()))).all()

    # Airwallex 未分类
    awx_q = select(AirwallexTransaction.id, AirwallexTransaction.merchant_name,
                   AirwallexTransaction.card_nickname, AirwallexTransaction.amount,
                   AirwallexTransaction.currency, AirwallexTransaction.transaction_date).where(
        AirwallexTransaction.category_id.is_(None))
    if year_month:
        awx_q = awx_q.where(_ym(AirwallexTransaction.transaction_date) == year_month)
    awx_rows = (await db.execute(awx_q.order_by(AirwallexTransaction.transaction_date.desc()))).all()

    items = []
    for r in appr_rows:
        items.append({
            "id": r[0], "source": "feishu", "type": r[1], "who": r[2],
            "amount": r[3], "currency": r[4] or "CNY", "amount_cny": r[5] or r[3],
            "date": str(r[6]) if r[6] else None, "desc": (r[7] or "")[:60],
        })
    for r in awx_rows:
        items.append({
            "id": r[0], "source": "airwallex", "type": "Airwallex", "who": r[2],
            "amount": r[3], "currency": r[4] or "USD", "amount_cny": None,
            "date": str(r[5]) if r[5] else None, "desc": r[1] or "",
        })
    return {"total": len(items), "items": items}

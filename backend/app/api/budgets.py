from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.budget import Budget
from app.models.approval import Approval
from app.models.category import ExpenseCategory
from app.schemas.budget import BudgetCreate, BudgetUpdate, BudgetResponse, BudgetWithSpent
from app.auth import get_allowed_categories

router = APIRouter(prefix="/budgets", tags=["budgets"])


@router.get("", response_model=list[BudgetWithSpent])
async def list_budgets(
    year_month: str = Query(...),
    allowed: list[int] | None = Depends(get_allowed_categories),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Budget, ExpenseCategory.name)
        .join(ExpenseCategory, Budget.category_id == ExpenseCategory.id)
        .where(Budget.year_month == year_month)
    )
    if allowed is not None:
        q = q.where(Budget.category_id.in_(allowed))
    result = await db.execute(
        q.order_by(ExpenseCategory.sort_order, ExpenseCategory.id)
    )
    rows = result.all()

    items = []
    for budget, cat_name in rows:
        spent = await db.scalar(
            select(func.coalesce(func.sum(Approval.amount), 0))
            .where(Approval.category_id == budget.category_id)
            .where(Approval.status.in_([1, 2]))
            .where(func.substr(func.coalesce(Approval.submit_date, Approval.expense_date), 1, 7) == year_month)
        ) or 0.0
        items.append(BudgetWithSpent(
            id=budget.id,
            year_month=budget.year_month,
            category_id=budget.category_id,
            category_name=cat_name,
            amount=budget.amount,
            spent=round(spent, 2),
            usage_pct=round(spent / budget.amount * 100, 1) if budget.amount > 0 else 0,
            note=budget.note or "",
        ))
    return items


@router.post("", response_model=BudgetResponse)
async def create_budget(data: BudgetCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(
        select(Budget).where(Budget.year_month == data.year_month, Budget.category_id == data.category_id)
    )
    if existing:
        raise HTTPException(status_code=400, detail="该月该分类的预算已存在")
    budget = Budget(**data.model_dump())
    db.add(budget)
    await db.commit()
    await db.refresh(budget)
    return budget


@router.put("/{budget_id}", response_model=BudgetResponse)
async def update_budget(budget_id: int, data: BudgetUpdate, db: AsyncSession = Depends(get_db)):
    budget = await db.get(Budget, budget_id)
    if not budget:
        raise HTTPException(status_code=404, detail="预算不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(budget, k, v)
    await db.commit()
    await db.refresh(budget)
    return budget


@router.delete("/{budget_id}")
async def delete_budget(budget_id: int, db: AsyncSession = Depends(get_db)):
    budget = await db.get(Budget, budget_id)
    if not budget:
        raise HTTPException(status_code=404, detail="预算不存在")
    await db.delete(budget)
    await db.commit()
    return {"ok": True}


@router.post("/copy-from-previous")
async def copy_from_previous(year_month: str = Query(...), db: AsyncSession = Depends(get_db)):
    """从上月复制预算到指定月份"""
    parts = year_month.split("-")
    y, m = int(parts[0]), int(parts[1])
    if m == 1:
        prev = f"{y - 1}-12"
    else:
        prev = f"{y}-{m - 1:02d}"

    prev_budgets = await db.execute(select(Budget).where(Budget.year_month == prev))
    prev_list = prev_budgets.scalars().all()
    if not prev_list:
        raise HTTPException(status_code=404, detail=f"{prev} 没有预算数据可复制")

    created = 0
    for b in prev_list:
        existing = await db.scalar(
            select(Budget).where(Budget.year_month == year_month, Budget.category_id == b.category_id)
        )
        if not existing:
            db.add(Budget(year_month=year_month, category_id=b.category_id, amount=b.amount, note=b.note))
            created += 1
    await db.commit()
    return {"ok": True, "created": created, "skipped": len(prev_list) - created}

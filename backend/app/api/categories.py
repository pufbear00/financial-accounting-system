from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.category import ExpenseCategory
from app.models.approval import Approval
from app.schemas.category import CategoryCreate, CategoryUpdate, CategoryResponse

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryResponse])
async def list_categories(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.id))
    return result.scalars().all()


@router.post("", response_model=CategoryResponse)
async def create_category(data: CategoryCreate, db: AsyncSession = Depends(get_db)):
    cat = ExpenseCategory(**data.model_dump())
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return cat


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(category_id: int, data: CategoryUpdate, db: AsyncSession = Depends(get_db)):
    cat = await db.get(ExpenseCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(cat, k, v)
    await db.commit()
    await db.refresh(cat)
    return cat


@router.delete("/{category_id}")
async def delete_category(category_id: int, db: AsyncSession = Depends(get_db)):
    count = await db.scalar(select(func.count()).where(Approval.category_id == category_id))
    if count and count > 0:
        raise HTTPException(status_code=400, detail=f"该分类下有 {count} 条审批记录，无法删除")
    cat = await db.get(ExpenseCategory, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    await db.delete(cat)
    await db.commit()
    return {"ok": True}

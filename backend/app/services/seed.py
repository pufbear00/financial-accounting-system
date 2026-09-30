"""Generic category labels only; no business records or default account."""
from sqlalchemy import select
from app.database import async_session
from app.models.category import ExpenseCategory

DEFAULT_CATEGORIES = ["办公费用", "差旅费用", "软件服务", "其他"]
CATEGORY_MAPPING: dict[str, str] = {}


async def seed_categories():
    async with async_session() as db:
        for index, name in enumerate(DEFAULT_CATEGORIES):
            if not await db.scalar(select(ExpenseCategory).where(ExpenseCategory.name == name)):
                db.add(ExpenseCategory(name=name, sort_order=index))
        await db.commit()

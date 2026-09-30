from sqlalchemy import Column, Integer, String, DateTime, func

from app.database import Base


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, default="")
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

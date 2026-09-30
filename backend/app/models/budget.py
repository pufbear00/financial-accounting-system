from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, func

from app.database import Base


class Budget(Base):
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year_month = Column(String, nullable=False)  # "2026-04"
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=False)
    amount = Column(Float, nullable=False)
    note = Column(String, default="")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("year_month", "category_id", name="uq_budget_month_category"),)

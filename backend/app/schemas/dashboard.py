from pydantic import BaseModel


class CategorySummary(BaseModel):
    category_id: int | None
    category_name: str
    amount: float
    count: int
    budget: float | None
    usage_pct: float | None


class SourceBreakdown(BaseModel):
    feishu_amount: float
    feishu_count: int
    airwallex_amount: float
    airwallex_count: int


class MonthlySummary(BaseModel):
    year_month: str
    total_amount: float
    approved_count: int
    pending_count: int
    total_budget: float
    budget_usage_pct: float
    by_category: list[CategorySummary]
    by_source: SourceBreakdown


class TrendPoint(BaseModel):
    year_month: str
    total: float


class BudgetAlert(BaseModel):
    category_id: int
    category_name: str
    budget: float
    spent: float
    usage_pct: float

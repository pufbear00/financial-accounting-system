from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, func

from app.database import Base


class AirwallexTransaction(Base):
    __tablename__ = "airwallex_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_id = Column(String, unique=True, nullable=False, index=True)  # lifecycle_id 去重
    card_id = Column(String)
    card_nickname = Column(String)       # 卡昵称（研发-订阅卡、市场运营费用 等）
    masked_card_number = Column(String)
    merchant_name = Column(String)       # 商户名（OPENAI、CLAUDE.AI 等）
    merchant_category = Column(String)   # 商户分类（Software 等）
    amount = Column(Float)               # 原币金额（负数=支出）
    currency = Column(String)            # 币种
    exchange_rate = Column(Float)        # 交易日真实汇率（→CNY，Frankfurter）
    amount_cny = Column(Float)           # 折合人民币（保留正负号）
    status = Column(String)              # APPROVED / DECLINED 等
    transaction_date = Column(Date)
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=True)
    raw_data = Column(String)            # 原始 JSON
    synced_at = Column(DateTime, server_default=func.now())

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, func

from app.database import Base


class Approval(Base):
    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instance_code = Column(String, unique=True, nullable=False, index=True)
    task_id = Column(String)
    definition_code = Column(String, nullable=False)
    definition_name = Column(String)  # "费用报销" / "付款申请"
    applicant_id = Column(String)     # open_id
    applicant_name = Column(String)
    status = Column(Integer)          # 1=待审批, 2=已通过, 3=已拒绝, 4=已撤销
    amount = Column(Float)            # 原币金额
    currency = Column(String, default="CNY")  # 币种
    exchange_rate = Column(Float)     # 汇率（→CNY）
    amount_cny = Column(Float)        # 折合人民币
    category_name = Column(String)    # 从 summaries 提取的原始分类名
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=True)
    expense_date = Column(Date)       # 表单付款日期（计划付款日，可能是未来）
    submit_date = Column(Date)        # 飞书提交日期（记账归属月用这个）
    description = Column(String)      # 事由
    raw_summaries = Column(String)    # 原始 summaries JSON
    topic = Column(Integer)           # 1=待审批, 2=已审批, 3=我发起
    synced_at = Column(DateTime, server_default=func.now())

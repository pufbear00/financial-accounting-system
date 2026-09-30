from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func

from app.database import Base


class ApprovalComment(Base):
    __tablename__ = "approval_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    approval_id = Column(Integer, ForeignKey("approvals.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user_name = Column(String, nullable=False)
    content = Column(String, nullable=False)
    resolved = Column(Integer, default=0)  # 0=未处理, 1=已处理
    created_at = Column(DateTime, server_default=func.now())

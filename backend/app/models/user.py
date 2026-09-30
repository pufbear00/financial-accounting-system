from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, func

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="viewer")  # admin / viewer
    feishu_open_id = Column(String, nullable=True)  # 飞书 open_id，用于推送日报
    created_at = Column(DateTime, server_default=func.now())


class UserCategory(Base):
    __tablename__ = "user_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("expense_categories.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "category_id", name="uq_user_category"),)

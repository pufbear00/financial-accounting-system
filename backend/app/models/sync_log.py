from sqlalchemy import Column, Integer, String, DateTime, func

from app.database import Base


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_type = Column(String, nullable=False)  # manual / scheduled
    total_fetched = Column(Integer, default=0)
    new_records = Column(Integer, default=0)
    updated_records = Column(Integer, default=0)
    status = Column(String, nullable=False)  # running / completed / failed
    error_message = Column(String)
    started_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)

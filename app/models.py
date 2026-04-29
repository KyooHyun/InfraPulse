from sqlalchemy import Column, Integer, String, Float, DateTime, func
from .db import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    account_from = Column(String(64), nullable=False)
    account_to = Column(String(64), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default="KRW")
    status = Column(String(32), nullable=False)
    reason = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

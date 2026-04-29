from pydantic import BaseModel
from datetime import datetime


class TransferRequest(BaseModel):
    account_from: str
    account_to: str
    amount: float
    currency: str = "KRW"


class LoginRequest(BaseModel):
    username: str
    password: str


class TransactionOut(BaseModel):
    id: int
    account_from: str
    account_to: str
    amount: float
    currency: str
    status: str
    reason: str | None
    created_at: datetime

    class Config:
        orm_mode = True

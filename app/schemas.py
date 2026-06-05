from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, validator


# ── 거래 ──────────────────────────────────────────────────────────────────────

_ALLOWED_CURRENCIES = {"KRW", "USD", "EUR", "JPY"}


class TransferRequest(BaseModel):
    account_from: str
    account_to: str
    amount: float
    currency: str = "KRW"

    @validator("amount")
    def amount_positive(cls, v):
        if v <= 0:
            raise ValueError("거래 금액은 0보다 커야 합니다")
        return v

    @validator("account_to")
    def accounts_must_differ(cls, v, values):
        if "account_from" in values and v == values["account_from"]:
            raise ValueError("송금 계좌와 수취 계좌가 동일합니다")
        return v

    @validator("currency")
    def valid_currency(cls, v):
        if v not in _ALLOWED_CURRENCIES:
            raise ValueError(f"지원 통화: {', '.join(sorted(_ALLOWED_CURRENCIES))}")
        return v


class TransactionOut(BaseModel):
    id: int
    account_from: str
    account_to: str
    amount: float
    currency: str
    status: str
    reason: Optional[str]
    risk_score: float
    created_at: datetime

    class Config:
        orm_mode = True


# ── 인증 ──────────────────────────────────────────────────────────────────────

class Token(BaseModel):
    access_token: str
    token_type: str


_ALLOWED_ROLES = {"STAFF", "RISK_OFFICER", "ADMIN"}


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: str = "STAFF"

    @validator("password")
    def password_min_length(cls, v):
        if len(v) < 8:
            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다")
        return v

    @validator("role")
    def valid_role(cls, v):
        if v not in _ALLOWED_ROLES:
            raise ValueError(f"유효한 역할: {', '.join(_ALLOWED_ROLES)}")
        return v

    @validator("username")
    def username_no_space(cls, v):
        if not v.strip() or " " in v:
            raise ValueError("사용자명에 공백을 포함할 수 없습니다")
        return v


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        orm_mode = True


# ── 감사 로그 ─────────────────────────────────────────────────────────────────

class AuditLogOut(BaseModel):
    id: int
    user_id: Optional[int]
    action: str
    entity_type: Optional[str]
    entity_id: Optional[str]
    detail: Optional[str]
    ip_address: Optional[str]
    checksum: Optional[str]
    created_at: datetime

    class Config:
        orm_mode = True


# ── FDS 룰 ────────────────────────────────────────────────────────────────────

class FdsRuleOut(BaseModel):
    id: int
    name: str
    condition_type: str
    threshold: float
    weight: float
    is_active: bool
    created_at: datetime

    class Config:
        orm_mode = True


class FdsRuleUpdate(BaseModel):
    threshold: Optional[float] = None
    weight: Optional[float] = None
    is_active: Optional[bool] = None

    @validator("threshold")
    def threshold_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("임계값은 0보다 커야 합니다")
        return v

    @validator("weight")
    def weight_range(cls, v):
        if v is not None and not (0 < v <= 100):
            raise ValueError("가중치는 0 초과 100 이하여야 합니다")
        return v


# ── FDS 알림 ──────────────────────────────────────────────────────────────────

class FdsAlertOut(BaseModel):
    id: int
    transaction_id: Optional[int]
    alert_type: str
    risk_score: float
    status: str
    detail: Optional[str]
    created_at: datetime
    reviewed_at: Optional[datetime]
    reviewed_by: Optional[int]

    class Config:
        orm_mode = True


# ── FDS 검토 결정 ─────────────────────────────────────────────────────────────

class FdsDecisionCreate(BaseModel):
    decision: str  # APPROVE | REJECT
    comment: Optional[str] = None

    @validator("decision")
    def valid_decision(cls, v):
        if v not in ("APPROVE", "REJECT"):
            raise ValueError("decision은 APPROVE 또는 REJECT")
        return v


class FdsDecisionOut(BaseModel):
    id: int
    alert_id: int
    reviewer_id: int
    decision: str
    comment: Optional[str]
    created_at: datetime

    class Config:
        orm_mode = True


# ── 컴플라이언스 보고서 ────────────────────────────────────────────────────────

class ComplianceReportOut(BaseModel):
    id: int
    report_type: str
    transaction_id: int
    account_from: str
    account_to: str
    amount: float
    currency: str
    reason: Optional[str]
    status: str
    report_number: str
    created_at: datetime
    submitted_at: Optional[datetime]

    class Config:
        orm_mode = True


# ── KYC ───────────────────────────────────────────────────────────────────────

_ALLOWED_ID_TYPES = {"RESIDENT_ID", "PASSPORT", "BUSINESS_REG"}
_ALLOWED_RISK_GRADES = {"LOW", "MEDIUM", "HIGH"}


class KycRecordCreate(BaseModel):
    account_id: str
    customer_name: str
    id_type: str
    id_number_masked: str  # 원문 식별번호를 받지 않는다 — 마스킹 값만 저장
    risk_grade: Optional[str] = "LOW"

    @validator("id_type")
    def valid_id_type(cls, v):
        if v not in _ALLOWED_ID_TYPES:
            raise ValueError(f"유효한 신분증 유형: {', '.join(_ALLOWED_ID_TYPES)}")
        return v

    @validator("risk_grade")
    def valid_risk_grade(cls, v):
        if v is not None and v not in _ALLOWED_RISK_GRADES:
            raise ValueError(f"위험 등급: {', '.join(_ALLOWED_RISK_GRADES)}")
        return v

    @validator("customer_name")
    def name_not_empty(cls, v):
        if not v.strip():
            raise ValueError("고객명은 필수입니다")
        return v


class KycRecordOut(BaseModel):
    id: int
    account_id: str
    customer_name: str
    id_type: str
    id_number_masked: str
    verification_status: str
    risk_grade: str
    verified_at: Optional[datetime]
    created_at: datetime

    class Config:
        orm_mode = True

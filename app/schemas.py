from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ── 거래 ──────────────────────────────────────────────────────────────────────

_ALLOWED_CURRENCIES = {"KRW", "USD", "EUR", "JPY"}


class TransferRequest(BaseModel):
    account_from: str
    account_to: str
    amount: float
    currency: str = "KRW"

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("거래 금액은 0보다 커야 합니다")
        return v

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, v: str) -> str:
        if v not in _ALLOWED_CURRENCIES:
            raise ValueError(f"지원 통화: {', '.join(sorted(_ALLOWED_CURRENCIES))}")
        return v

    @model_validator(mode="after")
    def accounts_must_differ(self) -> "TransferRequest":
        if self.account_from == self.account_to:
            raise ValueError("송금 계좌와 수취 계좌가 동일합니다")
        return self


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_from: str
    account_to: str
    amount: float
    currency: str
    status: str
    reason: Optional[str]
    risk_score: float
    created_at: datetime
    ml_anomaly_score: Optional[float] = None
    ensemble_score: Optional[float] = None


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

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다")
        return v

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        if v not in _ALLOWED_ROLES:
            raise ValueError(f"유효한 역할: {', '.join(_ALLOWED_ROLES)}")
        return v

    @field_validator("username")
    @classmethod
    def username_no_space(cls, v: str) -> str:
        if not v.strip() or " " in v:
            raise ValueError("사용자명에 공백을 포함할 수 없습니다")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


# ── 감사 로그 ─────────────────────────────────────────────────────────────────

class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int]
    action: str
    entity_type: Optional[str]
    entity_id: Optional[str]
    detail: Optional[str]
    ip_address: Optional[str]
    checksum: Optional[str]
    created_at: datetime


# ── FDS 룰 ────────────────────────────────────────────────────────────────────

class FdsRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    condition_type: str
    threshold: float
    weight: float
    is_active: bool
    created_at: datetime


class FdsRuleUpdate(BaseModel):
    threshold: Optional[float] = None
    weight: Optional[float] = None
    is_active: Optional[bool] = None

    @field_validator("threshold")
    @classmethod
    def threshold_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("임계값은 0보다 커야 합니다")
        return v

    @field_validator("weight")
    @classmethod
    def weight_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 < v <= 100):
            raise ValueError("가중치는 0 초과 100 이하여야 합니다")
        return v


# ── FDS 알림 ──────────────────────────────────────────────────────────────────

class FdsAlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: Optional[int]
    alert_type: str
    risk_score: float
    status: str
    detail: Optional[str]
    created_at: datetime
    reviewed_at: Optional[datetime]
    reviewed_by: Optional[int]


# ── FDS 검토 결정 ─────────────────────────────────────────────────────────────

class FdsDecisionCreate(BaseModel):
    decision: str  # APPROVE | REJECT
    comment: Optional[str] = None

    @field_validator("decision")
    @classmethod
    def valid_decision(cls, v: str) -> str:
        if v not in ("APPROVE", "REJECT"):
            raise ValueError("decision은 APPROVE 또는 REJECT")
        return v


class FdsDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_id: int
    reviewer_id: int
    decision: str
    comment: Optional[str]
    created_at: datetime


# ── 컴플라이언스 보고서 ────────────────────────────────────────────────────────

class ComplianceReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


# ── FDS 운영 통계 ──────────────────────────────────────────────────────────────

class FdsStatsOut(BaseModel):
    # 거래 현황
    total_transactions: int
    failed_transactions: int
    failure_rate_pct: float

    # FDS 알림 현황
    total_alerts: int
    pending_review: int
    detection_rate_pct: float       # 알림 수 / 거래 수 * 100
    false_positive_rate_pct: Optional[float]  # APPROVED / (APPROVED+REJECTED) * 100
    alerts_by_rule: dict            # 룰 유형별 알림 건수

    # 위험 점수 분포
    avg_risk_score: float
    high_risk_count: int            # risk_score >= 70
    high_risk_rate_pct: float

    # 컴플라이언스 보고서
    str_total: int
    ctr_total: int
    pending_compliance: int

    # 룰 커버리지 — 활성 룰 중 실제 알림을 발생시킨 룰 유형 비율
    active_rule_count: int
    triggered_rule_types: int
    rule_coverage_pct: float


# ── KYC ───────────────────────────────────────────────────────────────────────

_ALLOWED_ID_TYPES = {"RESIDENT_ID", "PASSPORT", "BUSINESS_REG"}
_ALLOWED_RISK_GRADES = {"LOW", "MEDIUM", "HIGH"}


class KycRecordCreate(BaseModel):
    account_id: str
    customer_name: str
    id_type: str
    id_number_masked: str  # 원문 식별번호를 받지 않는다 — 마스킹 값만 저장
    risk_grade: Optional[str] = "LOW"

    @field_validator("id_type")
    @classmethod
    def valid_id_type(cls, v: str) -> str:
        if v not in _ALLOWED_ID_TYPES:
            raise ValueError(f"유효한 신분증 유형: {', '.join(_ALLOWED_ID_TYPES)}")
        return v

    @field_validator("risk_grade")
    @classmethod
    def valid_risk_grade(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALLOWED_RISK_GRADES:
            raise ValueError(f"위험 등급: {', '.join(_ALLOWED_RISK_GRADES)}")
        return v

    @field_validator("customer_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("고객명은 필수입니다")
        return v


class KycRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    customer_name: str
    id_type: str
    id_number_masked: str
    verification_status: str
    risk_grade: str
    verified_at: Optional[datetime]
    created_at: datetime

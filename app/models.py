from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Float, DateTime, Text, func
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
    risk_score = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # PaySim 적재 및 ML 레이어 컬럼 (기존 거래는 모두 NULL)
    transaction_type = Column(String(32), nullable=True)
    balance_orig_before = Column(Float, nullable=True)
    balance_orig_after = Column(Float, nullable=True)
    balance_dest_before = Column(Float, nullable=True)
    balance_dest_after = Column(Float, nullable=True)
    is_fraud = Column(Boolean, nullable=True)        # PaySim 정답 레이블
    ml_anomaly_score = Column(Float, nullable=True)  # Isolation Forest 이상 점수 (0~1)
    ensemble_score = Column(Float, nullable=True)    # 룰+ML 앙상블 최종 점수 (0~100)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(128), unique=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="STAFF")  # STAFF | RISK_OFFICER | ADMIN
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLog(Base):
    """불변 감사 추적 — 행을 수정/삭제하지 않는다."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False)
    entity_type = Column(String(64), nullable=True)
    entity_id = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    checksum = Column(String(64), nullable=True)  # SHA-256 무결성 해시
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FdsRule(Base):
    """DB 기반 FDS 룰 — 운영 중 임계값/가중치 변경 가능."""
    __tablename__ = "fds_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    condition_type = Column(String(64), nullable=False)  # HIGH_VALUE | FAILURE_RATE | LOGIN_FAILURE | LATENCY | VELOCITY
    threshold = Column(Float, nullable=False)
    weight = Column(Float, nullable=False, default=1.0)  # 위험점수 기여 가중치
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FdsAlert(Base):
    __tablename__ = "fds_alerts"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    alert_type = Column(String(64), nullable=False)
    risk_score = Column(Float, nullable=False)
    status = Column(String(32), nullable=False, default="DETECTED")  # DETECTED | UNDER_REVIEW | APPROVED | REJECTED
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class FdsDecision(Base):
    __tablename__ = "fds_decisions"

    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, ForeignKey("fds_alerts.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    decision = Column(String(32), nullable=False)  # APPROVE(정상) | REJECT(이상거래 확정)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ComplianceReport(Base):
    """특정금융정보법 기반 자동 보고서 — STR(의심거래) / CTR(고액현금거래)."""
    __tablename__ = "compliance_reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(32), nullable=False)  # STR | CTR
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)
    account_from = Column(String(64), nullable=False)
    account_to = Column(String(64), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="PENDING")  # PENDING | SUBMITTED | ACKNOWLEDGED
    report_number = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)


class KycRecord(Base):
    """고객확인제도(KYC) — 계좌별 신원 확인 및 위험 등급."""
    __tablename__ = "kyc_records"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(String(64), unique=True, nullable=False, index=True)
    customer_name = Column(String(128), nullable=False)
    id_type = Column(String(32), nullable=False)  # RESIDENT_ID | PASSPORT | BUSINESS_REG
    id_number_masked = Column(String(64), nullable=False)  # 마스킹된 식별번호 (예: 900101-1*****)
    verification_status = Column(String(32), nullable=False, default="PENDING")  # PENDING | VERIFIED | REJECTED
    risk_grade = Column(String(8), nullable=False, default="LOW")  # LOW | MEDIUM | HIGH
    verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

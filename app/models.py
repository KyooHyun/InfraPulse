from sqlalchemy import (
    Boolean, Column, ForeignKey, Integer, String, Float, Numeric, DateTime, Text, func,
)
from .db import Base


class Account(Base):
    """계좌 원장 — 이체 시 잔액이 실제로 증감하는 유일한 테이블.

    금액은 Float가 아니라 Numeric(18, 2)로 둔다. 이진 부동소수는 0.1을 정확히
    표현하지 못해 잔액을 더하고 빼는 과정에서 오차가 누적된다. 원장은 합계가
    맞아야 하는 테이블이므로 십진 고정소수를 쓴다.
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(String(64), unique=True, nullable=False, index=True)
    balance = Column(Numeric(18, 2), nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="KRW")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    """불변 감사 추적 — 행을 수정/삭제하지 않으며, 해시 체인으로 그것을 증명한다.

    각 행의 checksum은 **직전 행의 checksum을 입력에 포함**해 계산된다(블록체인과
    같은 구조). 행 단위로 독립된 해시였을 때는 행 하나를 통째로 삭제해도 남은 행들의
    해시가 전부 맞아떨어져 탐지되지 않았다. 체인으로 묶으면 삭제·삽입·재배열이
    연결 고리를 끊으므로 audit.verify_chain()이 그 지점을 찾아낸다.

    event_time은 해시 입력에 들어간 타임스탬프 문자열을 그대로 보관한다.
    created_at을 다시 포맷해서 쓰면 DB마다 마이크로초 자리를 다르게 저장해
    (MySQL DATETIME은 기본이 초 단위) 검증이 DB 종류에 따라 깨진다.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False)
    entity_type = Column(String(64), nullable=True)
    entity_id = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)
    checksum = Column(String(64), nullable=True)       # SHA-256 체인 해시
    prev_checksum = Column(String(64), nullable=True)  # 직전 행의 checksum
    event_time = Column(String(32), nullable=True)     # 해시에 들어간 ISO-8601 UTC
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditChainHead(Base):
    """감사 로그 해시 체인의 머리 — 행이 **하나만** 존재한다(id=1).

    존재 이유는 두 가지다.

    1) 직렬화 지점. 체인에 행을 붙이려면 "현재 마지막 해시"를 읽고 그것을 포함해
       새 해시를 만들어야 한다. 두 요청이 같은 마지막 해시를 읽으면 형제 행 두 개가
       생겨 체인이 갈라지고, 검증기는 이를 위변조와 구분할 수 없다. 모든 기록이
       이 행을 FOR UPDATE로 잠그고 지나가게 해서 그 경합을 없앤다.
       대가는 감사 기록 쓰기가 전역 직렬화된다는 것이다. 감사 로그는 쓰기량이
       거래의 1~2배 수준이고 순서 자체가 증거이므로 받아들일 만한 비용이다.

    2) 꼬리 삭제 탐지. 마지막 행들을 통째로 지우면 남은 행끼리의 연결은 멀쩡하다.
       머리 해시를 따로 보관해야 "마지막 행이 사라졌다"를 알 수 있다.
    """
    __tablename__ = "audit_chain_head"

    id = Column(Integer, primary_key=True)
    head_checksum = Column(String(64), nullable=False)
    entry_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


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
    """특정금융정보법 보고서 — STR(의심거래). CTR은 현금 거래 유형 추가 시 구현."""
    __tablename__ = "compliance_reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(32), nullable=False)  # STR
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

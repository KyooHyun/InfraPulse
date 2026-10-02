-- STR 상태값 변경 마이그레이션 (PENDING → DRAFT)
--
-- 이전: 보고서는 PENDING으로 생성되어 바로 제출(SUBMITTED)할 수 있었다.
-- 이후: DRAFT → (담당자 검토) APPROVED | DISMISSED → APPROVED만 SUBMITTED.
--
-- 기존 PENDING 행은 검토를 거치지 않았으므로 DRAFT로 되돌린다. 그대로 두면
-- 검토(DRAFT만 허용)도 제출(APPROVED만 허용)도 할 수 없는 상태로 남는다.
--
-- 이체 경로에서 만들어졌던 CTR 행은 제도상 CTR 대상(현금 거래)이 아니었다.
-- 이력 보존을 위해 지우지 않고, 제출되지 않은 것만 DISMISSED로 종결한다.
--
-- 이미 SUBMITTED된 행은 건드리지 않는다.

UPDATE compliance_reports SET status = 'DRAFT'
 WHERE status = 'PENDING' AND report_type = 'STR';

UPDATE compliance_reports SET status = 'DISMISSED'
 WHERE status = 'PENDING' AND report_type = 'CTR';

-- 컴플라이언스 보고서 검토 컬럼 추가 (MySQL)
--
-- 검토자·시각·사유를 감사 로그에만 두면 "누가 왜 승인했는가"를 조회하려고 감사 로그의
-- detail을 뒤져야 한다. 감사 로그는 위변조 탐지용 기록이므로, 업무 데이터는 보고서에 둔다.
-- (fds_alerts.reviewed_by / reviewed_at 과 같은 구조)
--
-- 이 컬럼 추가 이전에 검토된 보고서는 세 컬럼이 NULL이다. 그 검토 기록은 audit_logs의
-- REVIEW_STR_* 항목에 남아 있다.

ALTER TABLE compliance_reports
  ADD COLUMN reviewed_at   DATETIME NULL,
  ADD COLUMN reviewed_by   INT      NULL,
  ADD COLUMN review_reason TEXT     NULL,
  ADD CONSTRAINT fk_compliance_reports_reviewed_by
      FOREIGN KEY (reviewed_by) REFERENCES users (id);

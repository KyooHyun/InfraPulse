import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'

function fmtDate(dt) {
  return new Date(dt).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
}

function fmtAmt(amount) {
  return amount?.toLocaleString('ko-KR') + ' 원'
}

const STATUS_INFO = {
  DRAFT: { label: '검토 대기', badge: 'badge-warning' },
  APPROVED: { label: '제출 대기', badge: 'badge-info' },
  DISMISSED: { label: '보고 불필요', badge: 'badge-gray' },
  SUBMITTED: { label: '제출 완료', badge: 'badge-success' },
}

const TYPE_INFO = {
  STR: {
    label: 'STR',
    desc: '의심거래보고',
    badge: 'badge-danger',
    law: '특금법 제4조',
  },
}

export default function Compliance({ user }) {
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(null)
  const [msg, setMsg] = useState(null)

  const canSubmit = user?.role === 'RISK_OFFICER' || user?.role === 'ADMIN'

  const load = useCallback(async () => {
    try {
      const data = await apiFetch('/compliance/reports')
      setReports(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  async function handleReview(id, decision) {
    const comment = window.prompt(
      decision === 'APPROVE' ? '보고 대상으로 판단한 근거를 입력하세요' : '보고 불필요로 판단한 근거를 입력하세요'
    )
    if (!comment || !comment.trim()) return
    setSubmitting(id)
    setMsg(null)
    try {
      await apiFetch(`/compliance/reports/${id}/review`, {
        method: 'POST',
        body: JSON.stringify({ decision, comment }),
      })
      setMsg({ type: 'success', text: `보고서 #${id} — ${decision === 'APPROVE' ? '보고 대상으로 승인' : '보고 불필요로 종결'}` })
      load()
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally {
      setSubmitting(null)
    }
  }

  async function handleSubmit(id) {
    setSubmitting(id)
    setMsg(null)
    try {
      await apiFetch(`/compliance/reports/${id}/submit`, { method: 'POST' })
      setMsg({ type: 'success', text: `보고서 #${id} — 금융정보분석원(FIU) 제출 완료` })
      load()
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally {
      setSubmitting(null)
    }
  }

  const strCount = reports.filter(r => r.report_type === 'STR').length
  const draftCount = reports.filter(r => r.status === 'DRAFT').length
  const approvedCount = reports.filter(r => r.status === 'APPROVED').length

  return (
    <div className="container">
      {msg && (
        <div className={`alert alert-${msg.type === 'success' ? 'success' : 'error'}`}>
          {msg.text}
        </div>
      )}

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">전체 보고서</div>
          <div className="stat-value">{reports.length}</div>
        </div>
        <div className="stat-card red">
          <div className="stat-label">STR (의심거래)</div>
          <div className="stat-value">{strCount}</div>
        </div>
        <div className="stat-card amber">
          <div className="stat-label">검토 대기</div>
          <div className="stat-value">{draftCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">제출 대기</div>
          <div className="stat-value">{approvedCount}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">
          📋 준법감시 보고서
          <span>특정금융정보법 제4조</span>
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn btn-ghost btn-sm" onClick={load}>새로고침</button>
          </div>
        </div>

        <div style={{ padding: '10px 0 14px', fontSize: 12, color: '#64748b', display: 'flex', gap: 24 }}>
          <span>🔴 <b>STR</b> — 위험점수 70 이상 거래에 초안 생성 → 담당자 판단 후 제출 (특금법 제4조)</span>
        </div>

        {loading ? (
          <div className="empty">불러오는 중...</div>
        ) : reports.length === 0 ? (
          <div className="empty">준법감시 보고서가 없습니다. 고위험 거래가 탐지되면 STR 초안이 생성됩니다.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>거래 ID</th>
                <th>유형</th>
                <th>금액</th>
                <th>상태</th>
                <th>관련 법령</th>
                <th>생성 일시</th>
                {canSubmit && <th>처리</th>}
              </tr>
            </thead>
            <tbody>
              {reports.map(report => {
                const info = TYPE_INFO[report.report_type] || { label: report.report_type, badge: 'badge-gray', law: '-' }
                return (
                  <tr key={report.id}>
                    <td style={{ color: '#94a3b8', fontSize: 12 }}>#{report.id}</td>
                    <td style={{ color: '#94a3b8', fontSize: 12 }}>TX#{report.transaction_id ?? '-'}</td>
                    <td>
                      <span className={`badge ${info.badge}`}>{info.label}</span>
                      <span style={{ marginLeft: 6, fontSize: 12, color: '#64748b' }}>{info.desc}</span>
                    </td>
                    <td style={{ fontWeight: 600 }}>{fmtAmt(report.amount)}</td>
                    <td>
                      <span className={`badge ${(STATUS_INFO[report.status] || {}).badge || 'badge-gray'}`}>
                        {(STATUS_INFO[report.status] || {}).label || report.status}
                      </span>
                      {report.review_reason && (
                        <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
                          사유: {report.review_reason}
                        </div>
                      )}
                    </td>
                    <td style={{ fontSize: 12, color: '#475569' }}>{info.law}</td>
                    <td style={{ color: '#94a3b8', fontSize: 12 }}>{fmtDate(report.created_at)}</td>
                    {canSubmit && (
                      <td>
                        {report.status === 'DRAFT' ? (
                          <div style={{ display: 'flex', gap: 6 }}>
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={submitting === report.id}
                              onClick={() => handleReview(report.id, 'APPROVE')}
                            >
                              보고 대상
                            </button>
                            <button
                              className="btn btn-ghost btn-sm"
                              disabled={submitting === report.id}
                              onClick={() => handleReview(report.id, 'DISMISS')}
                            >
                              보고 불필요
                            </button>
                          </div>
                        ) : report.status === 'APPROVED' ? (
                          <button
                            className="btn btn-primary btn-sm"
                            disabled={submitting === report.id}
                            onClick={() => handleSubmit(report.id)}
                          >
                            {submitting === report.id ? '처리중' : 'FIU 제출'}
                          </button>
                        ) : (
                          <span style={{ color: '#94a3b8', fontSize: 12 }}>완료</span>
                        )}
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ fontSize: 12, color: '#475569' }}>
        <div className="card-title" style={{ fontSize: 13 }}>📌 준법감시 규정 안내</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
          <div>
            <p style={{ fontWeight: 600, color: '#1e3a5f', marginBottom: 8 }}>CTR (고액현금거래보고) — 미구현</p>
            <ul style={{ lineHeight: 1.8, paddingLeft: 16 }}>
              <li>근거: 특정금융정보법 제4조의2</li>
              <li>기준: 동일인 1거래일 합산 1천만원 이상 현금 입출금</li>
              <li>본 시스템: 계좌이체만 있어 대상 거래 없음</li>
              <li>현금 거래 유형·고객 식별자 추가 시 구현 예정</li>
            </ul>
          </div>
          <div>
            <p style={{ fontWeight: 600, color: '#dc2626', marginBottom: 8 }}>STR (의심거래보고)</p>
            <ul style={{ lineHeight: 1.8, paddingLeft: 16 }}>
              <li>근거: 특정금융정보법 제4조</li>
              <li>기준: 자금세탁·불법재산 의심거래</li>
              <li>본 시스템: 위험점수 70 이상 초안 생성, 보고 여부는 담당자 판단</li>
              <li>제출기한: 의심 인식 후 3영업일 이내</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}

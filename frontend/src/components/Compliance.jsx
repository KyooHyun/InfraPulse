import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'

function fmtDate(dt) {
  return new Date(dt).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
}

function fmtAmt(amount) {
  return amount?.toLocaleString('ko-KR') + ' 원'
}

const TYPE_INFO = {
  CTR: {
    label: 'CTR',
    desc: '고액현금거래보고',
    badge: 'badge-info',
    law: '특금법 제4조의2',
  },
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

  const ctrCount = reports.filter(r => r.report_type === 'CTR').length
  const strCount = reports.filter(r => r.report_type === 'STR').length
  const pendingCount = reports.filter(r => r.status === 'PENDING').length

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
        <div className="stat-card">
          <div className="stat-label">CTR (고액거래)</div>
          <div className="stat-value">{ctrCount}</div>
        </div>
        <div className="stat-card red">
          <div className="stat-label">STR (의심거래)</div>
          <div className="stat-value">{strCount}</div>
        </div>
        <div className="stat-card amber">
          <div className="stat-label">제출 대기</div>
          <div className="stat-value">{pendingCount}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">
          📋 준법감시 보고서
          <span>특정금융정보법 제4조·제4조의2</span>
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn btn-ghost btn-sm" onClick={load}>새로고침</button>
          </div>
        </div>

        <div style={{ padding: '10px 0 14px', fontSize: 12, color: '#64748b', display: 'flex', gap: 24 }}>
          <span>🔵 <b>CTR</b> — 1천만원 이상 현금거래 자동 보고 (특금법 제4조의2)</span>
          <span>🔴 <b>STR</b> — 위험점수 70 이상 의심거래 보고 (특금법 제4조)</span>
        </div>

        {loading ? (
          <div className="empty">불러오는 중...</div>
        ) : reports.length === 0 ? (
          <div className="empty">준법감시 보고서가 없습니다. 거래를 생성하면 자동으로 생성됩니다.</div>
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
                {canSubmit && <th>제출</th>}
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
                      <span className={`badge ${report.status === 'SUBMITTED' ? 'badge-success' : 'badge-warning'}`}>
                        {report.status === 'PENDING' ? '제출 대기' : '제출 완료'}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: '#475569' }}>{info.law}</td>
                    <td style={{ color: '#94a3b8', fontSize: 12 }}>{fmtDate(report.created_at)}</td>
                    {canSubmit && (
                      <td>
                        {report.status === 'PENDING' ? (
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
            <p style={{ fontWeight: 600, color: '#1e3a5f', marginBottom: 8 }}>CTR (고액현금거래보고)</p>
            <ul style={{ lineHeight: 1.8, paddingLeft: 16 }}>
              <li>근거: 특정금융정보법 제4조의2</li>
              <li>기준: 1거래일 합산 1천만원 초과 현금거래</li>
              <li>제출처: 금융정보분석원 (KoFIU)</li>
              <li>제출기한: 거래 익영업일 이내</li>
            </ul>
          </div>
          <div>
            <p style={{ fontWeight: 600, color: '#dc2626', marginBottom: 8 }}>STR (의심거래보고)</p>
            <ul style={{ lineHeight: 1.8, paddingLeft: 16 }}>
              <li>근거: 특정금융정보법 제4조</li>
              <li>기준: 자금세탁·불법재산 의심거래</li>
              <li>본 시스템: 위험점수 70 이상 자동 탐지</li>
              <li>제출기한: 의심 인식 후 3영업일 이내</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}

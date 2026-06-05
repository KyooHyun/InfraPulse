import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'

function fmtDate(dt) {
  return new Date(dt).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
}

const ALERT_TYPE_LABEL = {
  HIGH_VALUE: '고액거래',
  VELOCITY: '단기 다건',
  FAILURE_RATE: '실패율 과다',
  LOGIN_FAILURE: '로그인 실패',
  LATENCY: '지연 이상',
}

export default function FdsAlerts({ user }) {
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(true)
  const [reviewing, setReviewing] = useState(null)
  const [decision, setDecision] = useState('APPROVE')
  const [memo, setMemo] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [msg, setMsg] = useState(null)

  const canReview = user?.role === 'RISK_OFFICER' || user?.role === 'ADMIN'

  const load = useCallback(async () => {
    try {
      const data = await apiFetch('/fds/alerts')
      setAlerts(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  function openReview(alert) {
    setReviewing(alert)
    setDecision('APPROVE')
    setMemo('')
    setMsg(null)
  }

  async function submitReview(e) {
    e.preventDefault()
    setSubmitting(true)
    try {
      await apiFetch(`/fds/alerts/${reviewing.id}/review`, {
        method: 'POST',
        body: JSON.stringify({ decision, memo }),
      })
      setMsg({ type: 'success', text: `알림 #${reviewing.id} — ${decision === 'APPROVE' ? '승인' : '기각'} 처리 완료` })
      setReviewing(null)
      load()
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally {
      setSubmitting(false)
    }
  }

  const pending = alerts.filter(a => a.status === 'PENDING').length
  const high = alerts.filter(a => (a.risk_score ?? 0) >= 70).length

  return (
    <div className="container">
      {msg && (
        <div className={`alert alert-${msg.type === 'success' ? 'success' : 'error'}`}>
          {msg.text}
        </div>
      )}

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">전체 알림</div>
          <div className="stat-value">{alerts.length}</div>
        </div>
        <div className="stat-card amber">
          <div className="stat-label">검토 대기</div>
          <div className="stat-value">{pending}</div>
        </div>
        <div className="stat-card red">
          <div className="stat-label">고위험</div>
          <div className="stat-value">{high}</div>
        </div>
        <div className="stat-card green">
          <div className="stat-label">처리 완료</div>
          <div className="stat-value">{alerts.length - pending}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">
          🚨 FDS 이상거래 알림
          <span>전자금융감독규정 제37조의2</span>
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn btn-ghost btn-sm" onClick={load}>새로고침</button>
          </div>
        </div>

        {loading ? (
          <div className="empty">불러오는 중...</div>
        ) : alerts.length === 0 ? (
          <div className="empty">이상거래 알림이 없습니다.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>거래 ID</th>
                <th>알림 유형</th>
                <th>위험점수</th>
                <th>상태</th>
                <th>탐지 시각</th>
                {canReview && <th>검토</th>}
              </tr>
            </thead>
            <tbody>
              {alerts.map(alert => (
                <tr key={alert.id}>
                  <td style={{ color: '#94a3b8', fontSize: 12 }}>#{alert.id}</td>
                  <td style={{ color: '#94a3b8', fontSize: 12 }}>TX#{alert.transaction_id ?? '-'}</td>
                  <td>
                    <span className="badge badge-warning">
                      {ALERT_TYPE_LABEL[alert.alert_type] || alert.alert_type}
                    </span>
                  </td>
                  <td>
                    {alert.risk_score != null ? (
                      <span className={alert.risk_score >= 70 ? 'risk-high' : alert.risk_score >= 40 ? 'risk-medium' : 'risk-low'}>
                        {alert.risk_score}
                      </span>
                    ) : '-'}
                  </td>
                  <td>
                    <span className={`badge ${alert.status === 'APPROVED' ? 'badge-success' : alert.status === 'REJECTED' ? 'badge-danger' : 'badge-warning'}`}>
                      {alert.status === 'PENDING' ? '검토대기' : alert.status === 'APPROVED' ? '승인' : '기각'}
                    </span>
                  </td>
                  <td style={{ color: '#94a3b8', fontSize: 12 }}>{fmtDate(alert.created_at)}</td>
                  {canReview && (
                    <td>
                      {alert.status === 'PENDING' ? (
                        <button className="btn btn-primary btn-sm" onClick={() => openReview(alert)}>
                          검토
                        </button>
                      ) : (
                        <span style={{ color: '#94a3b8', fontSize: 12 }}>완료</span>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {reviewing && (
        <div className="modal-overlay" onClick={() => setReviewing(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-title">🔍 FDS 알림 검토 — #{reviewing.id}</div>

            <div style={{ background: '#f8fafc', borderRadius: 6, padding: '12px 14px', marginBottom: 16, fontSize: 13 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <div><span style={{ color: '#94a3b8' }}>유형</span><br /><b>{ALERT_TYPE_LABEL[reviewing.alert_type] || reviewing.alert_type}</b></div>
                <div><span style={{ color: '#94a3b8' }}>위험점수</span><br />
                  <b className={reviewing.risk_score >= 70 ? 'risk-high' : reviewing.risk_score >= 40 ? 'risk-medium' : 'risk-low'}>
                    {reviewing.risk_score ?? '-'}
                  </b>
                </div>
              </div>
            </div>

            <form onSubmit={submitReview}>
              <div className="form-group">
                <label>검토 결정</label>
                <select value={decision} onChange={e => setDecision(e.target.value)}>
                  <option value="APPROVE">승인 (정상거래 확인)</option>
                  <option value="REJECT">기각 (이상거래 확정)</option>
                </select>
              </div>
              <div className="form-group">
                <label>검토 의견 (선택)</label>
                <textarea rows={3} value={memo} onChange={e => setMemo(e.target.value)} placeholder="검토 사유 입력..." />
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-ghost" onClick={() => setReviewing(null)}>취소</button>
                <button
                  type="submit"
                  className={`btn ${decision === 'APPROVE' ? 'btn-success' : 'btn-danger'}`}
                  disabled={submitting}
                >
                  {submitting ? '처리중...' : decision === 'APPROVE' ? '승인 처리' : '기각 처리'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

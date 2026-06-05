import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../api'

const CURRENCIES = ['KRW', 'USD', 'EUR', 'JPY']

function fmtAmt(amount, currency) {
  if (currency === 'KRW') return amount.toLocaleString('ko-KR') + ' 원'
  return amount.toLocaleString('en-US', { minimumFractionDigits: 2 }) + ' ' + currency
}

function fmtDate(dt) {
  return new Date(dt).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
}

function RiskBadge({ score }) {
  if (score == null) return <span className="badge badge-gray">-</span>
  if (score >= 70) return <span className="badge badge-danger">{score} HIGH</span>
  if (score >= 40) return <span className="badge badge-warning">{score} MED</span>
  return <span className="badge badge-success">{score} LOW</span>
}

export default function Transactions() {
  const [txs, setTxs] = useState([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState({ account_from: '', account_to: '', amount: '', currency: 'KRW', description: '' })
  const [submitting, setSubmitting] = useState(false)
  const [msg, setMsg] = useState(null)

  const load = useCallback(async () => {
    try {
      const data = await apiFetch('/transactions')
      setTxs(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  function openModal() {
    setForm({ account_from: '', account_to: '', amount: '', currency: 'KRW', description: '' })
    setMsg(null)
    setShowModal(true)
  }

  async function handleTransfer(e) {
    e.preventDefault()
    setSubmitting(true)
    setMsg(null)
    try {
      const body = {
        account_from: form.account_from,
        account_to: form.account_to,
        amount: parseFloat(form.amount),
        currency: form.currency,
        description: form.description || null,
      }
      const result = await apiFetch('/transactions/transfer', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const risk = result.risk_score ?? 0
      const level = risk >= 70 ? 'HIGH' : risk >= 40 ? 'MEDIUM' : 'LOW'
      setMsg({ type: 'success', text: `거래 완료 — 위험점수 ${risk} (${level})` })
      setShowModal(false)
      load()
    } catch (err) {
      setMsg({ type: 'error', text: err.message })
    } finally {
      setSubmitting(false)
    }
  }

  const stats = {
    total: txs.length,
    high: txs.filter(t => (t.risk_score ?? 0) >= 70).length,
    medium: txs.filter(t => { const s = t.risk_score ?? 0; return s >= 40 && s < 70 }).length,
  }

  return (
    <div className="container">
      {msg && (
        <div className={`alert alert-${msg.type === 'success' ? 'success' : 'error'}`}>
          {msg.text}
        </div>
      )}

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">전체 거래</div>
          <div className="stat-value">{stats.total}</div>
        </div>
        <div className="stat-card red">
          <div className="stat-label">고위험 거래</div>
          <div className="stat-value">{stats.high}</div>
        </div>
        <div className="stat-card amber">
          <div className="stat-label">중위험 거래</div>
          <div className="stat-value">{stats.medium}</div>
        </div>
        <div className="stat-card green">
          <div className="stat-label">정상 거래</div>
          <div className="stat-value">{stats.total - stats.high - stats.medium}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">
          💳 거래 내역
          <span>최근 100건</span>
          <div style={{ marginLeft: 'auto' }}>
            <button className="btn btn-primary btn-sm" onClick={openModal}>+ 거래 생성</button>
            <button className="btn btn-ghost btn-sm" onClick={load} style={{ marginLeft: 8 }}>새로고침</button>
          </div>
        </div>

        {loading ? (
          <div className="empty">불러오는 중...</div>
        ) : txs.length === 0 ? (
          <div className="empty">거래 내역이 없습니다. 거래를 생성해보세요.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>출금계좌</th>
                <th>입금계좌</th>
                <th>금액</th>
                <th>상태</th>
                <th>위험점수</th>
                <th>일시</th>
              </tr>
            </thead>
            <tbody>
              {txs.map(tx => (
                <tr key={tx.id}>
                  <td style={{ color: '#94a3b8', fontSize: 12 }}>#{tx.id}</td>
                  <td><code style={{ fontSize: 12 }}>{tx.account_from}</code></td>
                  <td><code style={{ fontSize: 12 }}>{tx.account_to}</code></td>
                  <td style={{ fontWeight: 600 }}>{fmtAmt(tx.amount, tx.currency)}</td>
                  <td>
                    <span className={`badge ${tx.status === 'COMPLETED' ? 'badge-success' : tx.status === 'FAILED' ? 'badge-danger' : 'badge-gray'}`}>
                      {tx.status}
                    </span>
                  </td>
                  <td><RiskBadge score={tx.risk_score} /></td>
                  <td style={{ color: '#94a3b8', fontSize: 12 }}>{fmtDate(tx.timestamp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-title">💸 이체 거래 생성</div>
            {msg && msg.type === 'error' && <div className="alert alert-error">{msg.text}</div>}
            <form onSubmit={handleTransfer}>
              <div className="form-group">
                <label>출금 계좌번호</label>
                <input value={form.account_from} onChange={e => setForm(f => ({ ...f, account_from: e.target.value }))} placeholder="예: ACC-001" required />
              </div>
              <div className="form-group">
                <label>입금 계좌번호</label>
                <input value={form.account_to} onChange={e => setForm(f => ({ ...f, account_to: e.target.value }))} placeholder="예: ACC-002" required />
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 120px', gap: 8 }}>
                <div className="form-group">
                  <label>금액</label>
                  <input type="number" min="0.01" step="0.01" value={form.amount} onChange={e => setForm(f => ({ ...f, amount: e.target.value }))} placeholder="0" required />
                </div>
                <div className="form-group">
                  <label>통화</label>
                  <select value={form.currency} onChange={e => setForm(f => ({ ...f, currency: e.target.value }))}>
                    {CURRENCIES.map(c => <option key={c}>{c}</option>)}
                  </select>
                </div>
              </div>
              <div className="form-group">
                <label>적요 (선택)</label>
                <input value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="거래 사유" />
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-ghost" onClick={() => setShowModal(false)}>취소</button>
                <button type="submit" className="btn btn-primary" disabled={submitting}>
                  {submitting ? '처리중...' : '이체 실행'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

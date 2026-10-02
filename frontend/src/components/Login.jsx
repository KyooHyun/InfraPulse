import { useState } from 'react'
import { login } from '../api'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const data = await login(username, password)
      localStorage.setItem('token', data.access_token)
      onLogin(data.access_token)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(135deg, #0f2444 0%, #1e3a5f 60%, #2a5298 100%)',
    }}>
      <div style={{ width: 380 }}>
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <div style={{ fontSize: 48, marginBottom: 12 }}>🏦</div>
          <h1 style={{ color: 'white', fontSize: 20, fontWeight: 700, letterSpacing: '-0.3px' }}>
            FDS 이상금융거래탐지시스템
          </h1>
          <p style={{ color: 'rgba(255,255,255,.5)', fontSize: 12, marginTop: 6 }}>
            전자금융감독규정 제37조의2 준거
          </p>
        </div>

        <div className="card" style={{ padding: 28 }}>
          <form onSubmit={handleSubmit}>
            {error && <div className="alert alert-error">{error}</div>}

            <div className="form-group">
              <label>사용자 ID</label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                placeholder="아이디 입력"
                autoFocus
                required
              />
            </div>

            <div className="form-group">
              <label>비밀번호</label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="비밀번호 입력"
                required
              />
            </div>

            <button
              type="submit"
              className="btn btn-primary"
              style={{ width: '100%', padding: '11px', marginTop: 6, fontSize: 14 }}
              disabled={loading}
            >
              {loading ? '로그인 중...' : '로그인'}
            </button>
          </form>

          <div style={{ marginTop: 20, padding: '12px 14px', background: '#f8fafc', borderRadius: 6, fontSize: 12, color: '#64748b' }}>
            <p style={{ fontWeight: 600, marginBottom: 6, color: '#475569' }}>테스트 계정</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span><b>admin</b> / Admin1234! &nbsp;(관리자)</span>
              <span><b>risk_officer</b> / Risk1234! &nbsp;(리스크 담당)</span>
              <span><b>staff</b> / Staff1234! &nbsp;(일반 직원)</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

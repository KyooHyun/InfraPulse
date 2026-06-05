const BASE = 'http://localhost:8000'

export async function login(username, password) {
  const resp = await fetch(`${BASE}/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username, password }).toString(),
  })
  if (!resp.ok) throw new Error('로그인 실패')
  return resp.json()
}

export async function getMe(token) {
  const resp = await fetch(`${BASE}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!resp.ok) throw new Error('사용자 정보 조회 실패')
  return resp.json()
}

export async function apiFetch(path, options = {}) {
  const token = localStorage.getItem('token')
  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  })
  if (resp.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.location.reload()
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err.detail || `오류 ${resp.status}`)
  }
  return resp.json()
}

const ROLE_LABEL = {
  ADMIN: '관리자',
  RISK_OFFICER: '리스크 담당',
  STAFF: '일반 직원',
}

const NAV_ITEMS = [
  { key: 'transactions', label: '거래 내역', icon: '💳' },
  { key: 'fds',          label: 'FDS 알림',  icon: '🚨' },
  { key: 'compliance',   label: '준법 보고',  icon: '📋' },
]

export default function Navbar({ user, page, onNav, onLogout }) {
  return (
    <nav style={{
      background: '#0f2444',
      color: 'white',
      display: 'flex',
      alignItems: 'center',
      padding: '0 24px',
      height: 52,
      position: 'sticky',
      top: 0,
      zIndex: 100,
      boxShadow: '0 2px 8px rgba(0,0,0,.25)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginRight: 32, cursor: 'pointer' }} onClick={() => onNav('transactions')}>
        <span style={{ fontSize: 20 }}>🏦</span>
        <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.3px' }}>FDS Portal</span>
      </div>

      <div style={{ display: 'flex', gap: 4, flex: 1 }}>
        {NAV_ITEMS.map(item => (
          <button
            key={item.key}
            onClick={() => onNav(item.key)}
            style={{
              background: page === item.key ? 'rgba(255,255,255,.12)' : 'transparent',
              border: 'none',
              color: page === item.key ? 'white' : 'rgba(255,255,255,.6)',
              padding: '6px 14px',
              borderRadius: 6,
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: page === item.key ? 600 : 400,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              transition: 'all .15s',
            }}
          >
            {item.icon} {item.label}
          </button>
        ))}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{user?.username}</div>
          <div style={{ fontSize: 11, color: 'rgba(255,255,255,.5)' }}>
            {ROLE_LABEL[user?.role] || user?.role}
          </div>
        </div>
        <button
          onClick={onLogout}
          style={{
            background: 'rgba(255,255,255,.1)',
            border: '1px solid rgba(255,255,255,.15)',
            color: 'rgba(255,255,255,.8)',
            padding: '5px 12px',
            borderRadius: 6,
            cursor: 'pointer',
            fontSize: 12,
          }}
        >
          로그아웃
        </button>
      </div>
    </nav>
  )
}

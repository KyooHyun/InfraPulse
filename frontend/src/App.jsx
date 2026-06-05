import { useState, useEffect } from 'react'
import { getMe } from './api'
import Login from './components/Login'
import Navbar from './components/Navbar'
import Transactions from './components/Transactions'
import FdsAlerts from './components/FdsAlerts'
import Compliance from './components/Compliance'

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem('token'))
  const [user, setUser] = useState(null)
  const [page, setPage] = useState('transactions')

  useEffect(() => {
    if (!token) { setUser(null); return }
    getMe(token)
      .then(setUser)
      .catch(() => {
        localStorage.removeItem('token')
        setToken(null)
      })
  }, [token])

  function handleLogin(newToken) {
    setToken(newToken)
  }

  function handleLogout() {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    setToken(null)
    setUser(null)
  }

  if (!token) return <Login onLogin={handleLogin} />

  return (
    <>
      <Navbar user={user} page={page} onNav={setPage} onLogout={handleLogout} />
      {page === 'transactions' && <Transactions />}
      {page === 'fds'          && <FdsAlerts user={user} />}
      {page === 'compliance'   && <Compliance user={user} />}
    </>
  )
}

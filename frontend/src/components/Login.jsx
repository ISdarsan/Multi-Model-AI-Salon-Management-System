import { useState } from 'react'
import { supabase } from '../lib/supabase'
import { Button, Card } from './ui'

// The sign-in screen: email + password. Supabase keeps the session after a refresh.
export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function signIn(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const { error } = await supabase.auth.signInWithPassword({ email, password })
    if (error) setError(error.message)
    setBusy(false) // on success the App notices the new session by itself
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm p-8">
        <h1 className="text-2xl font-semibold text-slate-900">SalonOS</h1>
        <p className="mt-1 text-sm text-slate-500">Manager sign-in</p>

        <form onSubmit={signIn} className="mt-6 space-y-4">
          <label className="block text-sm font-medium text-slate-700">
            Email
            <input
              type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-200"
            />
          </label>
          <label className="block text-sm font-medium text-slate-700">
            Password
            <input
              type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-200"
            />
          </label>
          {error && <p className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
          <Button type="submit" variant="primary" disabled={busy} className="w-full">
            {busy ? 'Signing in...' : 'Sign in'}
          </Button>
        </form>
      </Card>
    </div>
  )
}

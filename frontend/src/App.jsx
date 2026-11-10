import { useEffect, useState } from 'react'
import { configMissing, supabase } from './lib/supabase'
import Login from './components/Login'
import { Button, Card, ErrorBox, Loading } from './components/ui'
import CommandCentre from './screens/CommandCentre'
import ApprovalInbox from './screens/ApprovalInbox'
import DecisionLog from './screens/DecisionLog'

// The three screens in the sidebar.
const SCREENS = [
  { id: 'command', label: 'Command Centre', icon: '◧', component: CommandCentre },
  { id: 'inbox', label: 'Approval Inbox', icon: '✓', component: ApprovalInbox },
  { id: 'log', label: 'Decision Log', icon: '≡', component: DecisionLog },
]

// A centred message with a sign-out button (used when the user may not see the dashboard).
function Notice({ title, text, children }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md p-8 text-center">
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {text && <p className="mt-2 text-sm text-slate-500">{text}</p>}
        <div className="mt-6">{children}</div>
      </Card>
    </div>
  )
}

const signOut = () => supabase.auth.signOut()

export default function App() {
  const [session, setSession] = useState(undefined) // undefined = still checking, null = signed out
  const [profile, setProfile] = useState(undefined) // undefined = loading
  const [profileError, setProfileError] = useState(null)
  const [screenId, setScreenId] = useState('command')

  // 1. Find out if someone is signed in, and keep watching (sign in, sign out, refresh).
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session))
    const { data } = supabase.auth.onAuthStateChange((_event, newSession) => setSession(newSession))
    return () => data.subscription.unsubscribe()
  }, [])

  // 2. When someone is signed in, load their profile to learn their role.
  const userId = session?.user?.id
  useEffect(() => {
    if (!userId) {
      setProfile(undefined)
      return
    }
    setProfileError(null)
    supabase.from('profiles').select('role, full_name').eq('id', userId).single().then(({ data, error }) => {
      if (error) setProfileError(error.message)
      else setProfile(data)
    })
  }, [userId])

  if (configMissing) {
    return (
      <Notice title="Missing settings" text="frontend/.env needs VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY. Copy frontend/.env.example to frontend/.env, fill it in, and restart the dashboard." />
    )
  }
  if (session === undefined) return <Loading text="Starting..." />
  if (!session) return <Login />

  if (profileError) {
    return (
      <Notice title="Could not load your profile">
        <ErrorBox message={profileError} />
        <Button onClick={signOut}>Sign out</Button>
      </Notice>
    )
  }
  if (profile === undefined) return <Loading text="Checking your access..." />

  // Only managers may use the dashboard.
  if (profile.role !== 'manager') {
    return (
      <Notice title="This area is for managers only" text={`You are signed in as ${session.user.email}.`}>
        <Button onClick={signOut}>Sign out</Button>
      </Notice>
    )
  }

  const Screen = SCREENS.find((s) => s.id === screenId).component
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="px-6 py-5">
          <p className="text-lg font-semibold text-slate-900">SalonOS</p>
          <p className="text-xs text-slate-500">Manager dashboard</p>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          {SCREENS.map((s) => (
            <button
              key={s.id}
              onClick={() => setScreenId(s.id)}
              className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm font-medium transition ${
                s.id === screenId ? 'bg-indigo-50 text-indigo-700' : 'text-slate-600 hover:bg-slate-50'
              }`}
            >
              <span className="w-4 text-center">{s.icon}</span>
              {s.label}
            </button>
          ))}
        </nav>
        <div className="border-t border-slate-200 p-4">
          <p className="truncate text-sm font-medium text-slate-800">{profile.full_name || session.user.email}</p>
          <p className="truncate text-xs text-slate-500">{session.user.email}</p>
          <Button onClick={signOut} className="mt-3 w-full">Sign out</Button>
        </div>
      </aside>

      <main className="min-w-0 flex-1 p-8">
        <Screen />
      </main>
    </div>
  )
}

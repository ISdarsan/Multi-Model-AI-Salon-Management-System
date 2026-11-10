import { useEffect, useState } from 'react'
import { must, useLiveData } from '../hooks/useLiveData'
import { supabase } from '../lib/supabase'
import { approveApproval, rejectApproval } from '../lib/api'
import { PROPOSAL_OUTCOME, agentName, describeAction, timeAgo, fmtDateTime } from '../lib/format'
import { Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader } from '../components/ui'

const TABLES = ['approvals', 'agent_actions', 'events']

// Read the pending approvals, with everything needed to explain each one.
async function load() {
  // 1. Pending approvals, each with its agent action and the event that caused it.
  const pending = must(
    await supabase.from('approvals').select('*, agent_actions(*, events(id, type, source, created_at))').eq('status', 'pending'),
  ).data
  if (pending.length === 0) return { groups: [] }

  const eventIds = [...new Set(pending.map((a) => a.agent_actions.event_id))]

  // 2. All actions and log rows of those events (to find each proposal's reasoning), and product names.
  const [actions, logs, products] = (
    await Promise.all([
      supabase.from('agent_actions').select('id, event_id, tool_called, created_at').in('event_id', eventIds).order('created_at'),
      supabase.from('decision_log').select('event_id, outcome, reasoning, created_at').in('event_id', eventIds).order('created_at'),
      supabase.from('products').select('sku, name'),
    ])
  ).map(must)
  const productNames = Object.fromEntries(products.data.map((p) => [p.sku, p.name]))

  // 3. Find the reasoning of each proposal. A log row is not linked to one action in the database, but an agent
  // writes the action and its log row one after the other, so the Nth proposal of a kind matches the Nth log row.
  const reasoningByAction = {}
  for (const eventId of eventIds) {
    for (const [tool, outcome] of Object.entries(PROPOSAL_OUTCOME)) {
      const toolActions = actions.data.filter((a) => a.event_id === eventId && a.tool_called === tool)
      const toolLogs = logs.data.filter((l) => l.event_id === eventId && l.outcome === outcome)
      toolActions.forEach((a, index) => {
        if (toolLogs[index]) reasoningByAction[a.id] = toolLogs[index].reasoning
      })
    }
  }

  // 4. Group the cards by the event that caused them (newest event first).
  const byEvent = {}
  for (const approval of pending) {
    const action = approval.agent_actions
    const group = (byEvent[action.event_id] ||= { event: action.events, items: [] })
    group.items.push({ approval, action, products: productNames, reasoning: reasoningByAction[action.id] })
  }
  const groups = Object.values(byEvent)
  groups.forEach((g) => g.items.sort((a, b) => new Date(a.action.created_at) - new Date(b.action.created_at)))
  groups.sort((a, b) => new Date(b.event.created_at) - new Date(a.event.created_at))
  return { groups }
}

// One proposal with its Approve and Reject buttons.
function ApprovalCard({ item, onDone }) {
  const { approval, action, products, reasoning } = item
  const words = describeAction(action, products)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [rejecting, setRejecting] = useState(false) // is the "why reject?" box open?
  const [reason, setReason] = useState('')

  // Run approve or reject: disable the buttons, then show the result (or the error).
  async function run(job) {
    setBusy(true)
    setError(null)
    try {
      const result = await job()
      onDone(`${words.title}: ${result.outcome}.`)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="indigo">{agentName(action.agent_name)}</Badge>
        <Badge tone={action.risk_tier}>{action.risk_tier} risk</Badge>
      </div>

      <h3 className="mt-3 text-base font-semibold text-slate-900">{words.title}</h3>
      <p className="mt-1 text-sm text-slate-600">{words.detail}</p>

      <div className="mt-3 rounded-lg bg-slate-50 p-3 text-sm text-slate-600">
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Why</p>
        {reasoning || 'No written reasoning was found for this proposal.'}
      </div>

      {error && <p className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {rejecting ? (
        <div className="mt-4 space-y-3">
          <label className="block text-sm font-medium text-slate-700">
            Why are you rejecting this? (required)
            <textarea
              autoFocus rows={2} value={reason} onChange={(e) => setReason(e.target.value)} disabled={busy}
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-red-400 focus:ring-2 focus:ring-red-100"
            />
          </label>
          <div className="flex gap-2">
            <Button variant="danger" disabled={busy || !reason.trim()} onClick={() => run(() => rejectApproval(approval.id, reason.trim()))}>
              {busy ? 'Rejecting...' : 'Confirm reject'}
            </Button>
            <Button disabled={busy} onClick={() => { setRejecting(false); setReason(''); setError(null) }}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex gap-2">
          <Button variant="primary" disabled={busy} onClick={() => run(() => approveApproval(approval.id))}>
            {busy ? 'Working...' : 'Approve'}
          </Button>
          <Button variant="danger" disabled={busy} onClick={() => setRejecting(true)}>Reject</Button>
        </div>
      )}
    </Card>
  )
}

export default function ApprovalInbox() {
  const { data, loading, error, refresh } = useLiveData(load, TABLES)
  const [notice, setNotice] = useState(null) // the green "done" message at the top

  // Hide the message after a few seconds.
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 7000)
    return () => clearTimeout(timer)
  }, [notice])

  function done(message) {
    setNotice(message)
    refresh() // the decided card disappears from the list
  }

  return (
    <div>
      <PageHeader title="Approval Inbox" subtitle="Proposals from the agents that wait for your decision. Nothing happens until you approve." />
      {notice && <p className="mb-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">✓ {notice}</p>}
      {error && <ErrorBox message={error} onRetry={refresh} />}
      {loading && !data && <Loading />}

      {data && data.groups.length === 0 && (
        <Empty title="Nothing waiting for you" text="New proposals from the agents will appear here by themselves." />
      )}

      <div className="space-y-8">
        {data?.groups.map((group) => (
          <section key={group.event.id}>
            <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
                Event: {group.event.type.replace('_', ' ')}
              </h2>
              <span className="text-xs text-slate-400" title={fmtDateTime(group.event.created_at)}>
                from {group.event.source} · {timeAgo(group.event.created_at)} · {group.items.length} proposal{group.items.length === 1 ? '' : 's'}
              </span>
            </div>
            <div className="space-y-3">
              {group.items.map((item) => <ApprovalCard key={item.approval.id} item={item} onDone={done} />)}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}

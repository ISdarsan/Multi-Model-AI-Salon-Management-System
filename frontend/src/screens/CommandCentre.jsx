import { must, useLiveData } from '../hooks/useLiveData'
import { supabase } from '../lib/supabase'
import { agentName, fmtDateTime, timeAgo } from '../lib/format'
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader } from '../components/ui'

// The database tables this screen listens to for live updates.
// (decision_log is not in Realtime, so the 5-second refresh picks up its new rows.)
const TABLES = ['events', 'agent_actions', 'approvals', 'appointments']

// Read everything this screen needs in one go.
async function load() {
  // "Today" means today in UTC, like the rest of the salon's data.
  const now = new Date()
  const dayStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))
  const dayEnd = new Date(dayStart.getTime() + 24 * 3600 * 1000)
  const count = { count: 'exact', head: true } // ask only for a number, not the rows

  const [pending, stock, unhandled, failed, today, actions, logs] = (
    await Promise.all([
      supabase.from('approvals').select('id', count).eq('status', 'pending'),
      supabase.from('stock_levels').select('current_qty, products(sku, name, reorder_threshold)'),
      supabase.from('events').select('id', count).in('status', ['new', 'processing']),
      supabase.from('events').select('id', count).eq('status', 'failed'),
      supabase.from('appointments').select('id', count).neq('status', 'cancelled')
        .gte('start_time', dayStart.toISOString()).lt('start_time', dayEnd.toISOString()),
      supabase.from('agent_actions').select('id, agent_name, tool_called, status, risk_tier, created_at')
        .order('created_at', { ascending: false }).limit(12),
      supabase.from('decision_log').select('id, agents_involved, outcome, reasoning, created_at')
        .order('created_at', { ascending: false }).limit(12),
    ])
  ).map(must)

  // A product is "below its reorder threshold" when the stock is lower than the threshold.
  const lowProducts = stock.data.filter((row) => row.current_qty < row.products.reorder_threshold)

  // Mix the latest actions and log rows into one feed, newest first.
  const feed = [
    ...actions.data.map((a) => ({ kind: 'action', id: 'a' + a.id, time: a.created_at, ...a })),
    ...logs.data.map((l) => ({ kind: 'log', id: 'l' + l.id, time: l.created_at, ...l })),
  ].sort((a, b) => new Date(b.time) - new Date(a.time)).slice(0, 15)

  return {
    pending: pending.count,
    low: lowProducts.length,
    unhandled: unhandled.count,
    failed: failed.count,
    today: today.count,
    feed,
  }
}

// One number in a card.
function Kpi({ label, value, hint, tone }) {
  return (
    <Card className="p-5">
      <p className="text-sm text-slate-500">{label}</p>
      <p className={`mt-2 text-3xl font-semibold ${tone === 'alert' && value > 0 ? 'text-amber-600' : 'text-slate-900'}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </Card>
  )
}

// One line in the live feed.
function FeedItem({ item }) {
  if (item.kind === 'action') {
    const tone = item.risk_tier
    return (
      <li className="flex items-start gap-3 py-3">
        <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-indigo-500" />
        <div className="min-w-0 flex-1">
          <p className="text-sm text-slate-800">
            <span className="font-medium">{agentName(item.agent_name)}</span> used <span className="font-mono text-xs">{item.tool_called}</span>
            {' '}<Badge tone={tone}>{item.risk_tier} risk</Badge> <Badge>{item.status.replace('_', ' ')}</Badge>
          </p>
        </div>
        <span className="shrink-0 text-xs text-slate-400" title={fmtDateTime(item.time)}>{timeAgo(item.time)}</span>
      </li>
    )
  }
  return (
    <li className="flex items-start gap-3 py-3">
      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-slate-400" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-slate-800">{item.outcome}</p>
        <p className="truncate text-sm text-slate-500">{item.reasoning}</p>
      </div>
      <span className="shrink-0 text-xs text-slate-400" title={fmtDateTime(item.time)}>{timeAgo(item.time)}</span>
    </li>
  )
}

export default function CommandCentre() {
  const { data, loading, error, refresh } = useLiveData(load, TABLES)

  return (
    <div>
      <PageHeader title="Command Centre" subtitle="What is happening in the salon right now. Updates live." />
      {error && <ErrorBox message={error} onRetry={refresh} />}
      {loading && !data && <Loading />}

      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi label="Pending approvals" value={data.pending} hint="Waiting for you" tone="alert" />
            <Kpi label="Products below reorder level" value={data.low} hint="Stock under the threshold" tone="alert" />
            <Kpi label="Unhandled events" value={data.unhandled}
              hint={data.failed ? `${data.failed} more failed earlier` : 'New or being processed'} tone="alert" />
            <Kpi label="Appointments today" value={data.today} hint="Not cancelled, UTC day" />
          </div>

          <h2 className="mb-3 mt-8 text-lg font-semibold text-slate-900">Live agent activity</h2>
          {data.feed.length === 0 ? (
            <Empty title="No activity yet" text="When an event arrives, what the agents do will appear here." />
          ) : (
            <Card className="px-5">
              <ul className="divide-y divide-slate-100">
                {data.feed.map((item) => <FeedItem key={item.id} item={item} />)}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  )
}

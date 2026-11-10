import { Fragment, useState } from 'react'
import { must, useLiveData } from '../hooks/useLiveData'
import { supabase } from '../lib/supabase'
import { agentName, fmtDateTime } from '../lib/format'
import { Card, Empty, ErrorBox, Loading, PageHeader } from '../components/ui'

// decision_log is not in Realtime, so this screen relies on the 5-second refresh plus these tables.
const TABLES = ['events', 'approvals']

// The latest 300 log rows with the type of the event each belongs to.
async function load() {
  const result = must(
    await supabase.from('decision_log').select('id, created_at, agents_involved, outcome, reasoning, events(type, source)')
      .order('created_at', { ascending: false }).limit(300),
  )
  return result.data
}

export default function DecisionLog() {
  const { data, loading, error, refresh } = useLiveData(load, TABLES)
  const [filter, setFilter] = useState('all') // the outcome chosen in the filter
  const [openId, setOpenId] = useState(null)  // the row whose reasoning is shown

  // The outcomes found in the data, for the filter list.
  const outcomes = data ? [...new Set(data.map((row) => row.outcome))].sort() : []
  const rows = data ? data.filter((row) => filter === 'all' || row.outcome === filter) : []

  return (
    <div>
      <PageHeader
        title="Decision Log"
        subtitle="Every decision the agents and managers made, newest first. Click a row to read the reasoning."
        right={
          <select
            value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by outcome"
            className="max-w-xs rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
          >
            <option value="all">All outcomes</option>
            {outcomes.map((outcome) => <option key={outcome} value={outcome}>{outcome}</option>)}
          </select>
        }
      />
      {error && <ErrorBox message={error} onRetry={refresh} />}
      {loading && !data && <Loading />}

      {data && rows.length === 0 && (
        <Empty title={data.length === 0 ? 'The log is empty' : 'No rows match this filter'}
          text={data.length === 0 ? 'Decisions will appear here as the agents work.' : 'Choose "All outcomes" to see everything.'} />
      )}

      {rows.length > 0 && (
        <Card className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-3 font-medium">Time</th>
                <th className="px-4 py-3 font-medium">Event</th>
                <th className="px-4 py-3 font-medium">Agents</th>
                <th className="px-4 py-3 font-medium">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Fragment key={row.id}>
                  <tr
                    onClick={() => setOpenId(openId === row.id ? null : row.id)}
                    className="cursor-pointer border-b border-slate-100 hover:bg-slate-50"
                  >
                    <td className="whitespace-nowrap px-4 py-3 text-slate-500">{fmtDateTime(row.created_at)}</td>
                    <td className="px-4 py-3">{row.events ? row.events.type.replace('_', ' ') : '-'}</td>
                    <td className="px-4 py-3 text-slate-600">{row.agents_involved.map(agentName).join(', ')}</td>
                    <td className="px-4 py-3 font-medium text-slate-800">{row.outcome}</td>
                  </tr>
                  {openId === row.id && (
                    <tr className="border-b border-slate-100 bg-slate-50">
                      <td colSpan={4} className="px-4 py-3 text-slate-600">
                        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Reasoning</p>
                        {row.reasoning}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}

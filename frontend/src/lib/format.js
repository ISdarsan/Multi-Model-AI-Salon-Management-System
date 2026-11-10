// Small helpers that turn database values into readable text.

// The salon's data is stored in UTC, so every time on the dashboard is shown in UTC.
export function fmtDateTime(iso) {
  if (!iso) return '-'
  return (
    new Date(iso).toLocaleString('en-GB', {
      timeZone: 'UTC', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    }) + ' UTC'
  )
}

// "5 min ago", "2 h ago"...
export function timeAgo(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

// Friendly names for the agents.
const AGENT_NAMES = {
  inventory_agent: 'Inventory Agent',
  scheduling_agent: 'Scheduling Agent',
  orchestrator: 'Orchestrator',
  cx_agent: 'CX Agent',
  quality_agent: 'Quality Agent',
}
export const agentName = (name) => AGENT_NAMES[name] || name

// Say what a proposed action will do, in plain words.
// `products` is a lookup { sku: product name } used for purchase orders.
export function describeAction(action, products = {}) {
  const input = action.input || {}
  const result = action.result || {}

  if (action.tool_called === 'create_draft_po') {
    const sku = result.sku || input.sku
    const name = products[sku] ? `${products[sku]} (${sku})` : sku
    return {
      title: `Order ${result.qty} × ${name}`,
      detail: `From supplier ${result.supplier}. Nothing is ordered until you approve.`,
    }
  }
  if (action.tool_called === 'reassign_staff') {
    return {
      title: `Move the ${input.service} at ${fmtDateTime(input.start_time)}`,
      detail: `From ${input.old_staff_name} to ${input.new_staff_name}. The appointment changes only when you approve.`,
    }
  }
  if (action.tool_called === 'hold_appointment') {
    return {
      title: `Put the ${input.service} at ${fmtDateTime(input.start_time)} on hold`,
      detail: `It needs ${input.units_needed} × ${input.sku}, but only ${input.stock_before_this_booking} would be left for it.`,
    }
  }
  if (action.tool_called === 'draft_reply') {
    return {
      title: `Send a reply to the customer who complained about the ${input.service || 'visit'}`,
      detail: `Draft: "${result.draft_text}" Nothing is sent to the customer, even if you approve. Approving resolves the ticket.`,
    }
  }
  if (action.tool_called === 'suggest_refund') {
    const kind = input.basis === 'Delay Compensation' ? 'compensation' : 'refund'
    return {
      title: `Give a ${kind} of ${Number(result.suggested_amount).toFixed(2)} (${input.percent}%) for the ${input.service}`,
      detail: `Under the ${input.basis}, because ${(input.reasons || []).join(' and ')}. Only a manager can decide this. Approving records your decision and resolves the ticket.`,
    }
  }
  if (action.tool_called === 'raise_audit_flag') {
    return {
      title: `Check the stock count of ${input.sku} by hand`,
      detail: `The camera counted ${input.observed_count} on the shelf, but the database says ${input.db_qty} (difference ${input.difference > 0 ? '+' : ''}${input.difference}). Approving marks the flag resolved. The stock number is NOT changed.`,
    }
  }
  if (action.tool_called === 'shift_appointment') {
    return {
      title: `Move the ${input.service} with ${input.staff_name} from ${fmtDateTime(input.old_start)} to ${fmtDateTime(input.new_start)}`,
      detail: `${input.minutes} minutes later, because the waiting area is overloaded (${input.waiting_count} waiting). The stylist has no other booking in that time. The appointment changes only when you approve.`,
    }
  }
  if (action.tool_called === 'draft_customer_notice') {
    const reason = input.kind === 'delay' ? 'the delay' : 'the change of stylist'
    return {
      title: `Tell the customer about ${reason} for the ${input.service}`,
      detail: `Draft: "${result.draft_text}" Nothing is sent to the customer, even if you approve.`,
    }
  }
  return { title: action.tool_called, detail: '' }
}

// Which decision_log outcome belongs to which kind of action (used to find an action's reasoning).
export const PROPOSAL_OUTCOME = {
  create_draft_po: 'draft PO created, waiting for approval',
  reassign_staff: 'reassignment proposed, waiting for approval',
  hold_appointment: 'clash found, hold proposed, waiting for approval',
  // A complaint writes ONE log row that explains both its reply and its refund suggestion.
  draft_reply: 'complaint handled, waiting for approval',
  suggest_refund: 'complaint handled, waiting for approval',
  raise_audit_flag: 'audit flag proposed, waiting for approval',
  shift_appointment: 'shift proposed, waiting for approval',
  draft_customer_notice: 'customer notice drafted, waiting for approval',
}

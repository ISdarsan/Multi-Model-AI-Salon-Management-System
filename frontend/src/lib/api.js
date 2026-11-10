import { supabase } from './supabase'

// The backend server (FastAPI). Approve and reject go through it, never straight to the database.
const API_URL = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000'

// Turn whatever the server said into one readable sentence.
function readableError(body, status) {
  const detail = body && body.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ')
  return `The server answered with an error (${status}).`
}

// Send a POST request with the manager's login token. Throws an Error with a clear message on failure.
async function post(path, body) {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) throw new Error('You are not signed in. Please sign in again.')

  let response
  try {
    response = await fetch(API_URL + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new Error(`Could not reach the server at ${API_URL}. Is it running?`)
  }

  const result = await response.json().catch(() => null)
  if (!response.ok) throw new Error(readableError(result, response.status))
  return result
}

export const approveApproval = (id) => post(`/approvals/${id}/approve`)
export const rejectApproval = (id, reason) => post(`/approvals/${id}/reject`, { reason })

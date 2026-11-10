"""
proposals.py - shared helpers for agents that write proposals.

Every consequential action follows the same pattern:
  1. an agent_actions row   (what the agent wants to do, status 'awaiting_approval')
  2. an approvals row       (the manager's inbox, status 'pending')
  3. a decision_log row     (why)
Nothing real changes until a manager approves (see backend/approvals/approval_actions.py).

Small, harmless actions (like saving a classification) are written as already 'executed'.
"""

from backend.db import add_decision_log, supabase


def record_executed(event_id: str, agent_name: str, tool_called: str, input_: dict, result: dict,
                    risk_tier: str = "low") -> dict:
    """Log an action that has ALREADY been done and needs no approval (low risk). Returns the agent_actions row."""
    return supabase.table("agent_actions").insert({
        "event_id": event_id,
        "agent_name": agent_name,
        "tool_called": tool_called,
        "input": input_,
        "result": result,
        "risk_tier": risk_tier,
        "status": "executed",
    }).execute().data[0]


def propose(event_id: str, agent_name: str, tool_called: str, risk_tier: str, input_: dict, result: dict,
            reasoning: str = None, outcome: str = None, agents_involved: list = None):
    """
    Write a proposal: an agent_actions row ('awaiting_approval') plus a 'pending' approval.
    If `reasoning` and `outcome` are given, also write ONE decision_log row.
    Returns (action row, approval row). Nothing is carried out here.
    """
    action = supabase.table("agent_actions").insert({
        "event_id": event_id,
        "agent_name": agent_name,
        "tool_called": tool_called,
        "input": input_,
        "result": result,
        "risk_tier": risk_tier,
        "status": "awaiting_approval",
    }).execute().data[0]

    try:
        approval = supabase.table("approvals").insert({
            "agent_action_id": action["id"],
            "status": "pending",
        }).execute().data[0]
        if reasoning and outcome:
            add_decision_log(event_id, agents_involved or [agent_name], reasoning, outcome)
    except Exception:
        # Do not leave a half-finished proposal behind.
        supabase.table("approvals").delete().eq("agent_action_id", action["id"]).execute()
        supabase.table("agent_actions").delete().eq("id", action["id"]).execute()
        raise
    return action, approval


def pending_input_values(tool_called: str, key: str) -> set:
    """
    The values of input[key] for proposals of this kind that are still waiting for approval.
    Used to avoid proposing the same thing twice (for example the same appointment).
    """
    rows = (supabase.table("agent_actions").select("input")
            .eq("tool_called", tool_called).eq("status", "awaiting_approval").execute().data)
    return {row["input"].get(key) for row in rows}

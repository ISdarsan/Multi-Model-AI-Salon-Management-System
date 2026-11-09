"""
hello_agent.py - the smallest possible SalonOS agent (a "hello world" test).

What it does, step by step:
  1. Reads your secrets from the .env file and connects to Supabase.
  2. Creates one row in `events` (every agent action must belong to an event).
  3. Runs a tiny LangGraph "graph" where a local Ollama model is allowed to call
     ONE tool, get_low_stock(), which looks up products that need reordering.
  4. Writes ONE row into `agent_actions` saying what the agent did.
  5. Prints what it found and what it wrote.

Run it from the project root:   python backend/agents/hello_agent.py
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from supabase import create_client

# ----------------------------------------------------------------------------
# 1. Settings and the Supabase connection
# ----------------------------------------------------------------------------

# The .env file lives in the project root, two folders above this file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")  # the model name comes ONLY from .env

AGENT_NAME = "hello_agent"
TOOL_NAME = "get_low_stock"

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. Check your .env file.")
if not OLLAMA_MODEL:
    raise SystemExit("Missing OLLAMA_MODEL in .env (for example OLLAMA_MODEL=llama3.1:8b).")

# The service role key lets this backend script read and write everything.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ----------------------------------------------------------------------------
# 2. The tool: a normal Python function the AI model is allowed to call
# ----------------------------------------------------------------------------

@tool
def get_low_stock() -> list:
    """Return the products whose current stock is below their reorder threshold."""
    # Ask Supabase for every stock row together with its product details.
    response = supabase.table("stock_levels").select(
        "current_qty, products(sku, name, reorder_threshold)"
    ).execute()

    low = []
    for row in response.data:
        product = row["products"]
        # The rule that must be correct is plain Python, not the AI model.
        if row["current_qty"] < product["reorder_threshold"]:
            low.append({
                "sku": product["sku"],
                "name": product["name"],
                "current_qty": row["current_qty"],
                "reorder_threshold": product["reorder_threshold"],
            })
    return sorted(low, key=lambda item: item["sku"])


# ----------------------------------------------------------------------------
# 3. The LangGraph graph: model -> (tool) -> model
# ----------------------------------------------------------------------------

def build_graph():
    """Build a small graph: the model decides, the tool runs, the model summarises."""
    if LLM_PROVIDER != "ollama":
        raise SystemExit(f"LLM_PROVIDER={LLM_PROVIDER!r} is not supported yet. Use 'ollama'.")

    # temperature=0 makes the model as predictable as possible.
    llm = ChatOllama(model=OLLAMA_MODEL, temperature=0)
    # Tell the model that get_low_stock exists and may be called.
    llm_with_tools = llm.bind_tools([get_low_stock])

    def call_model(state: MessagesState):
        """Node 1: the model reads the conversation so far and replies (or asks for the tool)."""
        return {"messages": [llm_with_tools.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode([get_low_stock]))  # Node 2: actually runs the tool

    graph.add_edge(START, "model")
    # If the model asked for a tool go to "tools", otherwise finish.
    graph.add_conditional_edges("model", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")  # after the tool runs, the model writes its summary
    return graph.compile()


# ----------------------------------------------------------------------------
# 4. Putting it all together
# ----------------------------------------------------------------------------

def main():
    # Every agent action must point at an event, so create one first.
    event = supabase.table("events").insert({
        "type": "stock_low",
        "source": AGENT_NAME,
        "payload": {"note": "hello_agent test run"},
        "status": "processing",
    }).execute().data[0]
    print(f"Created event {event['id']}")

    # Run the graph. recursion_limit stops a confused model from looping forever.
    prompt = [
        SystemMessage(content="You are a salon stock assistant. Use the get_low_stock tool to "
                              "find products that need reordering, then summarise the result "
                              "in two short sentences."),
        HumanMessage(content="Which products are low on stock?"),
    ]
    try:
        result = build_graph().invoke({"messages": prompt}, config={"recursion_limit": 8})
    except Exception as error:
        raise SystemExit(
            f"\nCould not run the model ({type(error).__name__}: {error})\n"
            f"Is Ollama running, and did you run:  ollama pull {OLLAMA_MODEL}"
        )

    # Look through the conversation for the tool's answer.
    low_stock = None
    for message in result["messages"]:
        if isinstance(message, ToolMessage):
            low_stock = json.loads(message.content)

    # Small local models sometimes skip the tool. Fall back to calling it ourselves.
    if low_stock is None:
        print("(The model did not call the tool, so I called get_low_stock myself.)")
        low_stock = get_low_stock.invoke({})

    summary = result["messages"][-1].content
    print("\nModel summary:", summary)
    print(f"\nFound {len(low_stock)} low-stock product(s):")
    for item in low_stock:
        print(f"  - {item['sku']}  {item['name']}: {item['current_qty']} left "
              f"(reorder at {item['reorder_threshold']})")

    # Write ONE row to agent_actions: the proof of what the agent did.
    action = supabase.table("agent_actions").insert({
        "event_id": event["id"],
        "agent_name": AGENT_NAME,
        "tool_called": TOOL_NAME,
        "input": {},
        "result": {"low_stock_products": low_stock},
        "risk_tier": "low",      # reading data is low risk: no approval needed
        "status": "executed",
    }).execute().data[0]

    # Mark the event as finished.
    supabase.table("events").update({"status": "handled"}).eq("id", event["id"]).execute()

    print(f"\nWrote agent_actions row {action['id']}")
    print(f"  agent_name={action['agent_name']}  tool_called={action['tool_called']}  "
          f"risk_tier={action['risk_tier']}  status={action['status']}")


if __name__ == "__main__":
    main()

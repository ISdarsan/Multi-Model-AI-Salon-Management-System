# SalonOS: Multi-Agent Salon Operations Command Centre

Context file for Claude Code. Read this before generating code. Capstone: Project 15, 20-day Industry Capstone Program (Agentic AI and Computer Vision).

## 1. What we are building

A control tower for a salon. A shelf camera and live business data feed one event stream. Four specialist agents and one Orchestrator react to events, call real tools, and propose actions. The manager approves anything consequential. Every decision is logged.

Required demo flow: real-world input -> CV detection -> business event -> agent reasoning and tool execution -> recommendation or approved action -> dashboard update. An explanation without a real tool call, state change and decision log does not count.

## 2. Product surfaces

- Customer app (mobile-friendly, route `/book`): pick service, stylist and slot; get confirmation; leave feedback afterwards.
- Manager dashboard (login required, route `/admin`): KPIs, live alerts, approval inbox, agent activity, decision log. Only the manager role can approve.
- Agent backend (FastAPI + LangGraph + Ollama): reacts to each event as it arrives.
- Shelf CV pipeline: shelf photo in, `shelf_observation` event out.

## 3. Architecture (top to bottom)

1. Inputs: shelf photos (staged, no faces), customer bookings and feedback, simulated records from a data generator (for history and demo seed).
2. CV pipeline: YOLO11n + OpenCV detects and counts products, flags empty slots, emits a `shelf_observation` event.
3. Event bus and database: Supabase (Postgres) stores events, products, stock, appointments, tickets, agent_actions, approvals, decision_log. Supabase also provides auth, roles, realtime updates and image storage.
4. Agent layer (LangGraph + Ollama): the Orchestrator routes events to four agents. Agents call tools and propose actions.
5. Approval gate: applies risk tiers. Low runs automatically, Medium waits for approval, High is manager only.
6. Dashboard and decision log: shows KPIs, alerts, approvals and the full trace.

Everything flows through the event table. Agents react to events, not to button clicks.

## 4. Agents

| Agent | Watches | Tools | Output |
| --- | --- | --- | --- |
| Inventory Agent | Shelf CV counts, stock transactions, sales velocity | get_stock, get_sales_velocity, create_draft_po, raise_audit_flag | Draft purchase order, audit flag |
| Scheduling Agent | Appointments, staff availability, waiting-area load | get_appointments, get_staff, reassign_staff, shift_appointment | Reassignment, rescheduled slot |
| Quality Agent | Service log, workstation readiness | get_service_log, check_readiness, flag_quality_issue | Quality flag |
| Customer Experience (CX) Agent | Feedback, reviews, complaints | classify_feedback, search_policy, draft_reply, open_ticket | Reply draft, escalation ticket |
| Orchestrator | All events and proposals | route_event, detect_conflict, resolve_conflict, escalate_to_manager | Routing, conflict resolution |

Build depth: make Inventory and Scheduling the strongest. Keep Quality and CX simpler.

Rule-first, LLM-second: use rules for decisions that must be correct (reorder thresholds, double-booking checks). Use the LLM for reasoning text, drafting messages and complaint classification. The LLM provider must be swappable through one `LLM_PROVIDER` setting (Ollama locally, a hosted API later).

## 5. Risk tiers

| Tier | Examples | Rule |
| --- | --- | --- |
| Low | Booking reminder, log note | Runs automatically, logged |
| Medium | Draft purchase order, move an appointment, reassign a stylist | Waits in approval inbox |
| High | Refund or compensation, supplier change | Manager approval only |

Financial, personnel, customer-impacting, inventory-adjustment and claims actions always need human review.

## 6. Workflow for every event

1. Input arrives (shelf photo, booking, stock move, complaint).
2. Event is created in the events table.
3. Orchestrator routes it to the agent that owns the event type.
4. Agent reads data, applies rules, calls tools, drafts a proposal.
5. Check: do two agents clash? If yes, the Orchestrator resolves with a stated rule or escalates to the manager.
6. Check: risk tier. Low runs automatically. Medium or High goes to the approval inbox.
7. Manager approves or rejects. Approved: the action executes and the result is logged. Rejected: reason logged, agent informed.
8. Dashboard and decision log update.

## 7. Event types

`shelf_observation`, `stock_low`, `booking_created`, `staff_absent`, `complaint_received`, `queue_overload`

Each event has: id, type, source, payload (JSON), timestamp, status.

## 8. Database tables

- products, stock_levels
- appointments, staff
- feedback, tickets
- events
- agent_actions (what each agent proposed, tool used, result)
- approvals (pending / approved / rejected, who decided, when)
- decision_log (event, agents involved, reasoning text, outcome)

Notes: appointments and feedback must be writable by the customer booking page. Users have a `role` (manager or customer). Use row-level security: customers see only their own bookings; only managers read approvals and the decision log.

## 9. The five cross-agent scenarios (acceptance test)

1. Colour stock is low and a colour service is booked tomorrow. Inventory, Scheduling and Orchestrator: clash detected, booking held, draft purchase order created. Manager approves the order.
2. A stylist is marked absent. Scheduling and CX: appointments reassigned, notice drafted for affected customers. Manager approves the reassignment.
3. A customer complaint arrives. CX and Quality: ticket opened, service log checked, refund suggested if justified. Manager only, for any refund.
4. Shelf photo shows an empty slot but the database says stock exists. Inventory: audit flag raised. Manager reviews before any stock adjustment.
5. Waiting area is overloaded. Scheduling and CX: appointments shifted, delay notice drafted. Manager approves the shift.

Each scenario is an automated test with pass or fail and latency. Every one must show a real tool call and a database change.

## 10. CV module

One task only: shelf monitoring. 8 to 10 salon products, 150 to 200 self-taken photos, annotate, train YOLO11n on Google Colab free GPU. Targets: at least 90% SKU identification on held-out photos, reported count accuracy and CPU inference time. Staged or consented images only. No face recognition or emotion inference.

## 11. Tech stack (free)

- LLM: Ollama (Llama 3.2 3B or Qwen2.5 3B) locally; swappable hosted API later
- Agents: LangGraph
- CV: YOLO11n, OpenCV, optional PaddleOCR
- Backend: Python, FastAPI
- Database, auth, realtime, storage: Supabase
- Frontend: React, Tailwind, shadcn/ui, Recharts (one app, `/book` and `/admin`), hosted on Vercel
- Policy retrieval for complaints: Chroma or FAISS
- Packaging: Docker Compose, `.env.example`

## 12. Success targets (proposed, confirm with mentor)

- 5 of 5 scenarios pass
- At least 90% SKU identification
- Event-to-alert latency under 10 seconds
- 100% of Medium and High actions have human approval
- 100% of decisions traceable (event, agent, tool, result, approver)
- 15% lower simulated waiting time versus a no-AI baseline

## 13. Phases (20 days)

1. Days 1 to 4, Define and prepare: schema, event types, data generator, start shelf photos. Freeze the schema on Day 2.
2. Days 5 to 9, Computer vision: train and evaluate YOLO11n, shelf photo becomes an event.
3. Days 10 to 15, Agents and integration: Orchestrator, Inventory and Scheduling agents, approval gate, decision log, CV-to-agent link. Quality and CX agents from the same template.
4. Days 16 to 17, Dashboard and booking app: dashboard, approval inbox, decision log screen, customer booking page.
5. Days 18 to 20, Test and ship: scenario tests, latency, bug fixes, Docker Compose, README, report, demo video.

Do not start a phase until the previous phase's exit check passes.

## 14. Build order for Claude Code

1. Supabase SQL migration for the tables above, with row-level security and roles.
2. FastAPI skeleton with an event endpoint.
3. One tiny LangGraph agent that reads low-stock products and writes one row to agent_actions.
4. Then the Inventory Agent end to end, then Scheduling, Orchestrator, Quality, CX.
5. Dashboard and customer app last.

Build one slice at a time and check each works before moving on.

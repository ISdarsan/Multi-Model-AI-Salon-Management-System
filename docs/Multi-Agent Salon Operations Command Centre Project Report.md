# Multi-Agent Salon Operations Command Centre: Project Report

Nov 9, 2026 · @darsan

## 1. Project overview

**SalonOS is a control tower for a salon: AI agents watch stock, bookings, service quality and customer feedback, propose fixes, and the manager approves anything important.**

**Problem.** A salon manager checks stock in one sheet, appointments in a booking app, the staff rota somewhere else, and reviews on Google. None of these systems talk to each other, so problems are found late: colour runs out the day a colour service is booked, two clients are double-booked, a complaint sits unanswered.

**Solution.** One platform where a shelf camera and business records feed an event stream. Four specialist agents and one Orchestrator react to those events, call real tools (database lookups, draft purchase orders, staff reassignment, ticket creation), and log every decision. Consequential actions wait in an approval inbox.

**Capstone question (from the brief).** How can multiple AI agents collaborate to monitor salon inventory, appointments, service readiness, and customer experience through one platform?

**Required demonstration flow.** Real-world input → CV detection → business event → agent reasoning and tool execution → recommendation or approved action → dashboard update. An explanation without real tool calls, state, and a traceable decision log does not count.

| Item | Detail |
| --- | --- |
| Project | No. 15, Multi-Agent Salon Operations Command Centre |
| Program | 20-day industry capstone, Agentic AI and Computer Vision |
| Team size | 3 to 5 members, about 4 to 6 hours per member per day |
| Scope | Proof of concept with simulated data |
| Budget | Zero: only free and open-source tools |

## 2. Objectives, scope and success KPIs

**The project is done when five cross-agent scenarios run end to end with traceable decisions, correct routing, and manager approval on every consequential action.** That is the brief's acceptance criterion; the targets below add measurable numbers to it.

**Objectives**

- Detect stock levels from shelf images with one primary CV task.
- Run four specialist agents plus an Orchestrator that use real tools and keep state.
- Resolve conflicts between agents instead of letting them act blindly.
- Keep a human in the loop for money, staff and customer-impacting actions.
- Show everything on one executive dashboard with a full decision trail.

**In scope (core)**

- Shelf-monitoring CV, simulated salon data, event bus, 4 agents + Orchestrator, approval inbox, decision log, dashboard, 5 scenarios, test harness.

**Out of scope (stretch, decided after the core works)**

- Second CV task, forecasting model, multi-branch view, voice queries, QR feedback page.

**Success KPIs**

| KPI | Target | How it is measured |
| --- | --- | --- |
| Cross-agent scenarios passing | 5 of 5 | Automated scenario test harness |
| SKU identification on controlled shelf test set | at least 90% | Held-out shelf photos |
| Event-to-alert latency | under 10 seconds | Timestamps in the decision log |
| Consequential actions with human approval | 100% | Approval inbox records |
| Decisions with a full trace (event, agent, tool, result) | 100% | Decision log audit |
| Simulated average waiting time vs no-AI baseline | 15% lower | Simulation run, with and without agents |
| Simulated stockouts prevented vs baseline | reported as a count | Simulation run, with and without agents |

These are target thresholds to evaluate, not guaranteed results. The 90%, 10 s and 15% figures are borrowed from acceptance criteria in related projects of the same catalogue and should be confirmed with your mentor.

## 3. System architecture

**Everything flows through one event bus, and a human approval gate sits between agent proposals and real actions.** This follows the reference architecture in the brief: data acquisition, CV, event processing, agent orchestration, human approval, then a dashboard.

&#91;embedded content: system architecture · 6 layers\]

Read top to bottom: inputs become events, the Orchestrator routes them to agents, agents propose actions, and only approved or low-risk actions take effect. The manager approves in the dashboard, and each decision returns to the agent layer. CX Agent is the Customer Experience Agent.

| Layer | What it does | Built with |
| --- | --- | --- |
| Inputs | Shelf photos, plus a generator that simulates stock, bookings, staff and feedback | Python, staged images |
| CV pipeline | Detects and counts products, flags empty slots, emits an event | YOLO11n, OpenCV |
| Event bus and database | Stores every event, record, action and approval in one place | FastAPI, SQLite |
| Agent layer | Orchestrator routes events; four agents call tools and propose actions | LangGraph, Ollama |
| Approval gate | Applies risk tiers and holds actions for the manager | Backend rules |
| Dashboard and log | Shows KPIs, alerts, approvals and the full decision trail | React or Streamlit |

## 4. Agents, tools and risk tiers

**Four specialist agents handle one domain each, and an Orchestrator decides who acts and settles conflicts.** Each agent reacts to events from the event bus and acts only through defined tools, so every action is real, logged and reversible by the manager.

| Agent | Watches | Tools it can call | Typical output |
| --- | --- | --- | --- |
| Inventory Agent | Shelf CV counts, stock transactions, sales velocity | get\_stock, get\_sales\_velocity, create\_draft\_po, raise\_audit\_flag | Draft purchase order, audit flag |
| Scheduling Agent | Appointments, staff availability, waiting-area occupancy | get\_appointments, get\_staff, reassign\_staff, shift\_appointment | Staff reassignment, rescheduled slot |
| Quality Agent | Workstation readiness, service log | get\_service\_log, check\_readiness, flag\_quality\_issue | Quality flag, readiness report |
| Customer Experience Agent | Feedback, reviews, complaints | classify\_feedback, search\_policy, draft\_reply, open\_ticket | Reply draft, escalation ticket |
| Orchestrator | All events and all agent proposals | route\_event, detect\_conflict, resolve\_conflict, escalate\_to\_manager | Routing decision, conflict resolution |

**Build depth.** Make the Inventory and Scheduling agents the strongest, because they power most scenarios. Keep the Quality and Customer Experience agents simpler.

**Rule-first, LLM-second.** Small local models can be unreliable at tool calling. Use rules for decisions that must be correct (reorder thresholds, double-booking checks) and use the LLM for reasoning text, message drafting and complaint classification.

**Orchestrator duties**

- Route each event to the right agent.
- Detect when two agent proposals clash and resolve the clash with a stated rule.
- Escalate to the manager when a rule cannot settle it.
- Write the resolution steps to the decision log.

**Risk tiers** keep automation safe. The tier decides whether an action runs alone or waits.

| Tier | Example actions | Rule |
| --- | --- | --- |
| Low | Send a booking reminder, add a log note | Runs automatically, logged |
| Medium | Draft purchase order, move an appointment, reassign a stylist | Waits in the approval inbox |
| High | Refund or compensation, supplier change | Manager approval only, never automatic |

The brief requires human review for financial, personnel, customer-impacting, inventory-adjustment and claims actions, which is why anything beyond Low waits for approval.

## 5. Computer vision module

**One CV task is enough: shelf monitoring, where a photo of the product shelf becomes per-product counts and empty-slot alerts.** The brief says to pick one primary CV task and one meaningful agent workflow, and to treat the rest as stretch goals.

**Pipeline**

1. Capture a shelf photo (staged, no people in frame).
2. Detect product units with YOLO11n and OpenCV preprocessing.
3. Identify each product by class, with barcode or label reading where visible (PaddleOCR is the suggested option).
4. Count units per product and flag empty slots.
5. Emit a `shelf_observation` event with product, observed count and timestamp.
6. The Inventory Agent compares the observation with the database count.

**Dataset plan**

- Choose 8 to 10 salon products (shampoo, serum, colour tubes and similar).
- Photograph them yourself, or use printed product images, on a shelf from different angles and lighting. Aim for about 150 to 200 images.
- Start collecting by Day 3, because data is the largest risk in the project.
- Annotate with a free tool (Roboflow free tier or Label Studio), then train YOLO11n on Google Colab free GPU.

**Evaluation**

| Metric | Target | Notes |
| --- | --- | --- |
| SKU identification accuracy | at least 90% | Controlled test set, photos not used in training |
| Count accuracy | reported | Predicted vs true count per product |
| Inference time per image | reported | Measured on your laptop CPU |

**Limits to state honestly in the report**

- Video-only observation cannot measure product consumption precisely without sensors or transaction records.
- Use staged or consented images only, and no facial identification or emotion inference.

## 6. End-to-end workflow and scenarios

**Every event follows the same path: detect, route, reason, check for clashes, check the risk tier, then act or wait for the manager.** The brief's required demo flow is real-world input, CV detection, business event, agent reasoning and tool execution, approved action, dashboard update. This diagram is that flow with the decision points added.

&#91;embedded content: end-to-end workflow · 3 decisions\]

A clash between two agents goes to the Orchestrator first. Low-risk actions run on their own; everything else waits in the approval inbox. Rejected proposals are logged with a reason, so nothing disappears silently.

**The five cross-agent scenarios** are the acceptance test. Each one is built as an automated test with pass or fail and a latency reading.

| No. | Trigger | Agents involved | What happens | Human approval |
| --- | --- | --- | --- | --- |
| 1 | Colour stock is low and a colour service is booked tomorrow | Inventory, Scheduling, Orchestrator | Clash detected; booking held; draft purchase order created | Manager approves the order |
| 2 | A stylist is marked absent | Scheduling, CX | Appointments reassigned; affected customers get a notice draft | Manager approves the reassignment |
| 3 | A customer complaint arrives about a service | CX, Quality | Ticket opened; service log checked; refund suggested if justified | Manager only, for any refund |
| 4 | Shelf photo shows an empty slot but the database says stock exists | Inventory | Mismatch raised as an audit flag | Manager reviews before any stock adjustment |
| 5 | Waiting area is overloaded | Scheduling, CX | Appointments shifted; delay notice drafted | Manager approves the shift |

**Walkthrough of scenario 1**

1. The shelf photo shows two colour tubes left; the CV pipeline emits a `shelf_observation` event.
2. The Orchestrator routes it to the Inventory Agent, which checks stock, sales speed and lead time, and finds the product is below its reorder threshold.
3. The Scheduling Agent sees a colour service booked tomorrow that needs this product.
4. The Orchestrator detects the clash, holds the booking and asks the Inventory Agent for a draft purchase order.
5. The draft order lands in the approval inbox as a Medium-risk action.
6. The manager approves; the order is recorded and the booking hold is released or rescheduled.
7. The dashboard updates and the decision log stores every step.

## 7. Data design

**All business data is simulated by a generator script, and everything the system does is expressed as rows in a small SQLite database.** The brief expects simulated inventory, appointments, staff allocation and service-quality data, and 300 to 500 synthetic feedback records for the complaint workflow.

**Simulated datasets**

| Dataset | Size | Key fields |
| --- | --- | --- |
| Products | 20 to 30 | SKU, name, category, supplier, price, reorder threshold, lead time |
| Stock transactions | generated per simulated day | SKU, quantity change, type (sale, use, delivery), timestamp |
| Staff | 5 to 8 | name, skills, shift, availability |
| Appointments | generated per simulated day | client alias, service, stylist, start, duration, products needed |
| Feedback | 300 to 500 | text, rating, category, timestamp |
| Salon policies | a few short documents | refund rules, delay compensation, escalation rules (used for RAG) |

Customer data is synthetic or consented. Use aliases, not real names, and no face recognition.

**Core database tables**

| Table | Purpose |
| --- | --- |
| products, stock\_levels | Catalogue and current stock count |
| appointments, staff | Bookings and availability |
| feedback, tickets | Complaints and their resolution |
| events | Every CV detection, booking change, stock change or complaint, as one stream |
| agent\_actions | What each agent proposed, the tool used and the result |
| approvals | Pending, approved or rejected status, who decided and when |
| decision\_log | Event, agents involved, reasoning text, outcome; powers replay |

**Event model.** Every event has an id, type, source, payload (JSON), timestamp and status. Example types: `shelf_observation`, `stock_low`, `booking_created`, `staff_absent`, `complaint_received`, `queue_overload`. Agents subscribe to the event types they care about, which keeps them independent and makes new agents easy to add.

## 8. Dashboard and product features

**The dashboard is what makes this look like a product: one screen where the manager sees everything and approves.** The core screens below are the build target. Everything after them is Phase 2, added only once the core works.

**Core screens**

| Screen | What it shows |
| --- | --- |
| Command centre | KPI cards (stock health, average wait, open alerts, pending approvals), live alert feed, agent activity feed |
| Approval inbox | Every Medium and High action with approve and reject buttons and the agent's reasoning |
| Decision log | Searchable history: event, agents, tools, result, who approved |
| Inventory view | Shelf photo with detections, counts vs database, draft purchase orders |
| Schedule view | Today's bookings per stylist, conflicts, waiting-area load |
| Customer view | Complaints, tickets, reply drafts, sentiment trend |

**Phase 2 add-ons (after the core works)**

| Feature | What it does | Why it helps the demo |
| --- | --- | --- |
| Simulation clock and chaos buttons | Fast-forward the salon day and inject events such as supplier delay, stylist sick, angry review | The audience watches agents react live; also makes simulated data look intentional |
| Agent negotiation view | Shows how the Orchestrator settles a conflict step by step | Makes multi-agent behaviour visible |
| Decision replay | Click a past alert and replay event, agents, tools and outcome | Proves traceability |
| Business impact counter | Compares results with a no-AI baseline from the simulation | Covers the business-value marks |
| Scenario test harness | Runs the 5 scenarios as automated tests with pass or fail and latency | Covers testing and reproducibility marks |
| Telegram approvals | Manager approves a draft order from a phone chat using the free Telegram Bot API | Strong live-demo moment |
| Morning brief | Agent summarises the day's risks at the start of the simulated day | Shows proactive behaviour |

**Optional, decide later**

- Customer feedback page reached by QR code, feeding the Customer Experience Agent live.
- Voice queries using the free browser Web Speech API.
- Workstation-readiness CV as a second CV task.
- Sales-velocity forecasting with a simple model.
- Multi-branch view.
- Hosted Postgres on a free tier instead of SQLite.

## 9. Tech stack and project structure

**Every tool below is free or open source, and the whole system runs on a laptop.** Heavy training happens once on Google Colab's free GPU; day-to-day runs use CPU.

| Part | Tool | Why this choice |
| --- | --- | --- |
| LLM | Ollama with a small model (Llama 3.2 3B or Qwen2.5 3B) | Local, free, runs on a modest laptop |
| Agent framework | LangGraph | Named in the brief; supports tool calling, state and graphs |
| Computer vision | YOLO11n, OpenCV, optional PaddleOCR | Light enough for CPU; trained on Colab |
| Backend | Python with FastAPI | Simple REST and WebSocket endpoints |
| Database | SQLite | No setup; one file |
| Live updates | WebSocket or server-sent events | Pushes alerts to the dashboard |
| Frontend | React with Tailwind and Recharts (Streamlit as the fast fallback) | React looks like a product; Streamlit is quicker if time is short |
| Policy retrieval | Chroma or FAISS | Free vector store for the complaint-policy RAG |
| Notifications | Telegram Bot API | Free, for phone approvals |
| Packaging | Docker Compose | One command starts everything |
| Code and docs | GitHub | Repository, README, architecture diagram |

**Fallback for the LLM.** If the local model is too slow or unreliable, a free-tier hosted API can be used as a backup, but the project should not depend on it.

**Suggested repository layout**

```text
salonos/
  README.md
  docker-compose.yml
  data/            # generator script, sample CSVs, shelf images
  cv/              # training notebook, detector, shelf_observation emitter
  backend/
    api/           # FastAPI routes, WebSocket
    events/        # event bus and event types
    agents/        # inventory, scheduling, quality, cx, orchestrator
    tools/         # functions the agents can call
    approvals/     # risk tiers, approval inbox logic
    db/            # schema, migrations, seed
  frontend/        # dashboard
  simulation/      # clock, chaos buttons, scenario scripts
  tests/           # scenario test harness
  docs/            # report, diagrams, demo script
```

## 10. Delivery plan, team, testing and risks

**The core build follows the brief's 20-day plan in five phases, with agents getting the longest block because Agentic AI design carries the most marks (25%).** Extras are deliberately left out of this plan and decided only after the core works.

&#91;embedded content: 20-day core build · 5 phases\]

Start shelf photography on Day 3, not Day 6, because collecting and labelling images is the biggest schedule risk.

**Team split (two people)**

| Person | Owns |
| --- | --- |
| You | Architecture, database, event bus, backend, YOLO training, Orchestrator, Inventory and Scheduling agents, approval logic, dashboard, integration |
| Your partner | Shelf photos and annotation, data generator, Quality and CX agents, policy documents, scenario tests, report, README, presentation, demo video |

The partner's tasks depend only on your schema and the agent template, so nothing on the critical path waits for them. Both of you should be able to explain every agent in the demo. The phase-by-phase tasks are in section 11.

**Testing plan**

| Test | What it checks | Pass criterion |
| --- | --- | --- |
| Scenario tests | All 5 cross-agent scenarios, end to end | 5 of 5 pass, every step in the decision log |
| CV accuracy | SKU identification on held-out shelf photos | at least 90% |
| Latency | Event created to alert shown on the dashboard | under 10 seconds |
| Approval safety | No Medium or High action runs without approval | 0 exceptions |
| Traceability | Each action has event, agent, tool, result and approver | 100% |
| Baseline comparison | Simulated waiting time and stockouts with and without agents | Report the difference; 15% lower waiting time is the target |

**Risks and fallbacks**

| Risk | Mitigation |
| --- | --- |
| Too few shelf images | Start photography on Day 3; use 8 to 10 products; print product images if needed |
| Small local LLM is unreliable at tool calling | Rules for critical decisions; LLM for drafting and classification; free hosted API as backup |
| Scope grows too large | One CV task; build scenario 1 fully first; keep Quality and CX agents simple |
| Laptop is slow | YOLO11n, training on Colab, a 3B-parameter model |
| Agents only produce explanations | Every scenario must show a real tool call and a database change |
| Privacy | Staged or consented images, no face recognition, synthetic customer data |

**Mandatory final deliverables**

- [ ] Problem statement and industry use-case document
- [ ] System architecture and agent workflow diagram
- [ ] Source code repository with setup instructions
- [ ] Configured and trained CV pipeline with evaluation results
- [ ] Working agentic application with dashboard
- [ ] Final report, five-minute demo video and presentation

**How the project is marked**

| Criterion | Weight | Where we earn it |
| --- | --- | --- |
| Industry relevance and problem understanding | 15% | Salon problem statement, KPIs, scenarios |
| Computer vision implementation and accuracy | 20% | Shelf pipeline and evaluation report |
| Agentic AI design, tool use, decision logic | 25% | Orchestrator, risk tiers, tool calls, decision log |
| System integration and demonstration | 15% | End-to-end flow, five scenarios live |
| Business value and measurable KPIs | 10% | Baseline comparison, KPI table |
| Testing, documentation, reproducibility | 10% | Scenario tests, README, Docker Compose |
| Presentation and Q&A | 5% | Demo video and slides |

**After the core works.** Choose add-ons from the Phase 2 table in section 8, starting with the simulation panel and the scenario test harness, which add the most to the demo for the least work.

Source: the 20-Day Industry Capstone Program brief, Project 15.

## 11. Implementation plan by phase

**Build in five phases, and do not start a phase until the previous phase's exit check passes.** Each phase lists what you do, what your partner does, and the check that closes it. Day numbers follow the brief's 20-day plan.

**Working rules for two people**

- Freeze the database schema and event types on Day 2, so your partner can build against them.
- Use one GitHub repo with a branch per person and merge at the end of each phase.
- Hold a 15-minute sync every day: what is done, what is blocked, what is next.
- Your partner learns the full flow from the architecture diagram on Days 1 and 2.

### Phase 1: Define and prepare (Days 1 to 4)

**Goal:** the schema is fixed, the data generator exists, and shelf photography has started.

**You**

- Problem statement, KPIs and requirements document.
- Repo, folder structure, database schema, event types.
- Install and test Ollama and LangGraph with a one-tool "hello world" agent.
- Share the schema with your partner on Day 2.

**Your partner**

- Choose 8 to 10 salon products and start photographing them on Day 3, from different angles and lighting.
- Write the data generator from your schema: products, staff, appointments, feedback.
- Learn the flow from the architecture and workflow diagrams.

**Exit check:** the repo runs, the database fills from the generator, and 100 or more shelf photos exist.

### Phase 2: Computer vision (Days 5 to 9)

**Goal:** a shelf photo becomes a `shelf_observation` event in the database.

**You**

- Train YOLO11n on Colab and evaluate it on held-out photos.
- Build the event endpoint in FastAPI and the CV-to-event emitter.
- Write the evaluation report.

**Your partner**

- Finish the photo set (150 to 200 images) and split the annotation work with you.
- Set aside a test set of photos that are never used in training.
- Add stock transactions and sales history to the generator.

**Exit check:** SKU identification is at least 90% on the held-out photos, and a photo creates an event.

### Phase 3: Agents and integration (Days 10 to 15)

**Goal:** scenarios 1 and 4 run end to end with a decision log.

**You**

- Orchestrator, Inventory Agent and Scheduling Agent, with their tools.
- Approval gate, risk tiers and the decision log.
- Connect the CV events to the agents.

**Your partner** (from Day 11, once your first agent works)

- Quality Agent and CX Agent, built from your agent template.
- Short salon policy documents for the complaint workflow.
- Complaint classification using rules plus the LLM.
- First drafts of the scenario test scripts.

**Exit check:** a shelf photo triggers an agent, the proposal reaches the approval table, and every step is logged.

### Phase 4: Dashboard (Days 16 to 17)

**Goal:** the manager can see alerts and approve or reject from one screen.

**You**

- Dashboard: KPI cards, live alerts, approval inbox, agent activity.
- Decision log screen.

**Your partner**

- Run each screen against the scenarios and keep a bug list.
- Customer feedback and complaints view.
- Start the report: problem, architecture, data and CV sections.

**Exit check:** an approval clicked on the dashboard changes the database state and appears in the log.

### Phase 5: Test and ship (Days 18 to 20)

**Goal:** all five scenarios pass, and every final deliverable is ready.

**You**

- Fix bugs, measure latency, package with Docker Compose.
- Final integration check.

**Your partner**

- Automated tests for all 5 scenarios.
- README, final report and presentation.
- Record the five-minute demo video.

**Both**

- Rehearse the demo and Q&A. Each person explains the parts they built, and one agent they did not build.

**Exit check:** 5 of 5 scenarios pass and the six mandatory deliverables are ticked.

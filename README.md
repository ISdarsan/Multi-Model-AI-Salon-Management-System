# SalonOS: Multi-Agent Salon Operations Command Centre

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and [docs/SCHEMA.md](docs/SCHEMA.md) for the database.

## Run the hello agent

`backend/agents/hello_agent.py` is a tiny test agent. It asks a local Ollama model to call one tool, `get_low_stock()`, then writes one row to the `agent_actions` table.

**Before you start:** the database tables and seed data must already be in your Supabase project, and `.env` must hold `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `OLLAMA_MODEL` (copy `.env.example` to `.env`). [Ollama](https://ollama.com) must be installed.

Run these from the project root:

```powershell
# 1. Download the model named in OLLAMA_MODEL in .env (one time, about 5 GB)
ollama pull llama3.1:8b

# 2. Create a private Python environment (one time)
python -m venv .venv

# 3. Switch it on (every new terminal)
.venv\Scripts\Activate.ps1

# 4. Install the packages (one time)
pip install -r backend/requirements.txt

# 5. Run the agent
python backend/agents/hello_agent.py
```

On success it prints the low-stock products (with the seed data: `COL-6N`, `COL-7N`, `SHA-003`) and the `agent_actions` row it wrote. Each run adds one `events` row and one `agent_actions` row; you can see them in the Supabase Table Editor.

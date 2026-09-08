# Monitoring Agent

An autonomous **log monitoring pipeline** for Python/FastAPI applications.

The Monitoring Agent is **Agent 1** of a three-agent DevOps incident-remediation system. It takes raw application logs and turns them into **grouped, deduplicated, LLM-triaged incidents**.

---

## Architecture

```
Raw Logs (text file)
        │
        ▼
┌──────────────────────┐
│  TextFileCollector   │  ← tails the log file (pluggable LogCollector)
│  + LogNormalizer     │  ← converts raw lines to LogEvent (Pydantic)
└────────┬─────────────┘
         │  LogEvent
         ▼
┌──────────────────────┐
│  PreFilter           │  ← deterministic rule-based, NO LLM
│  IGNORE / CANDIDATE  │
│  / IMMEDIATE         │
└────────┬─────────────┘
         │  CANDIDATE + IMMEDIATE only
         ▼
┌──────────────────────┐
│  CorrelationGrouper  │  ← time window + traceback detection
│  → IncidentCluster   │
└────────┬─────────────┘
         │  IncidentCluster
         ▼
┌──────────────────────┐
│  TriageAgent         │  ← ONLY component that calls the LLM
│  → TriageResult      │  ← structured JSON, validated by Pydantic
└────────┬─────────────┘
         │  TriageResult
         ▼
┌──────────────────────┐
│  SQLiteStateStore    │  ← deduplication (pluggable StateStore)
│  Fingerprint + TTL   │
└────────┬─────────────┘
         │  new / duplicate
         ▼
┌──────────────────────┐
│  TerminalOutput      │  ← formatted alert (pluggable AlertOutput)
└──────────────────────┘
```

**Most important principle:** raw logs are never sent to the LLM directly. Only meaningful incident clusters reach the triage stage.

---

## Components

| Module | Responsibility |
|---|---|
| `app/ingestion/base.py` | `LogCollector` protocol |
| `app/ingestion/normalizer.py` | `LogNormalizer` — JSON / uvicorn / Python logging / fallback |
| `app/ingestion/text_file.py` | `TextFileCollector` — async tail-f |
| `app/filtering/prefilter.py` | `PreFilter` — 16 deterministic rules |
| `app/correlation/grouper.py` | `CorrelationGrouper` — time-window + traceback bucketing |
| `app/triage/prompt.py` | System prompt + few-shot examples |
| `app/triage/agent.py` | `TriageAgent` + `TriageProvider` protocol + `OllamaTriageProvider` + `OpenAITriageProvider` |
| `app/state/base.py` | `StateStore` protocol |
| `app/state/sqlite.py` | `SQLiteStateStore` + `generate_fingerprint()` |
| `app/output/base.py` | `AlertOutput` protocol |
| `app/output/terminal.py` | `TerminalOutput` — formatted stdout |
| `app/config.py` | `Settings` — all knobs in one place |
| `app/main.py` | `MonitoringPipeline` — wires everything together |

---

## Log Flow

```
1. TextFileCollector tails the log file.
   Every new line → LogNormalizer → LogEvent

2. LogEvent → PreFilter
   IGNORE  → discarded (DEBUG, INFO noise, 2xx, health checks)
   CANDIDATE/IMMEDIATE → passed to CorrelationGrouper

3. CorrelationGrouper buffers events into buckets.
   Same service + request_id/endpoint within 5 s → same bucket.
   "Traceback (most recent call last):" opens a traceback collector
   that gathers all following frame lines.
   After flush_delay_seconds of silence, the bucket becomes an IncidentCluster.

4. IncidentCluster → TriageAgent (LLM call)
   Full cluster context sent as structured prompt.
   Returns TriageResult (severity, category, summary, evidence …)

5. TriageResult → StateStore.is_duplicate()
   Fingerprint = sha256(service + exc_type + message + source + endpoint)[:16]
   If seen within dedup_window_hours → increment count, suppress alert.
   If new → record, emit alert.

6. AlertOutput.emit() → prints formatted report to terminal.
```

---

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env   # Linux / macOS
copy .env.example .env # Windows
```

### Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` or `openai` |
| `LLM_MODEL` | `llama3.1:8b` | Model name (Ollama) or e.g. `gpt-4o-mini` (OpenAI) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OPENAI_API_KEY` | *(empty)* | Required only when `LLM_PROVIDER=openai` |
| `OPENAI_BASE_URL` | *(empty)* | Optional OpenAI-compatible proxy URL |
| `LOG_FILE_PATH` | `sample_logs/application.log` | Log file to watch |
| `CORRELATION_WINDOW_SECONDS` | `5` | Max gap (s) between related events |
| `FLUSH_DELAY_SECONDS` | `3` | Silence before cluster is flushed |
| `DEDUP_WINDOW_HOURS` | `10` | Suppress duplicate alerts for N hours |
| `STATE_DB_PATH` | `monitoring_state.db` | SQLite database path |
| `CUSTOM_TRIAGE_RULES` | *(empty)* | Comma-separated deployment-specific rules |

---

## Running the Demo

### 1. Install dependencies

```bash
cd monitoring-agent
pip install -r requirements.txt
```

### 2. Set up the LLM (choose one)

#### Option A — Ollama (default, runs fully locally, no API key)

```bash
# Install Ollama from https://ollama.com/download, then:
ollama pull llama3.1:8b
```

Your `.env` (copy from `.env.example`) should have:
```
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1:8b
OLLAMA_BASE_URL=http://localhost:11434
```

#### Option B — OpenAI

```
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

### 3. Start the agent

```bash
python -m app.main
```

The agent watches `sample_logs/application.log` by default. It starts tailing from the **end** of the file (it does not replay history on startup).

### 4. Inject logs (separate terminal)

**Inject noise (no alert expected):**
```powershell
Add-Content sample_logs\application.log "2026-09-08 10:30:00,000 DEBUG    app.db: Initializing connection pool"
Add-Content sample_logs\application.log "2026-09-08 10:30:02,000 INFO     uvicorn.access: INFO:     127.0.0.1:54321 - `"GET /healthz HTTP/1.1`" 200 OK"
```

**Inject an exception traceback (alert expected):**
```powershell
Add-Content sample_logs\application.log "2026-09-08 10:31:02,010 ERROR    uvicorn.error: Exception in ASGI application"
Add-Content sample_logs\application.log "2026-09-08 10:31:02,010 ERROR    uvicorn.error: Traceback (most recent call last):"
Add-Content sample_logs\application.log "2026-09-08 10:31:02,013 ERROR    uvicorn.error:   File `"/app/app/routers/users.py`", line 42, in create_user"
Add-Content sample_logs\application.log "2026-09-08 10:31:02,014 ERROR    uvicorn.error: AttributeError: 'NoneType' object has no attribute 'email'"
```

Within a few seconds the agent will print:

```
================================================================
⚡  NEW INCIDENT  ⚡
================================================================

  Incident ID  : 8c7a3d...
  Severity     : HIGH
  Category     : APPLICATION_ERROR
  Confidence   : 91%
  Occurrences  : 1

  ----------------------------------------------------------------
  Service  : test-service
  Endpoint : POST /api/v1/users
  ...
```

**Replay the full sample log from scratch:**
```bash
# Truncate first so the agent sees it as "new" content
truncate -s 0 sample_logs/application.log   # Linux/Mac
# or on Windows:
Clear-Content sample_logs\application.log   # PowerShell

# Then copy the sample in (agent must be running)
cat sample_logs/application.log.bak >> sample_logs/application.log
```

---

## Running Tests

```bash
# All tests (no LLM calls — LLM is mocked)
pytest -v

# With coverage
pip install pytest-cov
pytest --cov=app --cov-report=term-missing
```

Tests cover:

| File | What it tests |
|---|---|
| `test_normalizer.py` | JSON, uvicorn, Python logging, fallback parsing |
| `test_prefilter.py` | All IGNORE / CANDIDATE / IMMEDIATE rules |
| `test_grouper.py` | Traceback grouping, time window, service separation |
| `test_fingerprint.py` | Stability, uniqueness, dynamic-value normalisation |
| `test_dedup.py` | First occurrence, dedup window, occurrence counting |
| `test_triage_schema.py` | Pydantic validation of TriageResult |
| `test_e2e.py` | Full pipeline with mocked LLM |

---

## How Deduplication Works

Each `IncidentCluster` gets a **fingerprint**:

```python
sha256(service + exception_type + message + source + endpoint)[:16]
```

Dynamic values (UUIDs, timestamps, IP addresses, long numbers) are stripped before hashing so the same bug produces the same fingerprint across repeated occurrences.

The `incidents` table in SQLite tracks:

| Column | Purpose |
|---|---|
| `fingerprint` | Primary key |
| `first_seen` | When first detected |
| `last_seen` | Most recent occurrence |
| `alerted_at` | When the alert was emitted |
| `occurrence_count` | How many times seen |

If `alerted_at > now - dedup_window_hours`, the incident is suppressed and only the count is incremented.

---

## Extending the System

### Adding a new log collector

```python
# app/ingestion/my_collector.py
class KafkaCollector:
    async def stream(self) -> AsyncIterator[LogEvent]:
        async for message in kafka_consumer:
            yield normalizer.normalise(message.value)
```

Satisfies `LogCollector` protocol automatically. Plug it into `MonitoringPipeline`.

### Adding a new triage provider

```python
# app/triage/my_provider.py
class AnthropicTriageProvider:
    async def triage(self, cluster: IncidentCluster) -> TriageResult:
        ...
```

Satisfies `TriageProvider` protocol. Pass to `TriageAgent(provider=...)`.

### Adding a new alert output

```python
# app/output/slack.py
class SlackOutput:
    async def emit(self, cluster, result, occurrence_count=1):
        await slack_client.post_message(...)
```

Satisfies `AlertOutput` protocol. Pass to `MonitoringPipeline(output=SlackOutput(...))`.

### Adding a new state store

```python
# app/state/redis.py
class RedisStateStore:
    async def is_duplicate(self, fingerprint, dedup_window_hours): ...
    async def record(self, fingerprint, cluster, result): ...
    ...
```

Satisfies `StateStore` protocol. Pass to `MonitoringPipeline(state_store=RedisStateStore(...))`.

### Adding codebase-specific triage rules

In `.env`:
```
CUSTOM_TRIAGE_RULES=In this system, /healthz always returns 200 during deploy.,Database pool exhaustion at startup is expected and non-actionable.
```

These rules are injected into the system prompt for every triage call.

---

## Future Agents (Not Implemented)

- **Fix Agent** — receives `IncidentCluster + TriageResult`, investigates the codebase, proposes a code fix.
- **Implementation Agent** — validates and applies an approved fix.

The clean interfaces defined here (`TriageResult`, `IncidentCluster`) are designed to be handed directly to those agents.

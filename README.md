# BlastRadius

**Multi-hop impact analysis for microservice estates, powered by a real FalkorDB graph.**

When something breaks at 2am, the useful question is not "which service is red?"
It is *"if I touch this, what else falls over, who owns it, and has this happened before?"*

BlastRadius answers that from one graph. Give it a failing service or datastore and it walks
`DEPENDS_ON` relationships **backwards** through FalkorDB, returning every affected service,
the **exact relationship path** that connects it to the failure, the teams you need to page,
and the incidents that touched the same component before.

Every number, path and name on screen is the return value of a Cypher query executed against
a live FalkorDB server. Nothing is generated on the client. The optional **Agent
investigation** tab adds a genuinely tool-using AI on top — a *local* Ollama model, no cloud,
no API key — but its only role is to **pick the next tool and its arguments**. It never
writes or executes Cypher, and it cannot invent data: see the agent section below.

Built for the **WeMakeDevs Graph Hacks** hackathon.

---

## Contents

- [What you get](#what-you-get)
- [Why FalkorDB is the core](#why-falkordb-is-the-core)
- [Graph model](#graph-model)
- [Quickstart](#quickstart)
- [Environment variables](#environment-variables)
- [Using the app](#using-the-app)
- [Agent investigation — local, tool-using AI](#agent-investigation--local-tool-using-ai)
- [Command line](#command-line)
- [The Cypher behind it](#the-cypher-behind-it)
- [Tests](#tests)
- [Demo scenario](#demo-scenario)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Design notes and limitations](#design-notes-and-limitations)

---

## What you get

| Capability | Where it lives | What it proves |
|---|---|---|
| Multi-hop impact analysis (1–6 hops, services **and** datastores) | `queries.impact_analysis` | Variable-length Cypher traversal, shortest real path per affected service |
| Ownership lookup | `queries.ownership_lookup`, `queries.team_ownership` | Two-hop graph joins `Team-[:OWNS]->Service` and `Engineer-[:MEMBER_OF]->Team` |
| Incident history | `queries.incident_history`, `queries.incident_detail` | `UNION ALL` over `ROOT_CAUSE` and `IMPACTED`, plus downstream replay |
| Honest missing-service handling | everywhere | Unknown ids return a message and close matches, never a substitute answer |
| Deterministic synthetic estate | `data.py`, `seed.py` | 40 services, 8 teams, 18 engineers, 90 dependencies, 10 incidents — reproducible |
| **Agent investigation (optional, local AI)** | `agent_tools.py`, `llm.py`, `app.py` | A locally running Ollama model selects tools from a 3-tool allowlist; every call runs real Cypher and the UI shows the evidence |
| Professional UI | `app.py` | Overview, Blast radius, Ownership, Incidents, Graph view, Agent investigation |
| Automated tests | `tests/` | 56 tests — 37 original + 19 agent tests, including a real Ollama tool-calling round trip |

---

## Why FalkorDB is the core

The interesting question — "what is the shortest dependency path from this failing component
to the public API gateway?" — is a traversal, not a join table. FalkorDB (openCypher over
Redis) expresses it in one statement and returns the path object itself:

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*1..4]->(origin:Service {id: $service_id})
WHERE dependent.id <> origin.id
RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p
ORDER BY hops ASC, dependent.name ASC
```

Because FalkorDB returns `p`, the application can show *which* chain of services carried the
impact, with the protocol and criticality of every edge — not just a flat list of names.

For datastore failure it is a different shape, and still one query:

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*0..3]->(consumer:Service)
          -[:READS_FROM|WRITES_TO]->(origin:Datastore {id: $service_id})
RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p
ORDER BY hops ASC, dependent.name ASC
```

The `*0..` allows the direct consumer to appear without pretending it has an extra hop, and
the final hop is the real `READS_FROM` / `WRITES_TO` edge.

---

## Graph model

```
 (Engineer)-[:MEMBER_OF]->(Team)-[:OWNS]->(Service)-[:HOSTED_IN]->(Region)
                                            |
                                            | [:DEPENDS_ON {protocol, criticality, calls_per_min}]
                                            v
                                         (Service)  <── the traversal edge
                                            |
                    ┌───────────────────────┼─────────────────────────┐
                    v                       v                         v
             [:READS_FROM]           [:WRITES_TO]        [:PUBLISHES_TO]
                    │                       │                         v
                    └────────► (Datastore)  │                     (Topic)
                                            │                         ^
                                     (Incident)-[:IMPACTED {role}]    │
                                            └──[:ROOT_CAUSE]──►  component
                                                                 [:SUBSCRIBES_TO]
```

| Node | Key properties | Count in demo |
|---|---|---|
| `Service` | `id`, `name`, `tier`, `language`, `repo`, `summary`, `slo_target`, `error_budget_minutes`, `region` | 40 |
| `Team` | `id`, `name`, `cost_center`, `slack_channel`, `timezone` | 8 |
| `Engineer` | `id`, `name`, `role` | 18 |
| `Datastore` | `id`, `name`, `engine`, `region` | 6 |
| `Topic` | `id`, `name`, `broker`, `partitions` | 4 |
| `Incident` | `id`, `title`, `severity`, `status`, `started_at`, `resolved_at`, `duration_minutes`, `summary` | 10 |
| `Region` | `id`, `name`, `region`, `provider` | 2 |

Relationship directions always read as "**from the dependent to its dependency**",
so an outage moves **up** the arrow.

Indexes are created on `id` for every label (and `Service.name`) so lookups and `MERGE`s stay fast.

---

## Quickstart

### 0. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| **Python 3.10+** | runs the app and the tests | `python --version` |
| **Docker Desktop** (or Docker Engine on Linux/macOS) | FalkorDB only ships as a container | `docker version` |
| **Git** (optional) | version control | `git --version` |

Windows note: if `python` opens the Microsoft Store, use the real interpreter or the
virtual environment path shown below. Docker Desktop needs WSL 2 on Windows; its installer
offers that during setup.

### 1. Get the code and create a virtual environment

```bash
git clone <your-fork-url> BlastRadius
cd BlastRadius

# Windows (Git Bash)
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

`pip install -e ".[dev]"` installs the project in editable mode plus `pytest`.
Everything lands inside `.venv/` — your system Python is untouched.

### 2. Configure the connection

```bash
cp .env.example .env       # Windows: copy .env.example .env
```

The template already contains a local development password (`blastradius-dev`).
Change it if you like — but use the same value in step 3.

### 3. Start FalkorDB

```bash
docker run -d --name blastradius-falkordb \
  -p 6379:6379 \
  -e BROWSER=0 \
  -e REDIS_ARGS="--requirepass blastradius-dev --save 60 1 --appendonly no" \
  -v blastradius-falkordb-data:/var/lib/falkordb/data \
  falkordb/falkordb:latest
```

Or, if you prefer Compose: `docker compose up -d` (reads `FALKORDB_PASSWORD` from `.env`).

> **Why `REDIS_ARGS` and not `redis-server --requirepass ...`?**
> The `falkordb/falkordb` image entrypoint ignores command-line arguments and starts
> `redis-server` itself. Passing the password through `REDIS_ARGS` (and the data directory
> through the documented mount path) is the supported way. Without it the server starts with
> **no password**, which is why this project verifies authentication at startup.

### 4. Prove the database is live

```bash
.venv/Scripts/python.exe -m blastradius.cli health
```

Expected:

```text
target      : redis://localhost:6379 (with password), graph='blastradius'
status      : live
server      : FalkorDB 4.22.1 / Redis 8.10.2
round-trip  : 6.12 ms
nodes/edges : 0 / 0
```

### 5. Write the graph with Cypher and run the app

```bash
.venv/Scripts/python.exe -m blastradius.cli seed --reset
.venv/Scripts/python.exe -m streamlit run src/blastradius/app.py
```

Open <http://localhost:8501>. The sidebar shows the live server version, node and edge
counts, and a round-trip time measured on that page load.

### 6. (Optional) Enable the local AI agent

Ollama is the only local-model requirement, is free, and needs no API key — every token
stays on your machine.

```powershell
# 1) Install Ollama (official installer, no admin):
#    https://ollama.com/download/windows  -> run OllamaSetup.exe
#    (or:  irm https://ollama.com/install.ps1 | iex)

# 2) Pull the model this demo uses:
ollama pull qwen3.5:2b

# 3) Make sure Ollama is running (its tray app; defaults to http://localhost:11434)
ollama serve   # only if the background app is not running
```

Resource cost (measured targets, hardware we verified on):

| Item | Requirement |
|---|---|
| Disk | ~1 GB for Ollama + **3.1 GB** for `qwen3.5:2b` (1.88 B param Q8_0 build) |
| RAM | ~2.5 GB free during inference (we ran with 5.9 GB free); CPU-only is fine |
| Network | none at inference time; only the one-time pull needs internet |

Now in the app, the **Agent investigation** tab shows `Ollama reachable · model loaded` and
the whole tab works. If Ollama is not running or the model is missing, that tab shows
explicit setup instructions instead — and the five dashboard tabs keep working either way.

To check the demo data without starting the UI:

```bash
.venv/Scripts/python.exe -m blastradius.cli impact payment-service --depth 4
```

---

## Environment variables

All optional; defaults match the quickstart.

| Variable | Default | Meaning |
|---|---|---|
| `FALKORDB_HOST` | `localhost` | server hostname |
| `FALKORDB_PORT` | `6379` | server port |
| `FALKORDB_PASSWORD` | *(none)* | password set on the container |
| `FALKORDB_GRAPH` | `blastradius` | graph name; the app never touches other graphs |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | local model endpoint (Agent tab only) |
| `OLLAMA_MODEL` | `qwen3.5:2b` | local model tag (Agent tab only) |

`.env` is git-ignored. `.env.example` is safe to commit because it holds no real secret.
Nothing in this repository needs a paid service, a credit card, or a hosted LLM API key.
The optional agent talks only to Ollama on `127.0.0.1`.

---

## Using the app

| Tab | What it does |
|---|---|
| **Overview** | Live node/edge/dependency counts, the most depended-on services, services per tier, severity mix, newest incidents. |
| **Blast radius** | Pick a service or datastore, choose 1–6 hops, and get the affected services with `hops`, owning teams, and the real path. Metrics show impacted services, direct dependents, teams to page and the deepest path. "Trace a single path" breaks a route into node/edge steps with protocol and criticality. The **Cypher executed** expander shows the exact statement. |
| **Ownership** | Owning team, Slack channel, cost centre, timezone and the engineers on rota. A team view lists everything that team owns plus incident history that touched it. |
| **Incidents** | Severity, duration and root cause for every incident; open one to see the recorded affected services and the downstream paths the graph returns for its root cause. |
| **Graph view** | The same blast radius rendered as a Graphviz diagram, with tier colours and real relationship labels. |
| **Agent investigation** | Ask a question in plain English (e.g. *"What breaks if payment-service fails, which teams own the affected services, and have we seen a similar incident?"*). A local Ollama model (`qwen3.5:2b`) picks tools from a fixed 3-tool allowlist; each pick is validated, executed against live FalkorDB, and its evidence is shown verbatim, followed by the model's evidence-citing answer. |

**Missing data is never papered over.** Type an unknown id in *Blast radius → Look up an id
directly* and the page says the component is not in the graph, offers close matches, and
shows no substitute results — the CLI exits with status `2` in the same situation.

---

## Agent investigation — local, tool-using AI

The sixth tab adds a real tool-using agent on top of the same verified query layer. Design
intents and their enforcement:

| Requirement | How it is enforced in code |
|---|---|
| Explicit allowlist | `agent_tools.TOOLS` contains exactly `impact_analysis`, `ownership_lookup`, `incident_history`. The model is given that menu over Ollama `/api/chat` `tools` — it never sees Cypher. |
| No arbitrary Cypher | There is no code path from the model to the query language. Unknown tool names, unknown argument names and bad types are rejected (`Rejection`) *before* any execution. |
| Real queries only | Every accepted call runs the same `queries.*` functions the dashboard uses, against the live graph. |
| Bounded loop | `MAX_TOOL_CALLS = 4` and at most 2 planning rounds; anything beyond the budget is marked `skipped` and shown as such. |
| Honest `not_found` | An unknown id returns a `not_found` record with genuine close matches (`suggest_services`) — the agent cannot wander off to a fabricated answer. |
| Grounded answer | Tool evidence is fed back verbatim (`role:"tool"` messages) and the final prompt says to use only those results, say so if insufficient, and cite ids/teams/incidents exactly as returned. |
| Honest availability | If Ollama is not running or the model is not pulled, the tab shows setup instructions. Nothing pretends a deterministic fallback is an LLM. |
| Zero cost / zero keys | The model runs on your machine; there is no network call beyond `127.0.0.1` in the agent. |

The request/response shape is the documented Ollama tool-calling flow
(<https://docs.ollama.com/capabilities/tool-calling>): the assistant returns
`tool_calls`, we execute them ourselves, and results go back as `role:"tool"` messages.
Ollama itself never runs our functions.

---

## Command line

```bash
python -m blastradius.cli health                    # connectivity + counts
python -m blastradius.cli dataset                   # dataset stats, no DB needed
python -m blastradius.cli seed --reset              # clear this graph, write 88 nodes / 267 edges
python -m blastradius.cli verify                    # counts read back from FalkorDB
python -m blastradius.cli impact <id> --depth 4     # multi-hop impact (add --json for machines)
python -m blastradius.cli owners <id>               # team + engineers
python -m blastradius.cli incidents <id>            # incident history for a service or datastore
python -m blastradius.cli overview                  # rankings and severity mix
```

`--graph <name>` on any command targets a different graph (the test suite uses this to keep
`blastradius_test` separate from the demo graph).

---

## The Cypher behind it

Every query is in [`docs/cypher.md`](docs/cypher.md) with an explanation of each clause and
a sample result. The three that matter most:

**1. Multi-hop impact with real paths**

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*1..4]->(origin:Service {id: $service_id})
WHERE dependent.id <> origin.id
RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p
ORDER BY hops ASC, dependent.name ASC
```

**2. Ownership**

```cypher
MATCH (t:Team)-[:OWNS]->(s:Service {id: $id})
RETURN t.id, t.name, t.cost_center, t.slack_channel, t.timezone
ORDER BY t.name
```

**3. Incident history (root cause *or* affected)**

```cypher
MATCH (i:Incident)-[:ROOT_CAUSE]->(s {id: $id})
RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status,
       i.started_at AS started_at, i.duration_minutes AS duration_minutes,
       i.summary AS summary, 'root cause' AS role
UNION ALL
MATCH (i:Incident)-[:IMPACTED]->(s {id: $id})
RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status,
       i.started_at AS started_at, i.duration_minutes AS duration_minutes,
       i.summary AS summary, 'affected' AS role
```

> Both branches of a `UNION` must expose identical column names — FalkorDB enforces this,
> and the application relies on the `role` column to distinguish "we caused it" from
> "we felt it". Python merges duplicates in favour of `root cause`.

---

## Tests

The suite runs against a **live** FalkorDB server on a separate graph (`blastradius_test`).
There is no mock database: a green run means the Cypher really executed.

```bash
.venv/Scripts/python.exe -m pytest -q
```

```text
................................ s ....                                  [100%]
56 passed, 1 skipped
```

The single skip is the **model round-trip test** (`tests/test_llm.py`): when the Ollama
service or `qwen3.5:2b` is not present on the machine, it skips with an explicit message
instead of pretending the integration passed. Pull the model and re-run to cover it.

What is covered:

- dataset determinism, unique ids, acyclic dependencies, every service owned, every incident
  referencing a real component (`tests/test_data.py`, no database needed);
- live health round-trip, seed counts read back from FalkorDB, direct and 3-hop impact,
  datastore failure traversal, path/edge integrity, depth clamping;
- honest missing-service behaviour plus typo suggestions;
- ownership for services and teams;
- incident history for a service *and* for a datastore root cause, including the
  recorded-vs-traversed distinction (`tests/test_queries.py`);
- the agent allowlist: exactly three tools, unknown-tool and malformed-argument rejection,
  depth range checks (0/7/1.5/"deep"/bool rejected, 1..6 accepted), unsupported arguments,
  honest `not_found` for unknown ids *after* a real graph lookup, real paths/teams/incident
  rows for known ids, and the hard 4-call budget (`tests/test_agent_tools.py`);
- the local-model integration: a genuine question → Ollama → tool selection → live
  graph → evidence-citing answer round trip, skipping honestly when Ollama is absent
  (`tests/test_llm.py`).

If the server is not running the suite fails immediately with a message telling you how to
start it — it does not silently skip.

---

## Demo scenario

A three-minute script with exact expected numbers is in [`docs/demo.md`](docs/demo.md).

The short version:

1. **Overview** — 40 services, 90 dependencies, 10 incidents, all read live.
2. **Blast radius → `payment-service`, 4 hops** — 5 services, 4 teams, deepest path 3 hops:
   `API Gateway -> Web Backend-for-Frontend -> Checkout Service -> Payment Service`.
3. **Switch to `config-service`, 3 hops** — 37 of 40 services and all 8 teams are affected.
   This is the "platform component" story.
4. **Switch to `redis-cart`** — a datastore failure. The path ends in a real `READS_FROM`
   edge, and the same component's incident `INC-2041` explains what happened last time.
5. **Incidents → INC-2041** — root cause `cart-cache`, recorded affected services, and the
   downstream paths recomputed from the graph today.
6. **Ownership → `ledger-service`** — Payments Platform, `#payments-oncall`, three engineers,
   and `INC-2029` as a root cause.
7. **Agent investigation** — ask: *"What breaks if payment-service fails, which teams own
   the affected services, and have we seen a similar incident?"* Show the tool calls the
   model picked, then the actual FalkorDB evidence (impacted services with the real
   `->` paths, owning teams with Slack channels, the matching incident from the graph),
   followed by the answer, which cites those records. Close with: *"the AI only chose
   which verified query to run — every fact on screen is a real Cypher result."*

---

## Project layout

```text
BlastRadius/
├── src/blastradius/
│   ├── app.py          Streamlit UI (6 tabs, live queries)
│   ├── agent_tools.py  agent allowlist: validation + bounded execution over queries.py
│   ├── llm.py          local Ollama /api/chat tool-calling client (no cloud)
│   ├── cli.py          health / seed / verify / impact / owners / incidents / overview
│   ├── config.py       FALKORDB_* configuration
│   ├── data.py         deterministic synthetic estate (fixed seed 1337)
│   ├── db.py           FalkorDB client, path conversion, health report
│   ├── queries.py      the Cypher query layer
│   └── seed.py         MERGE-based writes, indexes, verification counts
├── tests/
│   ├── conftest.py     live database fixtures (separate test graph)
│   ├── test_data.py    dataset invariants, no database required
│   ├── test_agent_tools.py  allowlist, validation, bounds, honest not-found (live DB)
│   ├── test_llm.py     real Ollama round trip (skips honestly when absent)
│   └── test_queries.py integration tests against FalkorDB
├── docs/
│   ├── cypher.md       every query, explained
│   └── demo.md         three-minute demo run sheet
├── docker-compose.yml  optional Compose equivalent of the docker run command
├── pyproject.toml      package metadata, pytest config
├── requirements.txt    runtime + test dependencies
└── .env.example        template; copy to .env
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot reach FalkorDB at localhost:6379` | container stopped, or Docker engine not running | `docker start blastradius-falkordb`, then `docker version` to confirm the engine |
| `NOAUTH Authentication required` | container started without `REDIS_ARGS` | remove it (`docker rm -f blastradius-falkordb`) and re-run the `docker run` command from step 3 |
| App shows **Database unreachable** | same as above | the page prints the exact error and target |
| `ModuleNotFoundError: blastradius` | running with system Python, not the venv | use `.venv/Scripts/python.exe -m ...` |
| `python` opens the Microsoft Store (Windows) | App Execution Alias shadows the real Python | use the venv path, or disable the alias in Settings → Apps → Advanced app settings |
| Empty tables in the UI | graph seeded into a different name | check `FALKORDB_GRAPH` in `.env`, then `python -m blastradius.cli verify` |
| `Index already exists` during seeding | harmless when indexes exist; older versions of this seeder raised here | fixed: `create_indexes` skips indexes FalkorDB already reports |

Wipe the demo graph and rebuild it at any time:

```bash
.venv/Scripts/python.exe -m blastradius.cli seed --reset
```

Remove everything, including the volume:

```bash
docker rm -f blastradius-falkordb
docker volume rm blastradius-falkordb-data
```

---

## Design notes and limitations

**Determinism.** The topology is hand-authored; numeric attributes come from
`random.Random(1337)`. Two runs produce byte-identical datasets (asserted in the tests), so
screenshots and demo numbers stay stable.

**No mocks.** Query results are never cached to disk or fabricated. There is no
fallback graph and no sample JSON. The five dashboard tabs remain pure Cypher: no model in
the loop, no client-side generation. The only optional model involvement is the Agent tab's
tool *selection*; tool *results* are always untouched Cypher returns. The only cached object
is the Redis connection itself (`st.cache_resource`), reused across page reruns.

**Agent honesty.** The agent can be wrong about *which* tool to ask first, and it may
express that uncertainty; it cannot be wrong about *facts*, because every fact it is shown
reaches it as a tool result from the database. When the model calls a tool with arguments
that do not validate, the rejection is recorded and shown in the UI. When the model asks
for more than four executions, the excess calls are visibly skipped (`skipped:
budget_exceeded`), never silently dropped. And when the local model is not installed, the
UI says so plainly rather than pretending a rule-based guesser is an LLM.

**Paths are the evidence.** For each affected service the application keeps the shortest
returned path and renders every node and edge, including `protocol`, `criticality` and
`calls_per_min`. The graph view draws only those paths.

**Scope.** The traversal follows `DEPENDS_ON` (and `READS_FROM`/`WRITES_TO` for datastore
origins). It deliberately ignores `PUBLISHES_TO`/`SUBSCRIBES_TO` when computing blast radius:
topic fan-out depends on retention and consumer lag, which this dataset does not model — so
including it would overstate impact. Incident replay is bounded to 4 hops.

**Scale.** This is a demo estate (88 nodes, 267 edges). The traversal is bounded to 6 hops to
keep ambiguous dense graphs comprehensible; the same queries run unchanged on much larger
graphs, where the `id` indexes carry the lookups.

**Not included.** Authentication, multi-tenancy, live metric ingestion and write-back to a
real service catalogue. The graph is read-only from the application's perspective, except
for the explicit "Reseed demo data" action.

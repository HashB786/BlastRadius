# BlastRadius

**Multi-hop impact analysis for microservice estates, powered by a real FalkorDB graph.**

When something breaks at 2am, the useful question is not "which service is red?"
It is *"if I touch this, what else falls over, who owns it, and has this happened before?"*

BlastRadius answers that from one graph. Give it a failing service or datastore and it walks
`DEPENDS_ON` relationships **backwards** through FalkorDB, returning every affected service,
the **exact relationship path** that connects it to the failure, the teams you need to page,
and the incidents that touched the same component before.

No LLM is involved and no result is generated on the client. Every number, path and name on
screen is the return value of a Cypher query executed against a live FalkorDB server.

Built for the **WeMakeDevs Graph Hacks** hackathon.

---

## Contents

- [What you get](#what-you-get)
- [Why FalkorDB is the core](#why-falkordb-is-the-core)
- [Graph model](#graph-model)
- [Quickstart](#quickstart)
- [Environment variables](#environment-variables)
- [Using the app](#using-the-app)
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
| Professional UI | `app.py` | Overview, Blast radius, Ownership, Incidents, Graph view |
| Automated tests | `tests/` | 37 tests, including live-database integration assertions |

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

`.env` is git-ignored. `.env.example` is safe to commit because it holds no real secret.
Nothing in this repository needs a paid service, a credit card, or an LLM API key.

---

## Using the app

| Tab | What it does |
|---|---|
| **Overview** | Live node/edge/dependency counts, the most depended-on services, services per tier, severity mix, newest incidents. |
| **Blast radius** | Pick a service or datastore, choose 1–6 hops, and get the affected services with `hops`, owning teams, and the real path. Metrics show impacted services, direct dependents, teams to page and the deepest path. "Trace a single path" breaks a route into node/edge steps with protocol and criticality. The **Cypher executed** expander shows the exact statement. |
| **Ownership** | Owning team, Slack channel, cost centre, timezone and the engineers on rota. A team view lists everything that team owns plus incident history that touched it. |
| **Incidents** | Severity, duration and root cause for every incident; open one to see the recorded affected services and the downstream paths the graph returns for its root cause. |
| **Graph view** | The same blast radius rendered as a Graphviz diagram, with tier colours and real relationship labels. |

**Missing data is never papered over.** Type an unknown id in *Blast radius → Look up an id
directly* and the page says the component is not in the graph, offers close matches, and
shows no substitute results — the CLI exits with status `2` in the same situation.

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
.....................................                                    [100%]
37 passed
```

What is covered:

- dataset determinism, unique ids, acyclic dependencies, every service owned, every incident
  referencing a real component (`tests/test_data.py`, no database needed);
- live health round-trip, seed counts read back from FalkorDB, direct and 3-hop impact,
  datastore failure traversal, path/edge integrity, depth clamping;
- honest missing-service behaviour plus typo suggestions;
- ownership for services and teams;
- incident history for a service *and* for a datastore root cause, including the
  recorded-vs-traversed distinction (`tests/test_queries.py`).

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

---

## Project layout

```text
BlastRadius/
├── src/blastradius/
│   ├── app.py          Streamlit UI (5 tabs, live queries)
│   ├── cli.py          health / seed / verify / impact / owners / incidents / overview
│   ├── config.py       FALKORDB_* configuration
│   ├── data.py         deterministic synthetic estate (fixed seed 1337)
│   ├── db.py           FalkorDB client, path conversion, health report
│   ├── queries.py      the Cypher query layer
│   └── seed.py         MERGE-based writes, indexes, verification counts
├── tests/
│   ├── conftest.py     live database fixtures (separate test graph)
│   ├── test_data.py    dataset invariants, no database required
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

**No mocks, no LLM.** Query results are never cached to disk or fabricated. There is no
fallback graph, no sample JSON, and no model in the loop. The only cached object is the Redis
connection itself (`st.cache_resource`), which is reused across page reruns.

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

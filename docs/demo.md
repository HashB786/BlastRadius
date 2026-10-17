# Three-minute demo script

Exact numbers from the seeded demo graph (`40` services, `90` `DEPENDS_ON` edges, `10`
incidents). Start with everything running:

```bash
docker start blastradius-falkordb
.venv/Scripts/python.exe -m blastradius.cli health
.venv/Scripts/python.exe -m streamlit run src/blastradius/app.py
```

Open <http://localhost:8501>.

---

## 0:00 — Overview: this is live

Point at the sidebar and the metrics row.

> "40 services, 8 teams, 90 dependency edges, 10 incidents. The badge says `Live FalkorDB`,
> and the round-trip time beside it is measured on this page load — those numbers come from
> `MATCH (n) RETURN count(n)` and friends, not from a JSON fixture."

Call out the bar chart: **Config Service** has **15 direct dependents**. That is the setup
for the climax.

---

## 0:30 — Blast radius: a payments failure

**Blast radius** tab → `Payment Service (payment-service)` → depth **4**.

Expected:

| Metric | Value |
|---|---|
| Impacted services | **5** |
| Direct dependents | **2** |
| Teams to page | **4** |
| Deepest path | **3 hops** |

Read one path out loud:

```text
API Gateway -> Web Backend-for-Frontend -> Checkout Service -> Payment Service
```

> "That chain is the return value of `MATCH p = (dependent:Service)-[:DEPENDS_ON*1..4]->(origin)`.
> FalkorDB gives me the path object, so I can show the protocol and criticality of every edge —
> and when the page refreshes in five minutes, this is still what the database says."

Open **Trace a single path** → the hop table shows `DEPENDS_ON`, `gRPC`, `critical` for the
`Checkout Service -> Payment Service` edge.

Open **Cypher executed against FalkorDB** and show the statement with `$service_id`.

---

## 1:10 — The datastore story

Change the picker to `cart-cache (redis-cart)`, depth **3**.

Expected:

| Metric | Value |
|---|---|
| Impacted services | **4** |
| Teams to page | **2** |
| Deepest path | **3 hops** |

> "A datastore is not a service, but it fails the same way. The traversal starts at every
> service that reads or writes it, then climbs the service graph. The last hop of the path is
> the real `READS_FROM` or `WRITES_TO` edge."

The `Cart Service -> cart-cache` row's path ends at the store, which is why the origin card
shows no tier — the UI does not invent one.

---

## 1:35 — Incident history: has this happened before?

**Incidents** tab. The newest incident is **INC-2041**, *Checkout latency spike after cart
cache eviction*, SEV1, 74 minutes.

> "Root cause: `cart-cache` — a datastore. Four services recorded as affected at the time.
> Downstream paths below were recomputed just now by the same query the Blast radius tab uses,
> so the incident record and today's graph can't drift apart."

Then **Ownership** → `Ledger Service (ledger-service)`:

- Team **Payments Platform**, `#payments-oncall`, `CC-1003`, `UTC`
- Engineers: Daniel Okafor (Staff Engineer), Hugo Lindqvist (SRE), Sofia Rossi (Senior Backend Engineer)
- Incident history for this service: **INC-2029**, root cause, 96 minutes.

> "Two hops from service to team to engineer, and the incident that this team owns. That is
> the whole on-call handoff in one screen."

---

## 2:10 — Platform failure: everything at once

Back to **Blast radius** → `Config Service (config-service)`, depth **3**.

Expected:

| Metric | Value |
|---|---|
| Impacted services | **37** |
| Teams to page | **8** |
| Deepest path | **2 hops** |

> "Fifteen services call Config Service directly; almost the entire estate reaches it within
> two hops. This is the query doing real work: 124 paths collapsed to 37 unique services, each
> keeping its shortest route."

**Graph view** tab, same component: the diagram is drawn only from returned paths, coloured by
tier, edges labelled with real relationship types.

---

## 2:40 — Agent investigation: real AI, real graph (world first here)

**Agent investigation** tab (6th tab). It should show `Ollama reachable · model loaded:
qwen3.5:2b`. In the question box, type it in plain English:

> *"What breaks if payment-service fails, which teams own the affected services, and have we
> seen a similar incident?"*

Press **Run agent investigation**. While it thinks, say:

> "There is a model in the loop — not in any cloud: qwen3.5 is running in Ollama on this very
> laptop. Its whole job is to choose tools. It gets a menu of exactly three validated tools
> — impact analysis, ownership, incident history — and a list of real service ids. It never
> sees or writes Cypher."

Expected transcript on screen, each numbered step backed by its tool result:

1. **`impact_analysis` with `payment-service`** — status `ok`; the evidence table lists the
   affected services with the real `->` paths (`API Gateway -> Web Backend-for-Frontend ->
   Checkout Service -> Payment Service`).
2. **`ownership_lookup`** (model reuses the ids it learned) — teams with Slack channels.
3. **`incident_history`** — the incidents the graph actually records for those services.

Then the agent answer, citing exactly those records. Per-fact reassurance:

> "The AI only chose *which verified query to run*. Every fact above came out of FalkorDB —
> cross-check any claim against the evidence tables right above its answer."

Point at the honesty rails: four-tool hard budget, argument validation, and that an unknown
id gives `not_found` with close matches instead of a hallucinated story.

---

## 2:55 — Honest failure

Back in **Blast radius**, open **Look up an id directly**, type `paymnt-service`, press Enter.

Expected: an error reading

```text
'paymnt-service' is not in the graph, so no impact can be computed.
Nothing was assumed or substituted.
```

plus the closest matches: `payment-service`, `cart-service`, `auth-service`, `tax-service`,
`pricing-service`.

> "No nearest-neighbour guess is presented as an answer. The graph is the source of truth, and
> when it has nothing to say, neither do we."

Optionally show the terminal equivalent returning exit code 2:

```bash
.venv/Scripts/python.exe -m blastradius.cli impact paymnt-service; echo "exit=$?"
```

---

## Closing line

> "FalkorDB is not decoration here. Every conclusion in this UI is a Cypher traversal, the
> evidence is the path itself, and the tests run against a live server — 56 of them,
> including a check that a three-hop chain really comes back as three hops, and one that runs
> a real local model picking real tools and gets its facts from the same graph."

---

## Reset between rehearsals

```bash
.venv/Scripts/python.exe -m blastradius.cli seed --reset
```

The dataset is deterministic (seed `1337`), so every number above reproduces exactly.

If you are rehearsing repeatedly, keep the app open: the sidebar's **Reseed demo data** action
(behind a confirmation checkbox) does the same thing without leaving the browser.

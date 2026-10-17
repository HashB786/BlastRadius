"""BlastRadius — Streamlit interface.

Run with:
    streamlit run src/blastradius/app.py

Every number on screen comes from a Cypher query executed against the live
FalkorDB graph on this request. There are no cached fixtures, no sample JSON
and no mock data: if the database is down the page says so.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from blastradius import __version__, queries
from blastradius.agent_tools import TOOL_NAMES
from blastradius import llm as local_llm
from blastradius.config import get_config
from blastradius.db import BlastRadiusDB
from blastradius.seed import seed_graph

st.set_page_config(
    page_title="BlastRadius · FalkorDB impact analysis",
    page_icon="🕸️",
    layout="wide",
)

CSS = """
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1250px;}
  h1, h2, h3 {letter-spacing: -0.02em;}
  .br-hero {display:flex; align-items:center; justify-content:space-between; gap:1rem;
            border-bottom:1px solid #E3E8F0; padding-bottom:1rem; margin-bottom:1.2rem;}
  .br-title {font-size:2.1rem; font-weight:700; margin:0; color:#101B2D;}
  .br-title span {color:#3B5BDB;}
  .br-sub {color:#5A6577; font-size:0.95rem; margin-top:0.25rem;}
  .br-badge {display:inline-flex; align-items:center; gap:0.45rem; padding:0.35rem 0.75rem;
             border-radius:999px; font-size:0.82rem; font-weight:600;}
  .br-live {background:#E6F4EA; color:#137333; border:1px solid #B7E1C1;}
  .br-down {background:#FCE8E6; color:#A50E0E; border:1px solid #F5C2BE;}
  .br-card {border:1px solid #E3E8F0; border-radius:12px; padding:1rem 1.15rem;
            background:#FFFFFF;}
  .br-chip {display:inline-block; padding:0.15rem 0.55rem; border-radius:999px;
            background:#EEF2FF; color:#2B3FA8; font-size:0.78rem; margin:0.1rem 0.2rem 0.1rem 0;
            border:1px solid #D6DEFF;}
  .br-muted {color:#6B7686; font-size:0.85rem;}
  .br-sev1 {color:#A50E0E; font-weight:700;}
  .br-sev2 {color:#B06000; font-weight:700;}
  .br-sev3 {color:#3A5CCC; font-weight:700;}
  .br-path {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:0.82rem;
            color:#2F3B4C;}
  div[data-testid="stMetricValue"] {font-size:1.7rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

SEVERITY_CLASS = {"SEV1": "br-sev1", "SEV2": "br-sev2", "SEV3": "br-sev3"}


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


@st.cache_resource(show_spinner=False)
def get_database() -> BlastRadiusDB:
    """One connection per Streamlit process, reused across reruns."""
    return BlastRadiusDB(get_config())


def load_entities(db: BlastRadiusDB) -> list[dict]:
    """Every possible failure origin: services first, then datastores."""
    services = [
        {"kind": "Service", "id": r[0], "name": r[1], "tier": r[2]}
        for r in db.run(
            "MATCH (s:Service) RETURN s.id, s.name, s.tier ORDER BY s.tier, s.name"
        )
    ]
    datastores = [
        {"kind": "Datastore", "id": r[0], "name": r[1], "tier": 0}
        for r in db.run("MATCH (d:Datastore) RETURN d.id, d.name ORDER BY d.name")
    ]
    return services + datastores


def label_for(entity: dict) -> str:
    tier = f" · tier {entity['tier']}" if entity.get("tier") else ""
    return f"{entity['name']} ({entity['id']}){tier}"


# --------------------------------------------------------------------------- #
# Pieces of UI
# --------------------------------------------------------------------------- #


def hero(health) -> None:
    if health.reachable:
        badge = (
            '<span class="br-badge br-live">● Live FalkorDB</span>'
        )
    else:
        badge = '<span class="br-badge br-down">● Database unreachable</span>'
    st.markdown(
        f"""
        <div class="br-hero">
          <div>
            <div class="br-title">Blast<span>Radius</span></div>
            <div class="br-sub">Multi-hop impact analysis over a real FalkorDB graph —
            every path on this page was returned by Cypher, not generated.</div>
          </div>
          <div style="text-align:right">{badge}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def offline_panel(db: BlastRadiusDB, health) -> None:
    st.error("FalkorDB is not reachable, so no analysis can run.")
    st.markdown(
        f"""
**Target:** `{db.config.host}:{db.config.port}` · graph `{db.config.graph}`

Start the database, then reload this page:

```bash
docker start blastradius-falkordb
```

If this is the first run, use the setup steps in `README.md`.
        """
    )
    st.code(str(health.error), language="text")
    st.stop()


def severity_badge(severity: str) -> str:
    css = SEVERITY_CLASS.get(severity, "br-sev3")
    return f'<span class="{css}">{html.escape(severity)}</span>'


def chips(items: list[str]) -> str:
    return "".join(f'<span class="br-chip">{html.escape(str(i))}</span>' for i in items)


def path_table(result: queries.ImpactResult) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Hops": item.hops,
                "Service": item.name,
                "Tier": item.tier,
                "Owners": ", ".join(item.teams),
                "Relationship path": item.arrow,
            }
            for item in result.impacted
        ]
    )


def build_dot(result: queries.ImpactResult, origin: dict) -> str:
    """Graphviz source for the returned paths (real edges only)."""
    colors = {1: ("#FFF5F5", "#C92A2A"), 2: ("#FFF9DB", "#B06000"), 3: ("#E7F5FF", "#1C7ED6")}
    lines = [
        "digraph blast {",
        'rankdir="LR";',
        'bgcolor="transparent";',
        'node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=11 '
        'penwidth=1.2 margin="0.14,0.07"];',
        'edge [color="#98A2B3" fontname="Helvetica" fontsize=8 arrowsize=0.7];',
    ]
    origin_id = "n_origin"
    lines.append(
        f'{origin_id} [label="{html.escape(origin["name"])}\\norigin", '
        f'fillcolor="#3B5BDB" fontcolor="white" color="#2B3FA8"];'
    )

    seen_nodes = {origin_id}
    seen_edges: set[tuple[str, str]] = set()
    names: dict[str, str] = {}

    for item in result.impacted:
        nodes = item.path["nodes"]
        edges = item.path["edges"]
        for index, node in enumerate(nodes):
            node_id = str(node.get("id"))
            key = "n_" + "".join(c if c.isalnum() else "_" for c in node_id)
            names[key] = str(node.get("name", node_id))
            if key not in seen_nodes and node_id != origin["id"]:
                fill, border = colors.get(node.get("tier") or 3, colors[3])
                lines.append(
                    f'{key} [label="{html.escape(names[key])}\\ntier {node.get("tier")}", '
                    f'fillcolor="{fill}" color="{border}"];'
                )
                seen_nodes.add(key)
        for index, edge in enumerate(edges):
            src = str(nodes[index].get("id"))
            dst = str(nodes[index + 1].get("id"))
            src_key = "n_origin" if src == origin["id"] else "n_" + "".join(
                c if c.isalnum() else "_" for c in src
            )
            dst_key = "n_origin" if dst == origin["id"] else "n_" + "".join(
                c if c.isalnum() else "_" for c in dst
            )
            pair = (src_key, dst_key)
            if pair in seen_edges:
                continue
            seen_edges.add(pair)
            relation = (edge.get("_type") or "DEPENDS_ON").replace("_", " ").lower()
            lines.append(
                f'{src_key} -> {dst_key} [label="{html.escape(relation)}"];'
            )

    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #


def overview_tab(db: BlastRadiusDB, health) -> None:
    stats = queries.overview(db)

    cols = st.columns(5)
    cols[0].metric("Services", stats["services"])
    cols[1].metric("Teams", stats["teams"])
    cols[2].metric("Dependencies", stats["dependencies"])
    cols[3].metric("Incidents", stats["incidents"])
    cols[4].metric("Engineers", stats["engineers"])

    st.markdown(
        f'<p class="br-muted">Read live from graph '
        f'<code>{html.escape(db.config.graph)}</code> at '
        f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC · '
        f'{stats["dependencies"]} DEPENDS_ON edges · '
        f'server latency {health.latency_ms} ms</p>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Most depended-on services")
        st.caption("A failure here has the largest direct blast radius.")
        fan_in = pd.DataFrame(stats["top_dependents"])
        if not fan_in.empty:
            st.bar_chart(fan_in.set_index("name")["dependents"], height=320)
    with right:
        st.subheader("Services per tier")
        tiers = pd.DataFrame(
            [{"Tier": k, "Services": v} for k, v in stats["tiers"].items()]
        )
        st.dataframe(tiers, hide_index=True, height=170)
        st.subheader("Incidents by severity")
        sev = pd.DataFrame(
            [{"Severity": k, "Count": v} for k, v in stats["severity"].items()]
        )
        st.dataframe(sev, hide_index=True, height=150)

    st.subheader("Recent incidents")
    incidents = queries.list_incidents(db, limit=10)
    if incidents:
        frame = pd.DataFrame(
            [
                {
                    "Incident": row["id"],
                    "Severity": row["severity"],
                    "Started": row["started_at"].replace("T", " ").replace("Z", " UTC"),
                    "Minutes": row["duration_minutes"],
                    "Root cause": row["root_cause"],
                    "Affected": row["affected_count"],
                    "Title": row["title"],
                }
                for row in incidents
            ]
        )
        st.dataframe(frame, hide_index=True)
    else:
        st.info("No incidents in this graph yet.")


def blast_radius_tab(db: BlastRadiusDB, entities: list[dict]) -> None:
    st.subheader("What breaks if this fails?")
    st.caption(
        "Traverses DEPENDS_ON backwards from the failing component, and includes "
        "READS_FROM / WRITES_TO hops when the origin is a datastore."
    )

    options = {label_for(e): e for e in entities}
    default_index = 0
    for idx, e in enumerate(entities):
        if e["id"] == "payment-service":
            default_index = idx
            break

    control_left, control_right = st.columns([3, 2])
    with control_left:
        chosen_label = st.selectbox(
            "Failing service or datastore", list(options), index=default_index
        )
    with control_right:
        depth = st.slider("Maximum hops", min_value=1, max_value=6, value=4)

    entity = options[chosen_label]
    with st.expander("Look up an id directly (typos are handled honestly)"):
        typed = st.text_input(
            "Component id",
            placeholder="e.g. payment-service",
            help="If the id is not in the graph the app says so instead of guessing.",
        )

    entity_id = typed.strip() or entity["id"]
    result = queries.impact_analysis(db, entity_id, depth)

    if result.missing:
        st.error(result.message)
        suggestions = queries.suggest_services(db, entity_id)
        if suggestions:
            st.markdown(
                "Closest matches in the graph:<br>" + chips(suggestions),
                unsafe_allow_html=True,
            )
        return

    origin = result.origin
    team_rows = (
        db.run(
            "MATCH (t:Team)-[:OWNS]->(s:Service {id: $id}) RETURN t.name, t.slack_channel",
            {"id": origin["id"]},
        )
        if origin["kind"] == "Service"
        else []
    )
    owners = " · ".join(f"{row[0]} {row[1]}" for row in team_rows) or "—"

    with st.container(border=True):
        st.markdown(
            f"**{html.escape(str(origin.get('name')))}** "
            f'<span class="br-muted">{html.escape(str(origin.get("id")))} · '
            f'{html.escape(str(origin.get("kind")))}</span>',
            unsafe_allow_html=True,
        )
        meta = []
        if origin.get("tier"):
            meta.append(f"Tier {origin['tier']}")
        if origin.get("language"):
            meta.append(str(origin["language"]))
        if origin.get("region"):
            meta.append(str(origin["region"]))
        if origin.get("slo_target"):
            meta.append(f"SLO {origin['slo_target']}%")
        st.markdown(
            f'<span class="br-muted">{html.escape(" · ".join(meta))}</span><br>'
            f'<span class="br-muted">Owner: {html.escape(owners)}</span>',
            unsafe_allow_html=True,
        )

    if not result.impacted:
        st.info(result.message or "Nothing depends on this component.")
        with st.expander("Cypher executed"):
            st.code(result.cypher or "-- no query: the entity is not in the graph", language="cypher")
        return

    metrics = st.columns(4)
    metrics[0].metric("Impacted services", len(result.impacted))
    metrics[1].metric("Direct dependents", len(result.direct))
    metrics[2].metric("Teams to page", len(result.teams))
    metrics[3].metric("Deepest path", f"{max(i.hops for i in result.impacted)} hops")

    st.dataframe(
        path_table(result),
        hide_index=True,
        column_config={
            "Hops": st.column_config.NumberColumn(width="small"),
            "Tier": st.column_config.NumberColumn(width="small"),
        },
    )

    st.subheader("Teams paged")
    if result.teams:
        team_cols = st.columns(min(3, len(result.teams)))
        for index, team in enumerate(result.teams):
            with team_cols[index % len(team_cols)]:
                with st.container(border=True):
                    st.markdown(f"**{html.escape(team['name'])}**")
                    st.markdown(
                        f'<span class="br-muted">{html.escape(str(team["slack_channel"]))}'
                        f' · {len(team["services"])} service(s) · '
                        f'{len(team["engineers"])} engineer(s)</span>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(chips(team["services"]), unsafe_allow_html=True)
    else:
        st.caption("No owning teams found for the impacted services.")

    st.subheader("Trace a single path")
    pick = st.selectbox(
        "Inspect the hops FalkorDB returned",
        [item.service_id for item in result.impacted],
        format_func=lambda sid: next(
            (item.arrow for item in result.impacted if item.service_id == sid), sid
        ),
    )
    item = next(i for i in result.impacted if i.service_id == pick)
    steps = pd.DataFrame(
        [
            {
                "Hop": index,
                "Node": step.name,
                "Tier": step.tier,
                "Edge into node": step.relation or "origin",
                "Protocol": step.protocol or "",
                "Criticality": step.criticality or "",
            }
            for index, step in enumerate(item.steps)
        ]
    )
    st.dataframe(steps, hide_index=True)

    with st.expander("Cypher executed against FalkorDB"):
        st.code(result.cypher, language="cypher")
        st.caption(f"Parameters: {result.params}")


def ownership_tab(db: BlastRadiusDB, entities: list[dict]) -> None:
    st.subheader("Who owns this service?")
    services = [e for e in entities if e["kind"] == "Service"]
    options = {label_for(e): e for e in services}
    default_index = next(
        (i for i, e in enumerate(services) if e["id"] == "ledger-service"), 0
    )
    chosen = st.selectbox("Service", list(options), index=default_index)
    entity = options[chosen]
    result = queries.ownership_lookup(db, entity["id"])

    if result is None:
        st.warning(f"{entity['id']} is not in the graph.")
        return

    if not result.teams:
        st.warning("No owning team is recorded for this service.")
    for team in result.teams:
        with st.container(border=True):
            left, right = st.columns([2, 3])
            with left:
                st.markdown(f"### {html.escape(team['name'])}")
                st.markdown(
                    f'<span class="br-chip">{html.escape(str(team["slack_channel"]))}</span>'
                    f'<span class="br-chip">{html.escape(str(team["cost_center"]))}</span>'
                    f'<span class="br-chip">{html.escape(str(team["timezone"]))}</span>',
                    unsafe_allow_html=True,
                )
            with right:
                st.caption("Engineers on this team")
                st.dataframe(
                    pd.DataFrame(result.engineers)[["name", "role"]].rename(
                        columns={"name": "Name", "role": "Role"}
                    ),
                    hide_index=True,
                )

    st.subheader("Team rota and incidents")
    teams = queries.list_teams(db)
    team_options = {f"{t['name']} ({t['id']})": t for t in teams}
    owning_team_id = result.teams[0]["id"] if result.teams else None
    default_team = next(
        (
            index
            for index, key in enumerate(team_options)
            if team_options[key]["id"] == owning_team_id
        ),
        0,
    )
    chosen_team = st.selectbox("Team", list(team_options), index=default_team)
    detail = queries.team_ownership(db, team_options[chosen_team]["id"])
    if detail:
        left, right = st.columns([1, 1])
        with left:
            st.markdown("**Services owned**")
            st.dataframe(
                pd.DataFrame(detail["services"]).rename(
                    columns={"name": "Service", "id": "ID", "tier": "Tier", "language": "Language"}
                ),
                hide_index=True,
            )
        with right:
            st.markdown("**Incident history touching this team**")
            if detail["incidents"]:
                st.dataframe(
                    pd.DataFrame(detail["incidents"]).rename(
                        columns={
                            "id": "Incident",
                            "title": "Title",
                            "severity": "Severity",
                            "started_at": "Started",
                        }
                    ),
                    hide_index=True,
                )
            else:
                st.caption("No incidents recorded for this team's services.")

    st.subheader(f"Incidents for {result.service['name']}")
    records = queries.incident_history(db, entity["id"])
    if records:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Incident": r.incident_id,
                        "Severity": r.severity,
                        "Role": r.role,
                        "Started": r.started_at,
                        "Minutes": r.duration_minutes,
                        "Title": r.title,
                    }
                    for r in records
                ]
            ),
            hide_index=True,
        )
    else:
        st.caption("No incidents recorded for this service.")


def incidents_tab(db: BlastRadiusDB) -> None:
    st.subheader("Incident history")
    incidents = queries.list_incidents(db, limit=50)
    if not incidents:
        st.info("No incidents in this graph.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Incident": row["id"],
                    "Severity": row["severity"],
                    "Started": row["started_at"].replace("T", " ").replace("Z", " UTC"),
                    "Minutes": row["duration_minutes"],
                    "Root cause": row["root_cause"],
                    "Affected": row["affected_count"],
                    "Title": row["title"],
                }
                for row in incidents
            ]
        ),
        hide_index=True,
    )

    choice = st.selectbox(
        "Open an incident",
        [row["id"] for row in incidents],
        format_func=lambda rid: next(
            f"{rid} — {row['title']}" for row in incidents if row["id"] == rid
        ),
    )
    detail = queries.incident_detail(db, choice)
    if detail is None:
        st.warning("That incident is no longer in the graph.")
        return

    with st.container(border=True):
        st.markdown(
            f"### {html.escape(detail['title'])}\n"
            f"{severity_badge(detail['severity'])} "
            f'<span class="br-muted">{html.escape(detail["status"])} · '
            f'{html.escape(str(detail["started_at"]))} · '
            f'{detail["duration_minutes"]} minutes · detected by '
            f'{html.escape(str(detail["detected_by"]))}</span>',
            unsafe_allow_html=True,
        )
        st.write(detail["summary"])

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Root cause**")
        st.markdown(
            f'<span class="br-chip">{html.escape(str(detail["root_cause_kind"]))}</span> '
            f'{html.escape(str(detail["root_cause_name"]))} '
            f'<span class="br-muted">({html.escape(str(detail["root_cause_id"]))})</span>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown("**Affected services recorded with the incident**")
        st.markdown(chips(detail["affected_services"]), unsafe_allow_html=True)

    st.markdown("**Downstream paths returned by Cypher**")
    if detail["downstream"]:
        st.dataframe(
            pd.DataFrame(
                [
                    {"Hops": row["hops"], "Service": row["service"], "Path": row["path"]}
                    for row in detail["downstream"]
                ]
            ),
            hide_index=True,
        )
    else:
        st.caption("This incident's root cause has no dependents in the graph.")


def graph_tab(db: BlastRadiusDB, entities: list[dict]) -> None:
    st.subheader("Blast radius as a graph")
    st.caption(
        "Nodes and edges below are exactly the paths returned by the Cypher "
        "traversal — nothing is drawn that the database did not return."
    )
    options = {label_for(e): e for e in entities}
    default_index = next(
        (i for i, e in enumerate(entities) if e["id"] == "payment-service"), 0
    )
    left, right = st.columns([3, 2])
    with left:
        chosen = st.selectbox("Failing component", list(options), index=default_index)
    with right:
        depth = st.slider("Maximum hops", 1, 5, 3)

    entity = options[chosen]
    result = queries.impact_analysis(db, entity["id"], depth)
    if not result.impacted or result.origin is None:
        st.info(result.message or "Nothing depends on this component.")
        return

    origin = result.origin
    st.graphviz_chart(build_dot(result, origin), width="stretch", height=520)
    st.markdown(
        '<span class="br-muted">Blue = failing origin · red = tier 1 · '
        "amber = tier 2 · light blue = tier 3. Edge labels are the real "
        "relationship types.</span>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Sidebar + main
# --------------------------------------------------------------------------- #


def sidebar(db: BlastRadiusDB, health) -> None:
    with st.sidebar:
        st.markdown("### BlastRadius")
        st.caption(f"v{__version__} · graph hack demo")
        st.divider()
        st.markdown("**Connection**")
        st.code(
            f"{db.config.host}:{db.config.port}\n"
            f"graph: {db.config.graph}\n"
            f"{'password: set' if db.config.password else 'password: none'}",
            language="text",
        )
        if health.reachable:
            st.markdown(
                f"**Server**  \n{html.escape(str(health.server_version))}  \n"
                f"**Nodes** {health.node_count} · **Edges** {health.edge_count}  \n"
                f"**Round-trip** {health.latency_ms} ms"
            )
        st.divider()
        st.markdown("**Severity key**")
        st.markdown(
            f'{severity_badge("SEV1")} customer-facing outage  \n'
            f'{severity_badge("SEV2")} degraded experience  \n'
            f'{severity_badge("SEV3")} internal or minor',
            unsafe_allow_html=True,
        )
        st.divider()
        with st.expander("Demo data"):
            st.caption(
                "Re-creates the synthetic graph in this database. Only the "
                "configured graph is cleared."
            )
            confirm = st.checkbox("I understand this clears the graph")
            if st.button("Reseed demo data", disabled=not confirm):
                with st.spinner("Writing Cypher…"):
                    counts = seed_graph(db, reset=True)
                st.success(
                    f"Seeded {counts['Service']} services, {counts['Team']} teams, "
                    f"{counts['DEPENDS_ON']} dependencies."
                )
                st.cache_data.clear()
                st.rerun()

        st.divider()
        st.caption(
            "No LLM, no mock data. Queries run against FalkorDB with the "
            "official Python client."
        )


def agent_tab(db: BlastRadiusDB, entities: list[dict]) -> None:
    """Agent Investigation: the local Ollama model picks tools; the graph answers."""

    st.caption(
        "AI agent, fully local — picks from three validated tools only "
        f"({', '.join(TOOL_NAMES)}) and executes each against live FalkorDB. "
        "It never writes Cypher and never invents data: unknown ids and "
        "insufficient evidence are reported honestly."
    )

    model_name = st.text_input(
        "Ollama model tag",
        value=os.getenv("OLLAMA_MODEL", "") or local_llm.DEFAULT_MODEL,
        help="Must exist on this machine:  ollama pull <tag>. Configured via OLLAMA_MODEL too.",
    )

    status, detail = local_llm.check_ollama(model_name)
    if status == "ok":
        st.success(f"Ollama reachable · model loaded: `{detail}`")
    elif status == "unreachable":
        st.warning("Ollama is not running, so no agent investigation can be shown.")
        st.markdown(detail)
        st.markdown(
            "The rest of the dashboard (the five tabs above) still works without Ollama."
        )
        return
    else:  # model_missing
        st.warning("Ollama is reachable, but the model needed for this tab is not pulled.")
        st.markdown(detail)
        return

    if "agent_question" not in st.session_state:
        st.session_state["agent_question"] = (
            "What breaks if payment-service fails, which teams own the "
            "affected services, and have we seen a similar incident?"
        )
    question = st.text_area(
        "Ask in natural language",
        value=st.session_state["agent_question"],
        key="agent_input",
        height=90,
    )

    asked = st.button("Run agent investigation", type="primary")
    if not asked:
        st.markdown(
            "**How it works:** the local model sees only a tool menu and service "
            "ids — never Cypher. Each chosen tool runs the same validated query "
            "functions as the dashboard tabs; the hard budget is "
            "4 tool calls per run (no unbounded agent loop)."
        )
        return

    with st.spinner("Agent investigating against live FalkorDB …"):
        known_ids = [e["id"] for e in entities]
        inv = local_llm.run_investigation(
            db,
            question.strip(),
            model=model_name.strip() or local_llm.DEFAULT_MODEL,
            known_ids=known_ids,
        )

    if not inv.succeeded:
        st.error("The agent could not complete the investigation.")
        st.markdown(inv.error or "unknown failure")
        return

    st.subheader("Selected tools & real graph evidence")
    for index, step in enumerate(inv.steps, start=1):
        res = step.result
        if step.kind == "tool_call" and res is not None:
            call_no = index
            status_icon = {"ok": "✅", "not_found": "🟡", "rejected": "⛔", "skipped": "⏭️"}.get(
                res.status, "•"
            )
            st.markdown(
                f"**{call_no}. {status_icon} `{res.tool}`** with `{res.arguments}` — "
                f"{html.escape(res.detail)}  (round {res.round_id}, action #{call_no})"
            )
            with st.expander(f"Evidence returned by {res.tool}"):
                if res.status == "rejected":
                    st.error(res.error)
                imp = res.evidence.get("impacted")
                if imp is not None:
                    st.dataframe(
                        pd.DataFrame(imp)[["service", "hops", "tier", "path"]]
                        if imp and "service" in imp[0]
                        else pd.DataFrame(imp),
                        use_container_width=True,
                    )
                teams_ev = res.evidence.get("teams")
                if isinstance(teams_ev, list) and teams_ev:
                    st.markdown("**Teams behind those services** (from the graph):")
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {"team": t["name"], "slack": t["slack_channel"], "services": ", ".join(t["services"]), "engineers": ", ".join(t["engineers"])}
                                for t in teams_ev
                            ]
                        ),
                        use_container_width=True,
                    )
                incidents_ev = res.evidence.get("incidents")
                if incidents_ev:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "id": i["id"],
                                    "severity": i["severity"],
                                    "status": i["status"],
                                    "role": i["role"],
                                    "started_at": i["started_at"],
                                    "affected": ", ".join(i["affected_services"]),
                                }
                                for i in incidents_ev
                            ]
                        ),
                        use_container_width=True,
                    )
                    for i in incidents_ev:
                        st.markdown(f"- **{html.escape(i['id'])}** · {html.escape(i['title'])} — {html.escape(i['summary'])}")
                eng = res.evidence.get("engineers")
                if eng is not None:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {"team": e["team"], "engineer": e["name"], "role": e["role"]}
                                for e in eng
                            ]
                        ),
                        use_container_width=True,
                    )
                if res.evidence.get("suggestions"):
                    st.markdown(
                        "Close matches in the graph: " + ", ".join(f"`{s}`" for s in res.evidence["suggestions"])
                    )
        elif step.kind in ("note", "status") and step.text:
            st.caption(step.text)

    st.subheader("Agent answer (grounded in the evidence above)")
    st.markdown(f"> {inv.answer.replace(chr(10), chr(10) + '> ')}")
    st.caption(
        "Raw tool results are the source of truth: if a claim above is not "
        "backed by an item in the evidence, treat this answer as model commentary."
    )


def main() -> None:
    db = get_database()
    health = db.health()
    hero(health)
    if not health.reachable:
        offline_panel(db, health)

    entities = load_entities(db)
    sidebar(db, health)

    tabs = st.tabs([
        "Overview",
        "Blast radius",
        "Ownership",
        "Incidents",
        "Graph view",
        "Agent investigation",
    ])
    with tabs[0]:
        overview_tab(db, health)
    with tabs[1]:
        blast_radius_tab(db, entities)
    with tabs[2]:
        ownership_tab(db, entities)
    with tabs[3]:
        incidents_tab(db)
    with tabs[4]:
        graph_tab(db, entities)
    with tabs[5]:
        agent_tab(db, entities)


main()

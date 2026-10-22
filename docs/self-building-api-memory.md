# Self-building API memory for agents

> Research report, 2026-10-06. Logbook: session #25, pinned note #7, decision #20, tasks #14-16.
> Status: research complete; design **proposed**, awaiting go-ahead.

## 1. Problem

The target API MCP server's instructions (WORKFLOW, GOTCHAS a-g, DEVICE MODEL) are maintained by hand.
Every time an agent discovers how the target API really behaves, a human copies that knowledge into the
server instructions. The goal is for the agent to build and maintain its own **data dictionary** and
**tool context** over time, using the engineer logbook as the memory store.

## 2. TL;DR

- The logbook is the right foundation. It already has episodes with provenance (sessions + logs), a skill
  library (snippets), Postgres full-text search and a UI.
- What is missing is the **closed loop**:
  1. keyed entries that get *refined* instead of appended,
  2. a lifecycle (`proposed -> verified -> core -> superseded`),
  3. a render path that pushes curated knowledge back into the API tool context automatically.
- Recommendation: a domain-scoped `kb_entries` store in the logbook with delta-style tools. The API MCP
  consumes it through startup-compiled instructions, a live lookup tool, and a deterministic error-hint hook.

## 3. Research findings

### 3.1 Context engineering and memory (Anthropic)

- **Effective context engineering for AI agents.** Treat context as a finite "attention budget". Prefer
  *just-in-time* retrieval: keep lightweight identifiers (paths, stored queries, links) and load data through
  tools at runtime. *Structured note-taking* (e.g. a `NOTES.md`) persists knowledge outside the context
  window. Hybrid strategy: a small file loaded up front (CLAUDE.md) plus on-demand exploration.
- **Memory tool** (Claude Developer Platform). A client-side `/memories` directory the model can view,
  create and edit. The API auto-injects *"ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE ...
  ASSUME INTERRUPTION"*. Guidance: keep memory up to date, coherent and organised; scope what gets written
  ("only write down information relevant to <topic>"); cap file sizes; expire stale files; strip sensitive data.
- **Writing effective tools for agents.** Write tool descriptions as if onboarding a new hire: make implicit
  context explicit (specialised query formats, niche terminology, relationships between resources).
  Prompt-engineer *error responses* so they give specific, actionable fixes instead of opaque codes. Resolve
  cryptic IDs to meaningful names. Refine descriptions iteratively from agent transcripts; small description
  changes produce large gains.
- **Agent Skills.** A skill is a folder with a `SKILL.md` (YAML frontmatter `name` + `description`).
  Level 1: name/description preloaded. Level 2: body loaded when relevant. Level 3+: bundled reference files
  read only as needed, so context is effectively unbounded through progressive disclosure. Recommended
  practice: ask the agent to capture successful approaches and common mistakes into a skill; Anthropic's
  stated goal is agents that create, edit and evaluate their own skills.

### 3.2 Self-improving context (papers)

- **ACE: Agentic Context Engineering** (arXiv 2510.04618). Context is an evolving *playbook* of itemized
  bullets, each with an ID, helpful/harmful counters and content. Roles: Generator (does the task) ->
  Reflector (extracts lessons from successes and errors) -> Curator (emits small *delta* entries that are
  merged by deterministic, non-LLM code). Grow-and-refine: append new bullets, update existing ones in place,
  de-duplicate by embeddings. This avoids *brevity bias* and *context collapse*: in a case study where an LLM
  rewrote the whole context each step, it collapsed from 18,282 to 122 tokens in one step and accuracy fell
  from 66.7 to 57.1, below the 63.7 no-adaptation baseline. Evaluated on AppWorld (an API-calling agent
  benchmark): +10.6%. Caveat: quality depends on the feedback signal; without reliable signals adaptation
  can degrade.
- **DRAFT** (arXiv 2410.08197, ICLR 2025 oral). Tool documentation refined from the agent's own trial and
  error: experience gathering -> learning from experience -> documentation rewriting. Uses diversity-promoting
  exploration and tool-adaptive termination (stop when the docs converge). Refined docs generalise across models.
- **ReasoningBank** (arXiv 2509.25140). Memory items are `{title, one-sentence description, content}`. At most
  3 items are extracted per trajectory, from both self-judged successes (validated strategies) and failures
  (preventive lessons). Retrieval is embedding top-k injected into the system prompt. Beats memories of raw
  trajectories or success-only routines. The LLM judge used to label success/failure was only 72.7% accurate,
  yet the memory still helped.

### 3.3 Memory frameworks

- **LangMem.** Three memory types: *semantic* (facts, stored as a strict-schema "profile" or an open
  "collection"), *episodic* (successful past interactions as few-shot examples) and *procedural* (system
  instructions that evolve from feedback). Memories form in the *hot path* (agent writes during the task) or
  in the *background* ("subconscious" reflection after the conversation). Recall should combine similarity,
  importance and strength (recency/frequency).
- **Vanna 2.0 `AgentMemory`** (verified in `src/vanna/capabilities/agent_memory/base.py`). An "agent memory
  capability interface for tool usage learning":
  `save_tool_usage(question, tool_name, args, success, metadata)`, `save_text_memory(content)`,
  `search_similar_usage(question, similarity_threshold=0.7, tool_name_filter)`, `search_text_memories`,
  `get_recent_memories`, `delete_by_id`, `clear_memories(tool_name, before_date)`. Per its docs it learns
  automatically from successful interactions (by default only for admin users) and saves corrected queries.
  Note: Vanna's training docs page is itself flagged as an LLM-generated placeholder; the source code is
  authoritative.
- **Letta MemFS + dreaming.** Memory is a git-backed tree of markdown files with frontmatter (`name`,
  `description`). Root files are loaded into the system prompt every turn (identity, durable facts, critical
  workflow rules); subdirectories with their own `MEMORY.md` index stay out of context until navigated. Every
  edit is a git commit. Background "dreaming" subagents review recent conversations (after N steps or on
  compaction) and consolidate lessons, optionally with a second-agent review; `/doctor` audits placement,
  duplication and system-prompt token usage.
- **MCP reference memory server.** A knowledge graph of entities (`name`, `entityType`, atomic
  `observations`) and directed relations. What gets remembered is driven entirely by the capture categories
  listed in the system prompt.
- **Graphiti (Zep).** A temporal context graph: every fact has a validity window; when information changes,
  old facts are *invalidated, not deleted*; every entity and relation traces back to its source episodes
  (provenance); updates are incremental.

### 3.4 MCP and opencode mechanics

- **MCP server instructions** (MCP blog, 2025-11-03). A "user manual" injected into the system prompt. Use it
  for cross-tool relationships, operational patterns and constraints. Don't repeat tool descriptions and don't
  write a manual; keep it concise and actionable ("no instructions are better than poorly written
  instructions"). The GitHub MCP server generates its instructions dynamically from the enabled toolsets.
  Instructions cannot guarantee behaviour: critical actions should be deterministic rules or hooks.
- **MCP spec 2026-07-28.** The protocol is now stateless and the `initialize` handshake is retired.
  `instructions` lives in the `server/discover` result alongside `ttlMs`/`cacheScope`, and `tools/list`,
  `prompts/list`, `resources/list` and `resources/read` results are cacheable with TTLs, so dynamically
  regenerated instructions and descriptions are a first-class pattern. To keep state across calls, mint an
  explicit handle and let the model pass it back (which the API session tooling already does).
- **opencode.** Injects MCP server instructions (observed: both the logbook and target-API instructions appear in
  the agent's system prompt). Skills are discovered from `~/.config/opencode/skills/<name>/SKILL.md` (also
  `.opencode/skills/`, `.claude/skills/`, `.agents/skills/`). Rules come from `AGENTS.md` (project, plus
  global `~/.config/opencode/AGENTS.md`); the `instructions` key in `opencode.json` accepts file globs **and
  remote URLs** (fetched with a 5 s timeout).

### 3.5 Convergent patterns

1. Itemized, keyed entries updated by small deltas merged in code; never monolithic LLM rewrites.
2. Every entry = title + one-line description (index tier) + body (on-demand tier).
3. Learn from failures as well as successes; execution outcomes are the feedback signal.
4. Tiered loading: tiny always-on core -> index -> full entries on demand.
5. Deterministic hooks for critical knowledge; instructions are probabilistic.
6. Validity windows, supersede, provenance and revision history to handle drift.
7. An explicit capture policy, with hot-path writes plus background consolidation.

## 4. Diagnosis of the current setup

Evidence from the logbook itself:

- **Open loop.** API knowledge is captured in logs and notes, but nothing feeds it back into the API
  tool context. The only path is a human copying it into the server instructions.
- **Notes accumulate instead of refine.** Free-form notes have no natural key or upsert, so knowledge piles
  up as new entries rather than improving existing ones.
- **Project scoping fragments API knowledge.** API facts live under `default` (note #3, log #176),
  a remote MCP project (note #2) and `l4-hiu-3054` (note #6). API knowledge should be scoped by *domain*
  (the API), not by project.
- **No supersede or validity.** Note #2, "bulk endpoint unsupported in deployed API" (high priority,
  2026-10-05 12:16), was contradicted about an hour later by log #176 (`/iot/bulk/lastSeen/{siteId}`
  works) and is also contradicted by the current gotcha (e). Note #2 is still surfaced in every
  `start_session` prior-context.
- **Same error, different causes.** `422 "Device id format not recognised - <reportType>"` means either a
  wrong timeSeries path order (gotcha c) or a route that was not deployed (note #2). Entries need
  `applies_to` and evidence, not just a message-to-fix mapping.

## 5. Proposed design

### 5.1 Architecture

```
CAPTURE                          STORE (logbook)             CONSUME (API MCP)
agent kb_upsert (hot path) ─┐                              ┌► startup instructions
end_session reflection ─────┼─► kb_entries (+revisions) ───┼► kb_lookup (live)
tool_events (errors) ───────┤   proposed→verified→         └► error hook: hint in
py_exec schema-diff ────────┘   core→superseded                response

The logbook owns the memory, which is cross-domain (the API and future APIs). The API MCP is a consumer: it
reads the KB but does not own it.

### 5.2 Store: `kb_entries` (illustrative sketch)

```sql
CREATE TABLE kb_entries (
    id               bigserial   PRIMARY KEY,
    domain           text        NOT NULL,               -- '<api-name>', e.g. '<api>-api'
    kind             text        NOT NULL,               -- field | endpoint | report_type | id_format | error
                                                         -- | empty_result | gotcha | workflow | recipe | glossary | model
    key              text        NOT NULL,               -- natural key, e.g. 'sensorReport.priOutTemp'
    title            text        NOT NULL,
    summary          text        NOT NULL,               -- one sentence (index tier)
    body             text,                               -- detail (on-demand tier)
    applies_to       text[]      NOT NULL DEFAULT '{}',  -- tool names; '{*}' = all tools
    error_pattern    text,                               -- regex matched against tool errors
    status           text        NOT NULL DEFAULT 'proposed', -- proposed | verified | core | superseded
    helpful          int         NOT NULL DEFAULT 0,
    harmful          int         NOT NULL DEFAULT 0,
    evidence         jsonb       NOT NULL DEFAULT '[]',  -- [{session_id, log_id, tool_event_id, observed, at}]
    superseded_by    bigint      REFERENCES kb_entries(id),
    valid_from       timestamptz NOT NULL DEFAULT now(),
    valid_to         timestamptz,                        -- set when superseded
    last_used_at     timestamptz,
    last_verified_at timestamptz,
    created_by       text,                               -- chat/session id or 'human'
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- one current entry per natural key
CREATE UNIQUE INDEX kb_entries_current_key
    ON kb_entries (domain, kind, key) WHERE valid_to IS NULL;

CREATE TABLE kb_revisions (
    entry_id   bigint      NOT NULL REFERENCES kb_entries(id),
    rev        int         NOT NULL,
    before     jsonb,
    after      jsonb       NOT NULL,
    reason     text,
    changed_by text,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_id, rev)
);
```

Search plugs into the existing `search_index` full-text search. De-duplication uses `pg_trgm` similarity on
`key`/`title`/`summary` (pgvector later if needed).

### 5.3 Tools (logbook MCP)

| Tool | Purpose |
|---|---|
| `kb_upsert(domain, kind, key, title, summary, body?, applies_to?, error_pattern?, evidence?)` | Create or patch by natural key (a delta); writes a revision; returns near-duplicates so the agent can merge instead of duplicating |
| `kb_search(query, domain?, kind?, status?)` | Full-text + trigram search; returns the index tier (title + summary) |
| `kb_get(id or domain/kind/key)` | Full entry with evidence and revision count |
| `kb_feedback(id, verdict, note?)` | `verdict` is helpful or harmful; bumps counters and `last_used_at` |
| `kb_supersede(id, reason, replacement_id?)` | Closes the validity window; history is kept |
| `kb_render(domain, tier)` | `tier` is core or index; renders markdown for instructions or skill export within a token budget |

### 5.4 Write paths (how knowledge gets in)

1. **Hot path.** The agent calls `kb_upsert` while working, following the capture policy (section 6).
2. **End-of-session reflection.** At `end_session`, review the session's logs and errors and propose at most
   3 deltas, drawn from successes *and* failures (ReasoningBank-style).
3. **Deterministic signals.**
   - The API MCP records `tool_events` (tool, argument shape, HTTP status, error text, empty-result flag,
     bytes). Repeated error signatures with no matching KB entry become candidates.
   - **Schema-diff.** A profiling script (run through the API's py_exec tool) lists every field path, type and example
     value in real responses. Diffing that against `kind=field` keys yields an "undocumented fields"
     worklist, so the agent only annotates meaning/unit/scale for new fields (DRAFT-style
     explore -> learn -> rewrite). This is how the data dictionary grows with measurable coverage.

### 5.5 Read paths (how knowledge gets back into context)

- **Tier 0, always on.** At startup the API MCP builds its `instructions` from the static hand-written core
  plus `kb_render('<api>-api', 'core')`, within a budget of roughly 1.5k tokens, falling back to the static
  text if the KB is unreachable. Refreshes when the MCP server is (re)started or reconnected (or via the
  `server/discover` TTL under the 2026-07-28 spec). Candidate to move out of Tier 0: the PlantUML DEVICE
  MODEL (the largest block), which can be served on demand instead.
- **Tier 1/2, on demand and live.** `kb_lookup(query or key)` returns the index (titles + summaries) or
  full entries. No restart needed.
- **Deterministic hook.** When an API tool call fails (or returns empty readings), the server matches the error
  text against `error_pattern` / `applies_to` and appends the matching entry's summary to the tool response.
  If the next call succeeds, it records `helpful`.
- **Optional export.** Render to `~/.config/opencode/skills/<api>-api/` (`SKILL.md` + reference files) for
  clients that don't read MCP instructions.

### 5.6 Governance

- Lifecycle: `proposed` -> `verified` (reproduced by a re-run or approved by a human) -> `core` (promoted
  into Tier 0 by counters or human approval) -> `superseded` (validity closed, never hard-deleted).
- Every change writes a `kb_revisions` row for audit and rollback.
- `kb_render` enforces the Tier-0 token budget.
- A periodic "doctor" pass merges duplicates, splits oversized entries and expires long-unused proposals.

### 5.7 Example entries

Seeded from the existing gotcha (f):

```
domain=<api>-api  kind=error  key="HTTP 500 Multiple devices found for IMEI"
error_pattern="Multiple devices found for IMEI \d{15}"
applies_to={*}
summary="Duplicate device rows exist for this IMEI; the API can't serve it until the data is fixed. Don't retry; report it."
status=core (human-seeded)
```

A data-dictionary entry, from the DEVICE MODEL:

```
domain=<api>-api  kind=field  key="sensorReport.priOutTemp"
applies_to={sensorreport_devid, timeSeries_reportType_devid}
summary="Primary return temperature (NTC). Sensor is NOT fitted on the EcoCtl HIU: open pads read about
         -40..-100 C. Treat as missing, not as a reading."
status=core (human-seeded)
```

Drift handled by supersede (note #2 -> log #176):

```
domain=<api>-api  kind=endpoint  key="GET /iot/bulk/{reportType}/{siteId}"
v1  2026-10-05 12:16  (note #2)   "Not supported on the deployed API"                 -> superseded
v2  2026-10-05 13:28  (log #176)  "Works; cache shares the store; smaller
                                   report-type set; data:null/{} if never reported"   -> current
```

## 6. Capture policy (draft for the logbook server instructions)

```
KNOWLEDGE CAPTURE: call kb_upsert (domain = API name, e.g. <api>-api) when you learn
something a future agent would otherwise rediscover the hard way:
  1. error signature -> cause -> fix (quote the exact error text; set error_pattern)
  2. why a call returned empty/null/404 (namespace, casing, id kind, path order)
  3. field meaning / unit / scale / sentinel values (key = reportType.field)
  4. id formats and which endpoints accept them
  5. a multi-call recipe that answered a real question (tool sequence + arg templates)
  6. device/site-specific quirks (include IMEI/site in the key)
Rules: kb_search first and UPDATE the existing entry rather than adding a near-duplicate.
If an entry proves wrong or stale, kb_supersede it with evidence; never silently delete.
Do not record secrets, one-off values, or anything already stated in a tool description.
Before working with an API domain, read its core entries (kb_render) or look up specifics (kb_search).
```

## 7. Phased plan

| Phase | Scope | Where |
|---|---|---|
| 0: no code | Single domain tag `<api>-api`; add the capture policy to the instructions; update notes instead of adding new ones (title convention `<kind>: <key>`); supersede note #2 | logbook usage |
| 1: store | `kb_entries` + `kb_revisions`; `kb_*` tools; seed from GOTCHAS a-g, the DEVICE MODEL, notes #3/#6 and log #176 | engineerLogBook |
| 2: consume | Startup-compiled instructions from the KB (static fallback); `kb_lookup`; error-hint hook | API MCP |
| 3: automate | `tool_events`; end-session reflection; schema-diff coverage report; de-dup (`pg_trgm`); review page in the logbook UI; optional SKILL.md export | both |

Tracked as logbook tasks #14 (note #2), #15 (Phase 1) and #16 (Phase 2).

## 8. Caveats and risks

- **Self-written memory can be wrong.** ReasoningBank's success judge was 72.7% accurate, and ACE degrades
  with weak feedback. HTTP outcomes are a good signal for *mechanics* (paths, IDs, errors) but not for
  *semantics* (units, meanings), so new entries stay `proposed` until verified; only `verified`/`core`
  entries reach Tier 0.
- **Instructions are probabilistic.** Anything that must happen belongs in the error hook or tool code, not
  only in instructions.
- **Context budget.** Tier 0 must stay small; everything else is on demand.
- **Memory poisoning and secrets.** Treat API payload text as data, never as instructions to store; never
  store credentials or tokens.
- **Ambiguous signatures.** One error can have several causes; keep `applies_to` and evidence and let the
  helpful/harmful counters arbitrate.
- **Refresh latency.** Tier 0 only refreshes when the MCP server (re)connects; live knowledge arrives through
  `kb_lookup` and the hook.

## 9. Alternatives considered

- **Agent-edited SKILL.md tree** (`~/.config/opencode/skills/<api>-api/`). Zero code, progressive disclosure,
  git-friendly; but monolithic file rewrites (collapse, duplicates) and no counters, status or hooks. A good
  Phase-0 experiment or export target.
- **Off-the-shelf memory** (MCP memory server, Graphiti/Zep, Mem0, Letta). Extra infrastructure, generic,
  and not wired into the API's errors or instructions.
- **Keep hand-editing static instructions.** The current pain point.
- **KB inside the API MCP.** Couples memory to one API, whereas the logbook is cross-domain.

## 10. Open questions

1. Should the API MCP read the logbook Postgres directly, or go through an HTTP endpoint or an exported file?
2. Promotion into Tier 0: automatic by counters, or human approval in the logbook UI?
3. Where is the readonly API MCP source? Its tools are generated from a local OpenAPI spec (per log #179).

## 11. Sources

- Anthropic, *Effective context engineering for AI agents*:
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Claude Platform docs, *Memory tool*:
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Anthropic, *Writing effective tools for AI agents, using AI agents*:
  https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, *Equipping agents for the real world with Agent Skills*:
  https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Zhang et al., *Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models*:
  https://arxiv.org/abs/2510.04618
- *From Exploration to Mastery: Enabling LLMs to Master Tools via Self-Driven Interactions* (DRAFT):
  https://arxiv.org/abs/2410.08197
- *ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory*:
  https://arxiv.org/abs/2509.25140
- MCP blog, *Server Instructions: Giving LLMs a user manual for your server*:
  https://blog.modelcontextprotocol.io/posts/2025-11-03-using-server-instructions/
- MCP blog, *The 2026-07-28 Specification*:
  https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP specification 2026-07-28, *Discovery*:
  https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- opencode docs, *Agent Skills*: https://opencode.ai/docs/skills/
- opencode docs, *Rules*: https://opencode.ai/docs/rules/
- LangMem, *Core concepts*: https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- Letta docs, *Memory & dreaming*: https://docs.letta.com/configuration/memory
- Letta docs, *MemFS*: https://docs.letta.com/concepts/memfs
- Vanna, `AgentMemory` interface:
  https://github.com/vanna-ai/vanna/blob/main/src/vanna/capabilities/agent_memory/base.py
- Vanna docs, *Training* (flagged by Vanna as an LLM-generated placeholder):
  https://vanna.ai/docs/placeholder/training
- MCP reference memory server: https://github.com/modelcontextprotocol/servers/tree/main/src/memory
- Graphiti: https://github.com/getzep/graphiti

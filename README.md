# Progress Control Center

A plan dashboard that **derives** progress from the markdown checkboxes you already
write, and turns each phase into something you can act on — run its tests, open a
coding session scoped to one checklist item, draft its ticket.

Two Python files. No pip install, no framework, no database.

```bash
python progress-serve.py --repo /path/to/your/project
```

An unconfigured project opens the setup wizard; a configured one opens the dashboard.

![The plan view: derived progress, computed schedule, phases with their status](docs/img/overview.png)

> **See it in 30 seconds.** This repo is configured with its own
> [ROADMAP.md](ROADMAP.md) as its plan, so cloning it and running
> `python progress-serve.py --repo .` gives you the dashboard above — the tool
> tracking its own roadmap. Every screenshot on this page is that.

---

## What it solves

**Plans go stale, and status gets retyped.** A plan is written once, drifts within a
week, and the truth about progress moves into standups, spreadsheets and someone's
head. Every status update is a human re-reading the repo and typing what they found.

This inverts that. The markdown checkboxes you already write **are** the status —
there is nothing to update — and the plan itself is editable from the dashboard by a
coding session that can re-assess it against the repo as it stands today. The plan
stops being a document you maintain and becomes one that maintains itself.

**And the gap between "I know what to do" and "the agent knows what to do."** Opening
a coding session on a piece of work means re-explaining the phase, its exit test, its
open items, and which knowledge sources to consult. Here that prompt is already
built, from the plan, per phase or per checklist item — and the phase is explained
once: a session that already holds it gets only the next item.

## Where it saves time

| Instead of | You |
|---|---|
| writing a status report | open the page — progress is derived, nothing to update |
| working out what can start now | read *Ready* — dependencies are resolved for you |
| checking three projects to find what needs you | open **Today**: briefs to confirm, plan changes, tasks marked for you and working sessions, across every project |
| guessing the finish date | read the projected finish, computed from your **measured** pace — items per active day and active days per week — not from typed days |
| pasting context into an AI session | click **Open session** — the prompt is built from the phase |
| re-explaining the phase to a session that already knows it | click **Send to phase session** — only the item goes, into the same conversation |
| leaving the terminal to fetch the next item | type `/next-item 0` — the session pulls it from the live checklist |
| an agent running off in the wrong direction | the session posts a plain brief — decisions numbered, outcomes each with a check — and **waits for your confirmation** before touching anything |
| writing a ticket from scratch | click **Draft ticket** on the plan, review, create — key written back |
| rewriting a plan that drifted | click **Re-plan…**, add steering, let a session edit it |
| a session quietly changing later items to fit what it found | it **proposes** the change instead; you see the exact lines and press **Apply change** to make it |
| chasing "is your checkout the same as mine?" | teammates get a launch command for *their* machine |
| a standup document | `--standup` writes it from the snapshot diff: markdown for the repo, and a self-contained HTML report to open, download or attach to the plan's ticket |

## What it integrates

| | |
|---|---|
| **Plan agents** | one named agent per plan, defined in Setup (with suggested sources) or in the config, generated for Claude Code and opencode from a source list you declare once: files, folders, URLs, MCP servers, prior plans |
| **Coding agents** | Claude Code, Codex, opencode, Cursor, VS Code — detected on PATH; one phase session per tool, resumed by id (Claude Code, opencode) or as the last session (Codex) |
| **Issue tracking** | JIRA Cloud and Server/DC — one ticket per plan: draft, review, create over the API, key recorded on the plan |
| **Knowledge** | any MCP provider (stateless or stateful HTTP) as a `[[context]]`, its usage rules injected into every session prompt |
| **Your repo** | git activity per phase, checkbox write-back, `--check` contract lint |
| **Your services** | TCP reachability probes across one or more hosts, adopted as context providers |
| **Nothing else** | Python ≥ 3.11 stdlib. No pip install, no daemon, no account, no telemetry |

## The one rule

**Progress lives only in your plan's checkboxes.** `- [ ]`, `- [x]`, `- [~]`.

There is no status field, no percentage you maintain, and no second store — not a
database, not a ticket system. Tick a box in the markdown and the dashboard moves.
Tick a box *in* the dashboard and it rewrites that line in the markdown.

**A plan written without checkboxes works as it is.** If a plan has phase headings
but no boxes — numbered steps, bullets, or a table under each phase — its top-level
entries under each phase heading are the items (`items = "lists"`, detected and
recorded for you). An entry with no mark is open; ticking it writes the mark into
that line: `3. Define the schema` becomes `3. [x] Define the schema`, and a table
row gets it at the start of its first cell. Nothing is converted up front, so a
plan your team shares is never rewritten just to be tracked.

Everything else — dependencies, effort estimates, lead times — lives in one
`docs/progress.toml`, because markdown cannot express them.

The consequence worth stating: the plan and the report can never disagree, because
there is only one of them.

## What you get

**A phase list.** Each phase expands in place to its checklist, its exit test (the
outcomes that must be true when it ends), what it unlocks, the branch the checkout is
on, and the git activity under its modules. Filter to what's *ready* (every
dependency met), what's *blocked*, or what's *done*.

**A schedule you did not write.** From `depends_on` and `days` it computes the
critical path, what can run in parallel, when each phase can start. Phases whose real
technical dependency differs from their order in the plan are exactly where the
parallelism shows up.

**An estimate that matches how the work is done.** When a model does the
implementing, a checklist item costs one brief-and-confirm cycle of your attention,
so effort is *sessions left*, and the calendar is set by how often you sit down and
what you wait on. The dashboard measures both from the snapshot history — items
ticked per active day, active days per week — and projects the finish from them,
labelled *measured* or *assumed*, as a range when the recent pace differs from the
all-time pace, and naming what limits the date: attention, an external wait, or the
work itself. Typed `days` remain the timeline's floor for genuinely time-bound work.

**Actions on the phase, not beside it.** Run that phase's exit test and watch the
output stream in. Open a coding session with a prompt already scoped to the phase, or
to one checklist item. Every session opens **brief-first**, with a brief written for
someone who is not a specialist: what the item is and why the plan needs it, what it
found that changes the approach, the decisions it needs from you (numbered, each with
options and a recommendation, so you can answer "1A, 2 default"), the steps it will take,
how you will know it is done (each outcome with a named check), and what it will leave
alone. Then it asks *"confirm these steps, or redirect me?"* and waits — no code, no
file edits, until you confirm. Above the phases, the plan has one ticket of its own: a
session drafts it from the whole plan, you review it, and create it or link an existing
key.

**One phase session, told the phase once.** Every prompt has two shapes. The *cold*
one carries the phase — doc, exit test, modules, open items, the protocol — and goes
out once, on *Start phase session*; the session acknowledges and waits for the first
item. Each item after that is a *warm* prompt of about 560 characters, sent into the
same conversation by *Send to phase session*, which resumes that phase's session by
id rather than whichever conversation was most recent in the directory. If the
session's terminal tab is still open, the text goes to the clipboard and the tab is
focused instead of a second process being started on the same transcript. On the
project this was measured on, a 19-item phase used to send 29,902 characters of
prompt, 27,400 of them repeats; now it sends one brief and then the items. A strip on
the phase shows what the page can honestly tell you about that session — tool and
id, when it started, the last item sent, whether the terminal is live and the
transcript still exists, and exactly what *Send* will do next — plus an amber line
when the item last sent is still open in the plan, which is the one thing the page
can verify. None of that is progress; the checkboxes stay the only store. The
session can also pull for itself: `/next-item 0` inside Claude Code or opencode
reads the next open item from the live checklist, with no dashboard click.

**A plan that stays current.** *Re-plan…* on any item, phase, or the whole plan hands
the rethink to a coding session with your steering attached and your context
providers consulted. It proposes its changes first and waits for your go-ahead, then
edits the plan and the config under rules that keep history intact: valid items keep
their state, a done item the new direction invalidates is flagged *needs redo* with
the redo added as new work, and headings stay machine-readable. Re-planning one item
or phase may add items — or a whole phase — when the rethink needs them.

**Plan changes found while working.** When an item's work shows that a later item is
now wrong, the working session does not edit the plan. It appends a one-line proposal
(reword, add, drop, redo, or a free-form note) to a per-plan file in `.pcc/`, and the
plan row shows *Plan changes: N proposed*. Each proposal previews the exact lines it
would change; **Apply change** then **Confirm** writes them, logs a dated line under
*Plan changes along the way* in the plan, and can be undone. Notes and proposals whose
target has moved go to **Re-plan with this** instead, and **Dismiss** drops one
without touching the plan. Saving the config reconciles `[[phase]]`
blocks with the plan's headings, so adding a phase to the markdown is enough. And the
page reloads itself when the plan changes on disk, so a `git pull` from a teammate
lands on your screen instead of going unnoticed.

**Several projects at once.** Every dashboard carries a project bar: *Today*, then
one tab per project with its mark, its colour and how many things wait on you there.
*Today* is one list across all of them, most pressing first: a session whose brief
waits for your confirmation (read from its transcript, wherever it runs), a session
that replied and waits for you, a proposed plan change, a task the plan marks
`[You]` — with *Mark done* right there — then the sessions still working, and what is
coming: critical blockers and the next phase. Each project still runs its own
dashboard on its own port; they find each other through the projects list, and a
project whose dashboard is off gets a *Start dashboard* button that opens it in a new
window. Terminal tabs carry the project mark, so two "Phase 1" sessions stay apart.

**A risk register** derived from the schedule: what is on the critical path, what
external blockers will stall which phase, and how much slack is left.

### A phase, expanded

Every action sits on the phase itself — run its exit test, open a session scoped to
it, re-plan it, draft its ticket. The checklist is the plan's own checkboxes; ticking
one here rewrites that line in the markdown.

![A phase expanded: its action row, checklist, exit test and what it unlocks](docs/img/phase-expanded.png)

### The schedule you did not write

`depends_on` and `days` are all you supply. The critical path, the parallel groups,
each phase's earliest start and the projected finish are computed.

![Timeline: scheduled windows per phase, with parallel groups](docs/img/timeline.png)

### Risks, derived rather than maintained

![Risk register: what is waiting on what, and the external blockers](docs/img/risks.png)

### Setup that shows its reasoning

Every autodiscovered value is shown *with the evidence for it* and can be changed or
switched off. Nothing reaches the committed config until you save, and the diff of
what landed is shown afterwards.

![The project tab of the setup wizard](docs/img/setup-project.png)

## Two surfaces, different powers

The same template renders twice, and they are **not** interchangeable:

| | runs commands | shareable |
|---|---|---|
| `progress-serve.py` on `127.0.0.1` | yes | no |
| the generated `.html` file | no | yes |

A published copy is static: it cannot reach localhost, so it must never show a Run
button that only pretends to work. It says `snapshot · read-only` in its header; the
live one says `live · actions enabled`. Where the live page launches a session, the
static one hands you the exact shell command instead — honest about what it can do.
Each open item carries its own prompt there too, and the command for a *continue*
tool carries the warm shape.

## Install

```bash
git clone https://github.com/<you>/progress-control-center
python progress-control-center/progress-serve.py --repo /path/to/project
```

Stdlib-only, Python ≥ 3.11 (for `tomllib`). Nothing to install, no supply chain —
which is also why it runs on a locked-down work machine.

Copy the two files rather than adding this repo as a submodule: a submodule pins
this URL into your project's source control, and a subtree imports its history.

## Adopting a project

Point it at a repo and configure in the browser:

```bash
python progress-serve.py --repo /path/to/project
```

With no `docs/progress.toml` it opens on **`/setup`**, whose two tabs are
*This machine* (your name, tool, shell — written to a profile outside every
repo) and *This project* (name, plan file, owner, integrations, tokens, and your
checkout of this project). Not all of that tab is committed: the config fields go
to `docs/progress.toml`, tokens to a gitignored env file beside it, and the
checkout to your profile, keyed by this repo — it is per project, but it is
yours, so it never enters git. Every autodiscovered value shows the evidence behind it and
can be changed or switched off. Nothing reaches the committed config until you
preview the diff.

JIRA asks for two things: the site URL and the project key. The browse URL,
create URL, API base, API version and auth mode are derived from them and shown
as they are derived; Advanced holds the overrides for an instance that differs.
The block ends with whether creating an issue can actually work, and names what
is missing if it cannot — otherwise an absent account email surfaces only as a
401, at the moment you try to raise a ticket.

Or from a terminal, for scripted installs:

```bash
python progress-report.py --init  --repo /path/to/project --name "My Project"
python progress-report.py --setup --repo /path/to/project    # your own profile
python progress-report.py --check --repo /path/to/project    # lint the contract
python progress-report.py        --repo /path/to/project -o report.html
```

`--check` exists for one specific silent failure: a phase whose heading the parser
cannot match resolves zero items and reads **0% forever**, which looks like idleness
rather than misconfiguration.

## The contract

Two things:

1. **A plan in markdown** with `### Phase <id> — <name>` or `### Phase <id>: <name>`
   headings at level 2 to 4 (or per-phase docs), whose checkboxes, or list entries
   in `items = "lists"` mode, are the only store of progress. Opening the dashboard
   adds a `[[phase]]` block for any heading the config does not declare yet.

   **More than one plan.** Pointing the project at another plan file starts a new
   plan without losing the old one: the old plan's `[[phase]]` blocks are tagged
   `plan = "<its file>"` and kept intact, and fresh blocks are generated for the new
   one, even where phase ids overlap. A select in the dashboard's bar switches
   between a project's plans; each keeps its own days, dependencies, tickets and
   items mode, and its progress stays in its own file.
2. **`docs/progress.toml`** holding what markdown cannot express.

```toml
[project]
name       = "My Project"
plan       = "PLAN.md"
start_date = "2026-01-06"
allow_artifact_publish = false      # recorded sharing policy — see docs/DESIGN.md

[[phase]]
id         = "1"
name       = "Ingest pipeline"
days       = 3                      # working days of focused effort, not calendar
depends_on = []                     # the REAL technical dependency, not plan order
doc        = "docs/PHASE-1.md"
exit_test  = ["curl /health -> 200", "ingest backlog drained"]   # outcomes; else read from the plan
modules    = ["services/ingest"]    # paths; the phase shows git activity under them
test       = "smoke"                # id of an [[action]] — never a command itself
owner      = "alice"

[plans."PLAN.md"]                   # per-plan settings: one ticket for the whole plan
jira = "PROJ-101"

[plans."PLAN.md".agent]             # the plan's own agent, generated per tool
name        = "plan-ingest"
description = "Owns the ingest plan. Use for anything touching services/ingest."
[plans."PLAN.md".agent.sources]     # its knowledge base - pointers, never copies
files = ["docs/decisions/00[1-4]-*.md"]
dirs  = ["services/ingest/"]
urls  = ["https://docs.example.com/ingest"]
mcp   = ["project-docs"]
plans = ["OLD-PLAN.md"]

[[action]]                          # the Run buttons
id = "smoke"; label = "Smoke tests"; kind = "argv"; args = ["npm", "test"]

[[blocker]]                         # real-world latency no code removes
id = "vendor-key"; name = "Vendor API key"; owner = "you"; lead_days = 5
```

Everything except `[project]` and `[[phase]]` is optional and degrades to nothing.
See [`docs/DESIGN.md`](docs/DESIGN.md) for the full schema and the reasoning.

## Security

This runs commands on your machine, so:

- it binds `127.0.0.1` only — never `0.0.0.0`. To reach it from elsewhere, tunnel:
  `ssh -N -L 8765:127.0.0.1:8765 user@host`
- every mutating request carries a per-run token, and the `Host` header must be
  loopback (which is what stops DNS rebinding)
- commands come from an allowlist. There is no passthrough: the browser sends a
  **key**, never an argv
- **`[[action]]` and `[[launcher]]` argvs are hashed and approved once**, at a
  console, with the store kept outside every repo. Cloning a repo does not grant it
  command execution on your machine, and switching projects in the UI never can —
  an unapproved project is served read-only with its commands named but stripped
- credentials are environment-variable **names** in config; values live in
  gitignored files and reach a launched session by file path, never on a command
  line and never through the page
- plan text is repo-authored and is escaped before it reaches an inline `<script>`,
  because that block also carries the API token

## Accessibility

Checked against WCAG 2.1 AA, measured on the rendered page rather than by eye: no
contrast failures, no interactive target under 24px, no `role="button"` on a
non-button, labelled controls, real landmarks, and a native `<details>` for every
disclosure so its keyboard behaviour and announced state come from the platform.

Automated checks catch perhaps a third of real issues. This has not been tested with
a screen reader.

## Where it is going

[ROADMAP.md](ROADMAP.md) — seven phases, ordered cheapest-truth-first, with the
non-goals written down. It is also this repo's plan file, so the roadmap and the
dashboard cannot disagree.

## Licence

MIT. See [LICENSE](LICENSE).

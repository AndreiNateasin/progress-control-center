# Progress Control Center

A plan dashboard that **derives** progress from the markdown checkboxes you already
write, and turns each phase into something you can act on — run its tests, open a
coding session scoped to one checklist item, draft its ticket.

Two Python files. No pip install, no framework, no database.

```bash
python progress-serve.py --repo /path/to/your/project
```

An unconfigured project opens the setup wizard; a configured one opens the dashboard.

---

## The one rule

**Progress lives only in your plan's checkboxes.** `- [ ]`, `- [x]`, `- [~]`.

There is no status field, no percentage you maintain, and no second store — not a
database, not a ticket system. Tick a box in the markdown and the dashboard moves.
Tick a box *in* the dashboard and it rewrites that line in the markdown.

Everything else — dependencies, effort estimates, lead times — lives in one
`docs/progress.toml`, because markdown cannot express them.

The consequence worth stating: the plan and the report can never disagree, because
there is only one of them.

## What you get

**A phase list.** Each phase expands in place to its checklist, exit test, what it
unlocks, and the git activity under its modules. Filter to what's *ready* (every
dependency met), what's *blocked*, or what's *done*.

**A schedule you did not write.** From `depends_on` and `days` it computes the
critical path, what can run in parallel, when each phase can start, and a projected
finish. Phases whose real technical dependency differs from their order in the plan
are exactly where the parallelism shows up.

**Actions on the phase, not beside it.** Run that phase's exit test and watch the
output stream in. Open a coding session — new or continuing an existing one — with
a prompt already scoped to the phase, or to one checklist item. Ask a session to
draft a JIRA ticket, review it, and create it.

**A risk register** derived from the schedule: what is on the critical path, what
external blockers will stall which phase, and how much slack is left.

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
*This machine* (your name, tool, shell, checkout path, tokens — written outside
every repo) and *This project* (name, plan file, owner, integrations — written to
the committed config). Every autodiscovered value shows the evidence behind it and
can be changed or switched off. Nothing reaches the committed config until you
preview the diff.

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

1. **A plan in markdown** with `### Phase <id> — <name>` headings (or per-phase
   docs), whose checkboxes are the only store of progress.
2. **`docs/progress.toml`** holding what markdown cannot express.

```toml
[project]
name       = "My Project"
plan       = "PLAN.md"
start_date = "2026-01-06"
allow_artifact_publish = false      # publishing is opt-in, always explicit

[[phase]]
id         = "1"
name       = "Ingest pipeline"
days       = 3                      # working days of focused effort, not calendar
depends_on = []                     # the REAL technical dependency, not plan order
doc        = "docs/PHASE-1.md"
exit_test  = "curl /health -> 200"
modules    = ["services/ingest"]    # paths; the phase shows git activity under them
test       = "smoke"                # id of an [[action]] — never a command itself
owner      = "alice"
jira       = "PROJ-101"

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

## Licence

MIT. See [LICENSE](LICENSE).

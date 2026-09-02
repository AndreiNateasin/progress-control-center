# Roadmap

This file is a plan in this tool's own format, so it can be opened with the tool:

```bash
python progress-serve.py --repo . --port 8765
```

Point the plan file at `ROADMAP.md` in the setup wizard and the phases below become
the dashboard — dependencies, critical path and all. A roadmap for a plan tracker
that the plan tracker cannot read would not be much of an argument for it.

**Ordering principle.** Cheapest-truth-first: work that makes the tool *more honest*
about what it already knows outranks work that adds a new surface. Two of the phases
below are mostly rendering data that is already being collected and thrown away.

---

### Phase 1 — Show the history already being collected

Every render writes a dated snapshot to `docs/progress-history/*.json` with
`overall`, `remaining_days`, `finish_date` and per-phase state. Nothing reads them
back. A project with weeks of history displays a single number and no trend, which
is the one question every stakeholder actually asks: *is this getting better?*

The data exists. This is rendering, not plumbing.

- [ ] Burn-up on the Timeline tab: completed items over time against total scope,
  drawn from the existing snapshots — inline SVG, no chart library, both themes.
- [ ] Scope-change line alongside it: total item count over time, so scope growth is
  visible instead of silently eating the burn-up's slope.
- [ ] Projected-finish drift: the finish date each snapshot predicted, plotted
  against the date it was predicted on. A finish date that moves out one day per day
  is the earliest honest signal a plan is not converging.
- [ ] Per-phase sparkline in the phase row, so a stalled phase looks stalled.
- [ ] Prune policy: snapshots are cheap but not free — keep daily for 90 days,
  weekly beyond, documented where the files live.

**Exit test:** a repo with ≥ 3 snapshots renders a burn-up whose final point equals
the dashboard's current percentage; a repo with one snapshot renders no chart and
says why.

### Phase 2 — A plan that shows its own history

`Re-plan…` edits the plan and leaves a `> re-planned <date>: reason` note, and the
phase-sync leaves dated banners for retired blocks. Both are invisible on the
dashboard — the change happened, and the page shows only the result.

- [ ] Surface `> re-planned` notes as a phase history strip: when, and the one-line
  reason, newest first.
- [ ] Plan-change ledger derived from git: which checkboxes flipped in which commit,
  by whom — no new store, `git log -p` over the plan file is the source.
- [ ] "Changed since you last looked": the freshness poll already knows the plan
  moved; say *what* moved rather than just reloading.
- [ ] Show retired `[[phase]]` blocks as a collapsed "previously planned" section
  instead of leaving them only in the config as comments.

**Exit test:** re-plan a phase, reload, and the dashboard shows the reason and the
date without opening the markdown.

### Phase 3 — Exit tests where they actually run

`test = "<action id>"` runs a command on the machine serving the dashboard. That is
right for a laptop and wrong for the truth: exit tests belong to CI, and a phase's
real status is whether its test passed *there*.

- [ ] Read CI status per phase from the forge API (GitHub Actions first), keyed by
  the phase's `modules` paths — no new credentials beyond a read token.
- [ ] A phase whose exit test is red cannot show as done, however its boxes are
  ticked — the one case where derived progress needs a second opinion.
- [ ] Link the run, do not summarise it: the dashboard is not a CI UI.
- [ ] Degrade honestly with no forge configured — the local runner stays exactly as
  it is today.

**Exit test:** a phase with a failing CI run shows red on a dashboard whose
checkboxes are all ticked, with a link to the run; unconfigure the forge and the
phase returns to derived-only.

### Phase 4 — More than one project at a time

The picker switches projects one at a time and the dashboard serves one repo. Anyone
running three projects runs three servers or clicks through three configs, and there
is no view of the portfolio.

- [ ] Overview across every registered project: percentage, next phase, projected
  finish, blocked count — one row each, from each project's own config.
- [ ] Cross-project risk roll-up: which external blockers stall more than one
  project, which projects share a critical-path dependency.
- [ ] Serve several repos from one process rather than rebinding `REPO`, so switching
  stops being a global mutation of server state.
- [ ] "My work across projects": filter every project's phases by the roster name
  already used by *only my phases*.

**Exit test:** two configured projects render in one overview with correct
percentages, and switching between them does not require a restart.

### Phase 5 — The shared-server story, finished

The per-developer handout works: pick your name and the launch command is built for
your tool, shell and checkout. What is not finished is everything around it on a
dashboard several people actually open.

- [ ] Verify `--host` on a non-loopback bind end to end: the profile is withheld
  (already gated), the handout still works, and the read-only surface is genuinely
  read-only.
- [ ] Per-viewer identity without accounts: the developer bar's selection is
  per-browser today; make it survive a project switch and name the viewer in the
  activity strip.
- [ ] Document the deployment properly — reverse proxy, TLS, what must never be
  exposed — or state plainly that loopback plus SSH tunnel is the only supported
  shape.
- [ ] Decide the write story on a shared dashboard: today anyone who can reach it can
  tick a box. Either that is fine and it is written down, or ticks need identity.

**Exit test:** three developers on three machines open one served dashboard, each
gets a launch command that runs on their own box, and the answer to "who ticked
this?" is written down.

### Phase 6 — Operational honesty

Small, independent items that each remove a way the dashboard can mislead.

- [ ] Context-provider health strip: the probes already run; show which providers are
  reachable right now, because a session launched against an unreachable provider
  fails in a way that looks like the model being unhelpful.
- [ ] Risk aging: a blocker that has been open for three weeks is a different risk
  from one raised today, and the register currently treats them identically.
- [ ] Trust-gate diff: when a repo's `[[action]]` argvs change, show what changed
  rather than re-prompting for the whole set.
- [ ] Verify the Codex `resume --last` flags against a machine that actually has
  Codex installed — they came from documentation, not from `--help`.

**Exit test:** an unreachable context provider is visible on the dashboard before a
session is launched against it.

---

## Non-goals

Stated so they stop being asked:

- **A database.** Progress lives in checkboxes; anything else is a cache that can
  disagree with the plan. This is the one rule and it does not bend.
- **A hosted service.** No account, no telemetry, no upload. The tool runs on the
  machine that has the repo.
- **A model client.** Ticket drafting and re-planning are done by *your* coding
  session, which already has the repo, your model configuration and your credentials.
  Adding a second model configuration here would be a second thing to keep in sync.
- **A CI UI.** Phase 3 reads status and links out. Building a log viewer is someone
  else's project.
- **A ticket system.** JIRA integration creates and links. It does not sync, mirror,
  or become a second place where work is tracked.
- **Dependencies.** Two files, Python stdlib. The reason this runs on a locked-down
  work laptop is that there is nothing to install and no supply chain to review.

## Contributing to the order

The sequencing above is opinionated, not fixed. Phases 1 and 2 render data the tool
already has; 3 and 4 add real surface; 5 and 6 are the honesty debt. If you want a
different order, the plan is a markdown file — change it, and the dashboard will
agree with you.

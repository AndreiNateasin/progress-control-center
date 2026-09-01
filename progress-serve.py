#!/usr/bin/env python3
"""Progress Control Center — the report as a LOCAL, actionable dashboard.

    python scripts/progress-serve.py            # http://127.0.0.1:8765

Same generator as scripts/progress-report.py: this imports build() and render()
and serves their output unmodified, then injects an action layer on top. The
published Artifact and this page are therefore the same report — one template
rendered twice — and the artifact's HTML is byte-identical to what it was
before this file existed.

WHY A SECOND SURFACE AT ALL
A published Artifact is a sandboxed page on claude.ai behind a strict CSP. It
cannot reach localhost, run a script, or start a session — so a "Run tests"
button there would be a lie. This runs on the machine that HAS docker, the
scripts and the `claude` CLI, so the buttons are real. Split of duties:

    local (this)   run tests, tick boxes, open a session   not shareable
    artifact       read-only, phone, shareable link        no actions

TRUTH STAYS IN THE PLAN
Ticking a box here rewrites the `- [ ]` in PLAN.md / docs/PHASE-*.md. The
dashboard is an EDITOR for the plan, never a second store of progress — so the
"derive, never duplicate" rule survives. Write-back matches the verbatim source
line, not a line number: if the file changed since the page was rendered, the
match fails and you are told to refresh, rather than the wrong box being ticked.

SECURITY
This endpoint executes commands, so:
  - it binds 127.0.0.1 only (never 0.0.0.0 — with WSL mirrored networking that
    would publish command execution to the LAN and every VPN tunnel);
  - every mutating request needs the per-run token injected into the page;
  - the Host header must be loopback, which is what blocks DNS rebinding;
  - commands come from the ACTIONS allowlist. There is no passthrough.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
_pr = import_module("progress-report")          # hyphenated module name
build, render = _pr.build, _pr.render

import tomllib

SELF_DIR = Path(__file__).resolve().parent      # where THIS install lives
REPO = _pr.REPO                                 # re-pointed by init_repo()
BIND_HOST = "127.0.0.1"                         # set by main() before init_repo
DISTRO = os.environ.get("PCC_DISTRO", "Ubuntu-24.04")
PROMPT_DIR = REPO / ".pcc"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def wsl(path: Path) -> str:
    r"""C:\src\proj -> /mnt/c/src/proj"""
    p = path.resolve().as_posix()
    if len(p) > 1 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


def _py(*args: str) -> list[str]:
    """Run the generator FROM THIS INSTALL against the CURRENT repo — the two
    are different directories once one installed copy serves many projects."""
    return [sys.executable, str(SELF_DIR / "progress-report.py"), "--repo", str(REPO), *args]


def _expand(s: str, repo: Path | None = None) -> str:
    """The ONLY placeholder expansion config strings get. Deliberately no
    user-input interpolation and no general templating — {repo}/{repo_wsl}/
    {distro} are server-side constants, so a hostile progress.toml can name
    commands (which the repo owner controls anyway) but a browser never can.

    `repo` overrides the served project, so ANOTHER project's argv set can be
    expanded — and therefore hashed — without pointing this server at it."""
    r = REPO if repo is None else repo
    return (s.replace("{repo}", str(r))
             .replace("{repo_wsl}", wsl(r))
             .replace("{distro}", DISTRO))


_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def build_actions(cfg: dict, repo: Path | None = None) -> dict:
    """The run-button allowlist. Two portable built-ins always exist; everything
    project-specific comes from [[action]] tables in that repo's progress.toml
    (a reference project's doctor/smoke/etc. live there, proving the
    mechanism). The allowlist is fixed at startup; the browser sends only keys.
    """
    r = REPO if repo is None else repo
    acts: dict[str, dict] = {
        "regen":   {"label": "Regenerate", "primary": False,
                    "hint": "rebuild docs/progress-report.html from the plan",
                    "argv": _py()},
        "standup": {"label": "Standup", "primary": False,
                    "hint": "write today's docs/standups/<date>.md from the snapshot diff",
                    "argv": _py("--standup")},
    }
    for a in cfg.get("action", []):
        aid, kind = str(a.get("id", "")), a.get("kind", "argv")
        if not _ID.match(aid):
            print(f"  config: skipping action with bad id {aid!r}", file=sys.stderr)
            continue
        args = [str(x) for x in a.get("args", [])]
        if kind == "wsl-bash":
            # args[0] = script path relative to the repo, run inside the distro.
            argv = ["wsl", "-d", DISTRO, "--", "bash", wsl(r) + "/" + args[0], *args[1:]]
        elif kind == "python-self":
            argv = _py(*args)
        elif kind == "argv":
            argv = [_expand(x, r) for x in args]
        else:
            print(f"  config: skipping action {aid!r} with unknown kind {kind!r}", file=sys.stderr)
            continue
        acts[aid] = {"label": a.get("label", aid), "hint": a.get("hint", ""),
                     "primary": bool(a.get("primary", False)), "argv": argv}
    return acts


# Populated by init_repo(); module-level so every handler sees the same dicts.
ACTIONS: dict[str, dict] = {}
_PROBE_CACHE: dict[str, tuple[float, dict]] = {}
_PROBE_TTL = 30.0


def probe_provider(c: dict) -> dict:
    """Is this context provider reachable from here, right now?

    Credential-free by design: a plain TCP connect, nothing sent, nothing read.
    The only question is "could a session launched now reach this" — which is
    what replaced the [[service]] start/stop machinery. WHO brings a tunnel up
    (a terminal, an OS service, a teammate) is not our business; whether the
    port answers is the fact that matters, and it stays true no matter who did it.
    """
    name = str(c.get("name", "?"))
    url = str(c.get("url", ""))
    now = time.time()
    hit = _PROBE_CACHE.get(name)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]

    import socket
    from urllib.parse import urlparse as _up
    res = {"name": name, "label": c.get("label", name), "url": url, "state": "unknown", "hint": ""}
    if not url:
        # A prompt-only provider legitimately has no url. Defaulting to
        # 127.0.0.1:80 probed something unrelated and reported this provider
        # "reachable" whenever anything happened to be serving on port 80.
        res["hint"] = "no url to probe (prompt-only provider)"
        _PROBE_CACHE[name] = (now, res)
        return res
    try:
        u = _up(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1.5):
            pass
        res["state"] = "reachable"
        res["hint"] = f"{host}:{port} answering"
    except OSError as exc:
        res["state"] = "unreachable"
        res["hint"] = f"{type(exc).__name__} — tunnel/VPN down?"
    except ValueError:
        res["hint"] = "unparseable url"
    _PROBE_CACHE[name] = (now, res)
    return res


def probe_status() -> list[dict]:
    """Only providers that opted in with probe = true."""
    return [probe_provider(c) for c in CFG.get("context", []) if c.get("probe")]


MARK = {"done": "x", "active": "~", "todo": " "}

RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()


def start_run(task: str) -> str:
    spec = ACTIONS[task]
    rid = secrets.token_hex(6)
    with RUNS_LOCK:
        RUNS[rid] = {"task": task, "lines": [], "done": False, "rc": None, "started": time.time()}

    def worker() -> None:
        rc = -1
        try:
            proc = subprocess.Popen(
                spec["argv"], cwd=str(REPO),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=NO_WINDOW,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                with RUNS_LOCK:
                    RUNS[rid]["lines"].append(line.rstrip("\n"))
            rc = proc.wait()
        except Exception as exc:                       # noqa: BLE001 — surfaced to the page
            with RUNS_LOCK:
                RUNS[rid]["lines"].append("!! " + type(exc).__name__ + ": " + str(exc))
        with RUNS_LOCK:
            RUNS[rid]["rc"] = rc
            RUNS[rid]["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return rid


def tick(rel_file: str, raw: str, state: str) -> dict:
    """Flip one checkbox in the plan. Matches the verbatim line, never a number."""
    if state not in MARK:
        return {"ok": False, "error": "unknown state " + repr(state)}

    target = (REPO / rel_file).resolve()
    if REPO.resolve() not in target.parents or target.suffix != ".md":
        return {"ok": False, "error": "refusing to edit outside the repo's markdown"}
    if not target.exists():
        return {"ok": False, "error": rel_file + " does not exist"}

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [n for n, ln in enumerate(lines) if ln.rstrip("\r\n") == raw]
    if len(hits) != 1:
        found = "no" if not hits else str(len(hits))
        return {"ok": False, "stale": True,
                "error": found + " matching lines in " + rel_file
                         + " — the file moved on; refresh and try again"}

    n = hits[0]
    line = lines[n]
    if not _pr.CHECK.match(line):
        return {"ok": False, "error": "matched line is not a checkbox"}
    ob = line.index("[")
    cb = line.index("]", ob)
    lines[n] = line[:ob + 1] + MARK[state] + line[cb:]
    target.write_text("".join(lines), encoding="utf-8")

    # Keep the artifact-bound HTML in step. The PostToolUse hook only fires on
    # Claude's edits, and this edit came from a browser. The plan file — the
    # source of truth — is already written above, so a regeneration failure is
    # reported rather than fatal: the tick DID happen.
    r = subprocess.run(_py("--quiet"), cwd=str(REPO), capture_output=True,
                       creationflags=NO_WINDOW)
    out = {"ok": True, "raw": lines[n].rstrip("\r\n")}
    if r.returncode != 0:
        out["warning"] = ("the plan was updated, but regenerating the report failed (rc "
                          f"{r.returncode}): "
                          + (r.stderr or b"")[:200].decode("utf-8", "replace").strip())
        print("  regen after tick: " + out["warning"], file=sys.stderr)
    return out


def _detect_claude_app() -> str | None:
    """AppUserModelID of the Claude desktop app, when installed as a Store
    package. One Get-StartApps probe at startup — the family-hash in the id
    varies per machine, so it must be discovered, not hardcoded."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-StartApps | Where-Object { $_.Name -eq 'Claude' } | Select-Object -First 1).AppID"],
            capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW,
            **_pr.TEXT_IO)
        appid = (r.stdout or "").strip()
        return appid if appid and "!" in appid else None
    except (OSError, subprocess.SubprocessError):
        return None


def build_launchers(cfg: dict | None = None) -> dict:
    """Session launchers, detected once at startup so the page only offers what
    this machine actually has.

    `mode` decides how the phase prompt travels:
      terminal  — as an argument read from the prompt FILE (never inline: the
                  generated prompts contain `;`, which Windows Terminal treats
                  as a command separator)
      clipboard — tools with no prompt argument (desktop apps, editors) get the
                  prompt on the clipboard and the tool opened; you paste.
    Every `cmd` template is authored HERE — the browser only ever sends a
    launcher KEY, so this stays an allowlist, same rule as ACTIONS.
    """
    import shutil
    L: dict[str, dict] = {}
    if shutil.which("claude"):
        L["claude"] = {"label": "Claude Code — new session", "mode": "terminal",
                       "cmd": "claude (Get-Content -Raw -Encoding UTF8 {pf})", "cmd_blank": "claude"}
        # "Append to the session I already have open." Verified against
        # `claude --help`: -c/--continue continues the most recent conversation
        # in this directory, and still accepts a prompt argument — so the phase
        # prompt lands in the conversation that is already going rather than
        # starting a cold one that has to re-read everything.
        L["claude-continue"] = {"label": "Claude Code — continue last session",
                                "mode": "terminal",
                                "cmd": "claude --continue (Get-Content -Raw -Encoding UTF8 {pf})",
                                "cmd_blank": "claude --continue"}
    if shutil.which("opencode"):
        # --prompt verified against `opencode --help` (v as of 2026-08-30).
        L["opencode"] = {"label": "opencode — new session", "mode": "terminal",
                         "cmd": "opencode --prompt (Get-Content -Raw -Encoding UTF8 {pf})",
                         "cmd_blank": "opencode"}
        # `opencode run -c <message>` continues the last session.
        L["opencode-continue"] = {"label": "opencode — continue last session",
                                  "mode": "terminal",
                                  "cmd": "opencode run -c (Get-Content -Raw -Encoding UTF8 {pf})",
                                  "cmd_blank": "opencode"}
    appid = _detect_claude_app()
    if appid:
        L["claude-app"] = {"label": "Claude app (prompt → clipboard)",
                           "mode": "clipboard",
                           "open": ["explorer.exe", "shell:AppsFolder\\" + appid]}
    code_path = shutil.which("code")
    if code_path:
        # Full resolved path on purpose: CreateProcess does not do PATHEXT
        # resolution, so Popen(["code", ...]) cannot find the code.cmd shim.
        L["vscode"] = {"label": "VS Code (repo + prompt → clipboard)",
                       "mode": "clipboard",
                       "open": [code_path, str(REPO)]}
    return L


def _merge_config_launchers(L: dict, cfg: dict) -> None:
    """[[launcher]] tables let a project add tools beyond the built-ins.
    `detect` gates on an executable existing; cmd templates must carry {pf}
    (the prompt file); `open` argv gets only the standard {repo} expansion."""
    import shutil
    for c in cfg.get("launcher", []):
        lid = str(c.get("id", ""))
        if not _ID.match(lid):
            print(f"  config: skipping launcher with bad id {lid!r}", file=sys.stderr)
            continue
        det = c.get("detect")
        if det and not shutil.which(str(det)):
            continue
        mode = c.get("mode", "terminal")
        if mode == "terminal" and "{pf}" in str(c.get("cmd", "")):
            # {pf} now expands to an ALREADY-QUOTED PowerShell literal, so a
            # config written against the old contract ('{pf}') would end up with
            # doubled quotes. Strip the author's quotes rather than break them.
            cmd = str(c["cmd"]).replace("'{pf}'", "{pf}").replace('"{pf}"', "{pf}")
            L[lid] = {"label": c.get("label", lid), "mode": "terminal", "cmd": cmd}
        elif mode == "clipboard" and c.get("open"):
            L[lid] = {"label": c.get("label", lid), "mode": "clipboard",
                      "open": [_expand(str(x)) for x in c["open"]]}
        else:
            print(f"  config: skipping malformed launcher {lid!r}", file=sys.stderr)


LAUNCHERS: dict[str, dict] = {}
CFG: dict = {}


def trust_store() -> Path:
    """Outside every repo on purpose: a repo must not be able to pre-approve
    its own commands by shipping the trust file."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") \
        or str(Path.home() / ".config")
    return Path(base) / "progress-control-center" / "trust.json"


def _argv_digest(actions: dict, launchers: dict | None = None) -> str:
    import hashlib
    payload = {"a": {k: v["argv"] for k, v in sorted(actions.items())}}
    if launchers:
        # [[launcher]] tables are repo-authored too, and they name executables
        # this server spawns. They were never hashed or shown, so a cloned repo
        # could introduce a command through `open`/`cmd` without ever tripping
        # the gate that exists precisely to stop that.
        payload["l"] = {k: (v.get("open") or [v.get("cmd", "")])
                        for k, v in sorted(launchers.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def config_launchers(cfg: dict) -> dict:
    """Only the launchers a REPO added. The built-ins are ours, detected from
    what is installed, and are not the repo's to introduce."""
    out = {}
    for c in (cfg or {}).get("launcher", []):
        lid = str(c.get("id", ""))
        if lid and lid in LAUNCHERS:
            out[lid] = LAUNCHERS[lid]
    return out


def gate_actions() -> dict:
    """Non-interactive trust check, for switching projects from the browser.

    The startup gate asks at a console. A switch has no console, and letting a
    browser POST point the server at any repo would otherwise load THAT repo's
    [[action]] argvs into the run allowlist — which is precisely the escalation
    the gate exists to prevent.

    So a switch never grants execution. An already-approved repo keeps its
    commands; an unapproved one is served READ-ONLY: its config actions and
    launchers are stripped and named, and approving them still requires a
    restart, where the argv set can be printed and answered for.
    """
    from_cfg_a = {k: v for k, v in ACTIONS.items() if k not in ("regen", "standup")}
    from_cfg_l = config_launchers(CFG)
    if not from_cfg_a and not from_cfg_l:
        return {"trusted": True, "blocked": []}

    digest = _argv_digest(from_cfg_a, from_cfg_l)
    store = trust_store()
    try:
        db = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    except (OSError, json.JSONDecodeError):
        db = {}
    if db.get(str(REPO).lower()) == digest:
        return {"trusted": True, "blocked": []}

    blocked = sorted(from_cfg_a) + sorted(from_cfg_l)
    for k in from_cfg_a:
        ACTIONS.pop(k, None)
    for k in from_cfg_l:
        LAUNCHERS.pop(k, None)
    return {"trusted": False, "blocked": blocked}


def project_trust(repo: Path) -> str:
    """Would switching to this project keep its Run buttons? Computed the same
    way gate_actions() will compute it, so the chip in the picker cannot
    disagree with what happens when you click.

    The first version only asked whether the path appeared in the trust store.
    That said "ready" for a project whose commands had CHANGED since approval —
    which is the exact case the gate exists to catch.
    """
    cfgp = repo / "docs" / "progress.toml"
    if not cfgp.exists():
        return "unconfigured"
    try:
        cfg = tomllib.loads(cfgp.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "broken"
    acts = {k: v for k, v in build_actions(cfg, repo).items()
            if k not in ("regen", "standup")}
    lids = {str(c.get("id", "")) for c in cfg.get("launcher", [])}
    lchr = {k: v for k, v in build_launchers(cfg).items() if k in lids}
    _merge_config_launchers(lchr, cfg)
    lchr = {k: v for k, v in lchr.items() if k in lids}
    if not acts and not lchr:
        return "ready"                      # nothing repo-authored to approve
    try:
        db = json.loads(trust_store().read_text(encoding="utf-8")) \
            if trust_store().exists() else {}
    except (OSError, json.JSONDecodeError):
        db = {}
    return "ready" if db.get(str(repo).lower()) == _argv_digest(acts, lchr) else "read-only"


def browse(start: str, want: str) -> dict:
    """List one directory for the path pickers. Read-only, loopback only.

    The page cannot enumerate the filesystem and a native file picker withholds
    real paths on purpose, so a text box was the only way to name a directory —
    fine until you have to type C:\\Users\\you\\src\\thing from memory. This
    server is already on the machine, so it can simply answer.

    `want` is "dir" (directories only, for a checkout) or "md" (directories plus
    markdown, for a plan file). Nothing here writes, and the caller is gated on
    a loopback bind — on a LAN-bound dashboard this would enumerate the server's
    disk for anyone who could reach the page.
    """
    try:
        # A relative value means repo-relative - the plan file is stored that way.
        # Resolving it against the PROCESS CWD instead opened the browser wherever
        # the server happened to be launched from, which is nobody's project.
        raw = Path(start).expanduser() if start else REPO
        p = (raw if raw.is_absolute() else (REPO / raw)).resolve()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"not a usable path: {exc}"}
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else REPO
    try:
        entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        return {"ok": False, "error": f"permission denied: {p}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    dirs, files = [], []
    for e in entries[:2000]:
        try:
            if e.is_dir():
                if e.name.startswith(".") or e.name in _pr.SCAN_SKIP:
                    continue
                dirs.append({"name": e.name, "path": str(e)})
            elif want == "md" and e.suffix.lower() == ".md":
                files.append({"name": e.name, "path": str(e)})
        except OSError:
            continue          # a broken junction or a race; skip the entry
    parent = str(p.parent) if p.parent != p else ""
    return {"ok": True, "path": str(p), "parent": parent,
            "dirs": dirs[:400], "files": files[:400],
            "roots": [str(r) for r in _drive_roots()]}


def _drive_roots() -> list[Path]:
    """Somewhere to jump to when the current path is a dead end."""
    out = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = Path(f"{letter}:\\")
            if d.exists():
                out.append(d)
    else:
        out.append(Path("/"))
    home = Path.home()
    if home.exists():
        out.insert(0, home)
    return out


def switch_project(path: str) -> dict:
    """Point the running dashboard at another project."""
    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"not a usable path: {exc}"}
    if not p.is_dir():
        return {"ok": False, "error": f"{p} is not a directory"}
    try:
        init_repo(p)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"{p} has an unreadable config ({exc})"}
    gate = gate_actions()
    if gate["trusted"]:
        post_trust_setup()
    _pr.remember_project(p, (CFG.get("project", {}) or {}).get("name", ""))
    return {"ok": True, "repo": str(p),
            "name": (CFG.get("project", {}) or {}).get("name", "") or p.name,
            "configured": (p / "docs" / "progress.toml").exists(),
            "trusted": gate["trusted"], "blocked": gate["blocked"]}


def check_trust(repo: Path, actions: dict, assume_yes: bool, launchers: dict | None = None) -> bool:
    """Gate on repo-authored commands.

    `--repo` makes this tool run other repositories' configs, and [[action]] /
    [[service]] argvs are commands executed on THIS machine. Cloning a work repo
    should not silently grant it that. So the argv set is hashed and remembered;
    a new or CHANGED set has to be shown and approved once. The two built-ins
    (regen/standup) are ours, not the repo's, so a repo with no config tables
    never prompts at all.
    """
    from_cfg_a = {k: v for k, v in actions.items() if k not in ("regen", "standup")}
    from_cfg_l = launchers or {}
    if not from_cfg_a and not from_cfg_l:
        return True

    digest = _argv_digest(from_cfg_a, from_cfg_l)
    store = trust_store()
    try:
        db = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    except (OSError, json.JSONDecodeError):
        db = {}
    key = str(repo).lower()
    if db.get(key) == digest:
        return True

    print()
    print("  This repo defines commands the dashboard can run on this machine:")
    for k, v in sorted(from_cfg_a.items()):
        print(f"    action  {k:<12} {' '.join(v['argv'])}")
    for k, v in sorted(from_cfg_l.items()):
        print(f"    launcher {k:<11} {' '.join(v.get('open') or [v.get('cmd', '')])}")
    print(f"  repo: {repo}")
    print("  (" + ("changed since you last approved it" if key in db else "not seen before") + ")")
    if assume_yes:
        print("  --trust-yes given: approving without asking.")
    else:
        try:
            if input("  Approve and remember? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  not approved — start refused.", file=sys.stderr)
                return False
        except (EOFError, KeyboardInterrupt):
            print("\n  no answer (non-interactive?) — start refused. "
                  "Re-run with --trust-yes if you have reviewed these.", file=sys.stderr)
            return False
    db[key] = digest
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(db, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  warning: could not persist trust ({exc}) — you will be asked again",
              file=sys.stderr)
    return True


MANAGED = "x-managed-by"
MANAGED_BY = "progress-control-center"


def sync_context(cfg: dict) -> dict:
    """Upsert one .mcp.json entry per [[context]] provider that asks for it.

    Only entries carrying x-managed-by == progress-control-center are touched:
    a hand-added server in the same file survives untouched, and a provider
    removed from progress.toml has its managed entry removed. .mcp.json is
    committed, so every change to it shows up in `git status` and is revertible
    — which is the point of writing it rather than holding connections here.

    Secrets travel by ${VAR} reference, never by value: the token stays in the
    gitignored env file and is expanded by the agent's own MCP client.
    """
    providers = [c for c in cfg.get("context", []) if c.get("generate_mcp_json")]
    target = REPO / ".mcp.json"
    try:
        doc = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f".mcp.json is unreadable ({type(exc).__name__}) — "
                                      "fix or remove it; refusing to overwrite"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": ".mcp.json is not a JSON object — refusing to overwrite"}

    servers = doc.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error": ".mcp.json mcpServers is not an object — refusing"}

    wanted, added, updated = {}, [], []
    for c in providers:
        name = str(c.get("name", ""))
        kind, url = c.get("kind", ""), c.get("url")
        if not _ID.match(name) or not url or not str(kind).startswith("mcp-"):
            print(f"  config: skipping mcp entry for provider {name!r} "
                  f"(needs a valid name, url and mcp-* kind)", file=sys.stderr)
            continue
        entry: dict = {"type": "http", "url": str(url), MANAGED: MANAGED_BY}
        if c.get("auth_env"):
            entry["headers"] = {"Authorization": "Bearer ${" + str(c["auth_env"]) + "}"}
        wanted[name] = entry
        prev = servers.get(name)
        if prev is None:
            added.append(name)
        elif prev != entry:
            if prev.get(MANAGED) != MANAGED_BY:
                print(f"  config: {name!r} exists in .mcp.json but is not managed — leaving it",
                      file=sys.stderr)
                wanted.pop(name)
                continue
            updated.append(name)

    removed = [n for n, v in servers.items()
               if isinstance(v, dict) and v.get(MANAGED) == MANAGED_BY and n not in wanted]
    for n in removed:
        servers.pop(n)
    servers.update(wanted)

    if not servers:
        doc.pop("mcpServers", None)
    if added or updated or removed:
        if doc:
            target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        elif target.exists():
            target.unlink()
    return {"ok": True, "added": added, "updated": updated, "removed": removed,
            "path": str(target), "total_managed": len(wanted)}


def init_repo(path: Path) -> None:
    """Point the server (and the imported generator) at a repo, then rebuild the
    per-repo allowlists from its progress.toml. One installed copy, any project."""
    global REPO, PROMPT_DIR, CFG
    REPO = Path(path).resolve()
    _pr.set_repo(REPO)
    # Only YOUR dashboard may carry your profile. Bound to anything but
    # loopback the page is served to other people, and the profile it would
    # bake in describes this server's machine, not the viewer's - a checkout
    # they do not have and a shell whose syntax breaks on paste.
    _pr.LOCAL_SURFACE = BIND_HOST in ('127.0.0.1', 'localhost', '::1')
    PROMPT_DIR = REPO / ".pcc"
    cfgp = REPO / "docs" / "progress.toml"
    # An unconfigured repo must still START, because /setup is the thing that
    # configures it — a wizard you can only reach once you no longer need it
    # would be useless on exactly the machine that needs it.
    CFG = tomllib.loads(cfgp.read_text(encoding="utf-8")) if cfgp.exists() else {}
    ACTIONS.clear()
    ACTIONS.update(build_actions(CFG))
    LAUNCHERS.clear()
    LAUNCHERS.update(build_launchers(CFG))
    _merge_config_launchers(LAUNCHERS, CFG)
    _pr.remember_project(REPO, (CFG.get("project", {}) or {}).get("name", ""))
    # Sync at startup so a session launched seconds later already has its MCP
    # servers. Reported, never silent: this writes a committed file.
    return CFG


def post_trust_setup() -> None:
    """Side effects that must not happen before the commands are approved."""
    if any(c.get("generate_mcp_json") for c in CFG.get("context", [])):
        r = sync_context(CFG)
        if not r.get("ok"):
            print("  mcp sync: " + str(r.get("error")), file=sys.stderr)
        elif r["added"] or r["updated"] or r["removed"]:
            print(f"  mcp sync: +{len(r['added'])} ~{len(r['updated'])} -{len(r['removed'])}"
                  f" in {r['path']}")


def _env_prelude() -> str:
    """PowerShell that loads the context env file into the NEW terminal, by path.

    Providers declare `auth_env = "DOCS_JWT"` — a variable NAME. The value
    lives in a gitignored env file, and it has to reach the launched agent's MCP
    client, which expands ${VAR}.

    It travels by PATH, never by value. `Popen(env=...)` cannot work here: with
    `wt.exe -w 0 nt` the tab's shell is spawned by the EXISTING terminal process,
    so our environment is not inherited. Putting the token in the command line
    instead would expose it in the process list, in shell history, and in this
    server's memory. So the generated command reads the file itself; the only
    secret-adjacent thing that ever appears is a file path.

    Returns "" when no env file is configured or present — no provider, no cost.
    """
    # One file, the project's own. Every token a session needs — provider JWTs,
    # the JIRA token, a forge PAT — is loaded from the same place.
    files = _secret_files()
    if not files:
        return ""

    # NEWLINE-separated, not `; `-joined. This text goes into a .ps1 that the
    # terminal runs with -File, and a semicolon on a wt.exe command line is a
    # COMMAND SEPARATOR: wt split there and tried to launch the rest as a
    # program ("0x80070002 The system cannot find the file specified"). The file
    # in this module already warned about that for prompts; the prelude
    # reintroduced it the moment it had anything to emit.
    return "".join(
        "foreach($l in Get-Content -Encoding UTF8 " + _ps_lit(f) + "){"
        "if($l -match '^\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(.*)$'){"
        "Set-Item -Path \"env:$($Matches[1])\" -Value $Matches[2].Trim()}}\n"
        for f in files)


def _ps_lit(s) -> str:
    r"""A PowerShell single-quoted literal. Apostrophes are escaped by doubling.

    Paths come from the filesystem, and a checkout under a name like O'Brien
    would otherwise close the quote early and turn the rest of the path into
    PowerShell syntax. _env_prelude already did this; the clipboard command
    and the launcher templates did not.
    """
    return "'" + str(s).replace("'", "''") + "'"


def _copy_clipboard(path: Path) -> bool:
    """Copy a file's contents to the SERVER's clipboard, and VERIFY it.

    Belt and braces: the page copies to the viewer's clipboard itself, which is
    the correct one when the dashboard is reached over a tunnel. This exists for
    clipboard-mode launchers, where the paste target is a local app.

    The exit code is checked. The first version returned True whether or not the
    copy worked, so "prompt copied" was an assertion the code had not earned —
    and the user is then told to paste something that is not there.
    """
    for argv in (
        ["powershell", "-NoProfile", "-Command",
         "Get-Content -Raw -Encoding UTF8 " + _ps_lit(path) + " | Set-Clipboard"],
        ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
    ):
        try:
            if argv[0] == "powershell":
                r = subprocess.run(argv, capture_output=True, timeout=15, creationflags=NO_WINDOW)
            else:
                with path.open("rb") as fh:
                    r = subprocess.run(argv, stdin=fh, capture_output=True, timeout=15)
            if r.returncode == 0:
                return True
            print(f"  clipboard: {argv[0]} exited {r.returncode}: "
                  f"{(r.stderr or b'')[:200].decode('utf-8', 'replace').strip()}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def open_session(phase_id: str, prompt: str, tool: str = "claude",
                 blank: bool = False) -> dict:
    """Open a development session in the chosen tool, seeded with the phase prompt.

    Both things always happen — the session opens AND the prompt is available to
    paste — and the result says which route delivered it. The old version put the
    prompt on the clipboard in clipboard mode only, so on a terminal launcher the
    "Copy prompt" you thought you had was not there.
    """
    spec = LAUNCHERS.get(tool)
    if spec is None:
        return {"ok": False, "error": "launcher " + repr(tool) + " not available on this machine"}

    safe = "".join(c for c in phase_id if c.isalnum()) or "x"
    PROMPT_DIR.mkdir(exist_ok=True)
    pf = PROMPT_DIR / ("prompt-" + safe + ".txt")
    pf.write_text(prompt, encoding="utf-8")
    copied = False if blank else _copy_clipboard(pf)

    if spec["mode"] == "terminal":
        import shutil
        # The command goes in a .ps1 run with -File, never on the command line.
        # wt.exe treats `;` as a command separator and PowerShell -Command needs
        # a second level of quoting; a generated prompt or an env prelude hits
        # both. A script file has neither problem — the same fix this repo's
        # PowerShell helpers already use.
        body = (
            "# Generated by the control center. Safe to delete.\n"
            "$ErrorActionPreference = 'Continue'\n"
            + _env_prelude()
            + (spec.get("cmd_blank") if blank and spec.get("cmd_blank")
               else spec["cmd"].format(pf=_ps_lit(pf)))
            + "\n")
        ps1 = PROMPT_DIR / ("launch-" + safe + ".ps1")
        # utf-8-SIG: Windows PowerShell 5.1 reads a .ps1 as ANSI unless it
        # finds a BOM, which would mangle any non-ASCII path inside it.
        ps1.write_text(body, encoding="utf-8-sig")
        # Do not hard-code pwsh: Windows 11 ships Windows Terminal but PowerShell 7
        # is a separate install, so `pwsh` is often absent while `powershell` works.
        shell = "pwsh" if shutil.which("pwsh") else "powershell"
        tried = []
        for argv in (
            ["wt.exe", "-w", "0", "nt", "-d", str(REPO), shell,
             "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            [shell, "-NoExit", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        ):
            try:
                proc = subprocess.Popen(argv, cwd=str(REPO), creationflags=NEW_CONSOLE)
            except (OSError, subprocess.SubprocessError) as exc:
                # Catch every spawn failure, not just FileNotFoundError. A
                # PermissionError or WinError 193 used to escape the handler,
                # drop the connection, and leave the button stuck on "Opening…".
                tried.append(f"{argv[0]}: {type(exc).__name__}")
                continue
            # Popen only proves a process image was created. wt.exe is a hand-off
            # stub: an old build, a Store alias for an uninstalled terminal, or a
            # missing inner shell all exit immediately, and reporting "session
            # started" from process creation alone is the same unearned claim the
            # clipboard copy used to make. Give it a moment and check.
            time.sleep(0.6)
            rc = proc.poll()
            if rc in (None, 0):
                return {"ok": True, "via": argv[0], "tool": tool, "copied": copied,
                        "prompt_file": str(pf), "mode": "terminal",
                        "blank": blank,
                        "note": ("opened with no prompt" if blank else
                                 "prompt sent into the session" +
                                 (" · also on your clipboard" if copied else ""))}
            tried.append(f"{argv[0]}: exited {rc}")
        return {"ok": False, "error":
                "could not start a terminal session (" + "; ".join(tried) + "). The prompt is "
                + ("on your clipboard and " if copied else "") + "in " + str(pf) +
                " — open the tool yourself."}

    # clipboard mode: the tool takes no prompt argument, so paste is the delivery.
    try:
        subprocess.Popen(spec["open"], cwd=str(REPO), creationflags=NO_WINDOW)
        return {"ok": True, "via": spec["open"][0], "tool": tool, "copied": copied,
                "mode": "clipboard",
                "note": ("prompt on your clipboard — paste it into the session" if copied
                         else "opened, but the clipboard copy failed — use `view prompt`")}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}


def phase_activity(phase_id: str, model: dict) -> dict:
    """What has actually changed in a phase's work tree.

    A phase declares `modules` (paths). The plan says what SHOULD happen there;
    git says what did. Read-only, and scoped to the declared paths — this never
    runs a repo-supplied command, it runs git with paths as arguments.
    """
    ph = next((p for p in model.get("phases", []) if str(p["id"]) == str(phase_id)), None)
    if ph is None:
        return {"ok": False, "error": "unknown phase " + repr(phase_id)}
    mods = [str(m) for m in (ph.get("modules") or []) if str(m).strip()]
    docs = [d for d in (ph.get("doc"), ) if d]
    paths = mods + docs
    if not paths:
        return {"ok": True, "paths": [], "commits": [], "stat": "",
                "note": "no modules or doc declared for this phase — add `modules` to see activity"}
    try:
        # Check the exit code. A failed git run yields empty stdout, which is
        # indistinguishable from "succeeded and found nothing" — so the panel
        # used to report "no commits yet" for a repo git could not even read.
        lg = subprocess.run(["git", "-C", str(REPO), "log", "--pretty=%h|%ad|%an|%s",
                             "--date=short", "-15", "--", *paths],
                            capture_output=True, text=True, timeout=20, **_pr.TEXT_IO)
        if lg.returncode != 0:
            return {"ok": False, "error": "git log failed: " +
                    (lg.stderr or "").strip()[:200]}
        log = lg.stdout.strip()
        st = subprocess.run(["git", "-C", str(REPO), "diff", "--stat", "HEAD", "--", *paths],
                            capture_output=True, text=True, timeout=20, **_pr.TEXT_IO)
        stat = st.stdout.strip() if st.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    commits = []
    for line in log.splitlines():
        bits = line.split("|", 3)
        if len(bits) == 4:
            commits.append(dict(zip(("sha", "date", "author", "subject"), bits)))
    return {"ok": True, "paths": paths, "commits": commits, "stat": stat}


def ticket_prompt(ph: dict, plan: str, doc: str, open_items: list,
                  project: str, out: Path, cap: int = 1600) -> str:
    """The drafting prompt.

    The first version asked for "what, why, acceptance criteria, and the exit
    test" with no length limit, told the session to read the code so the ticket
    reflected real work, and invited it to record scope doubts in the
    description. It obligingly produced 8,500 characters: a repo-state
    inventory, rationale essays and four paragraphs of open questions. Good
    analysis; wrong artifact. A ticket is a work order, not a design document.

    So: a fixed skeleton, hard caps per section, an explicit character budget,
    and a named list of things that must NOT appear. The investigation still
    happens — it just informs the ticket instead of being pasted into it.
    """
    items = "\n".join("  - " + i for i in open_items) or "  (none open)"
    return (
        f"Draft a JIRA ticket for Phase {ph['id']} ({ph['name']}) of {plan}.\n\n"

        "Read for context first — then throw the reading away and write a short work "
        f"order. Sources: {doc} if it exists, otherwise that phase's section of {plan}, "
        f"and the code under {', '.join(ph.get('modules') or ['the repo'])}. The point of "
        "reading is that the scope and the acceptance criteria are TRUE, not that the "
        "ticket recounts what you read.\n\n"

        f"Exit test for the phase: {ph.get('exit_test', 'see plan')}\n"
        f"Open checklist items:\n{items}\n"
        + (f"JIRA project key: {project}\n" if project else "")

        + "\nWrite EXACTLY this structure into the description, in this order, using "
        "these headings verbatim:\n\n"
        "Scope\n"
        "- 3 to 6 bullets. One line each. What will be built or changed, concretely.\n"
        "Out of scope\n"
        "- 0 to 3 bullets. Things a reader would otherwise assume are included.\n"
        "Acceptance criteria\n"
        "- 3 to 7 bullets. Each must be decidable by INSPECTING A NAMED THING or RUNNING "
        "A NAMED COMMAND, so two reviewers would always agree. Ban judgement words: "
        "vetted, reviewed, appropriate, properly, correctly, sensible, intelligent, "
        "relevant, secure, clean. If a scope item resists that, put the check on its "
        "artifact instead — not \"no unvetted plugin is installed\" but \"the installed "
        "plugin list is empty, or every entry has a source recorded in <file>\". Every "
        "criterion must correspond to something in Scope; do not test work the ticket "
        "does not ask for. ONE assertion per bullet — a bullet that checks two things "
        "can half-pass, and then nobody knows what to do with it. No rationale.\n"
        "Exit test\n"
        "- the phase exit test, one line, as given above.\n"
        "Blockers\n"
        "- 0 to 3 bullets, only things that genuinely stop the work starting.\n"
        "Open questions\n"
        "- 0 to 3 bullets, ONE line each, only where the answer changes what gets built. "
        "If there are none, omit this heading entirely.\n\n"

        f"HARD LIMITS. summary: one imperative line, at most 80 characters. description: "
        f"at most {cap} characters TOTAL — count it, and cut until it fits. Every bullet "
        "one line. No sub-bullets. No nested headings.\n\n"

        "Do NOT include, at all: a summary of the repository's current state; an "
        "inventory of files you looked at; explanations of why the phase exists; "
        "quotations from code or config; measurements; a 'what I based this on' section; "
        "notes to the reviewer about your own process; or any paragraph of prose. If a "
        "sentence explains rather than instructs, delete it.\n\n"

        f"Write the result as JSON to {out} with exactly two keys, summary and "
        "description. Serialise it with a JSON library rather than by hand — the "
        "description contains newlines and they must be escaped as \\n inside the string "
        "or the file will not parse.\n\n"

        "Write ONLY that file. Do not create the ticket, do not call any JIRA API, and do "
        "not modify the plan — a person reviews this draft and submits it. Where scope is "
        "genuinely undecided, put ONE line under Open questions; do not write an essay "
        "about it, and do not invent a decision to avoid the question."
    )


def _draft_path(phase_id: str) -> Path:
    safe = "".join(c for c in str(phase_id) if c.isalnum()) or "x"
    return PROMPT_DIR / f"ticket-{safe}.json"


def draft_ticket(phase_id: str, tool: str, model: dict) -> dict:
    """Ask a CODING SESSION to draft a ticket for this phase.

    Deliberately not an LLM call from here. The session already has the repo,
    the plan, the phase doc and every configured context provider, and it
    already routes through whichever model you set up — so the dashboard stays
    a stdlib renderer with no model client, no second model configuration and
    no additional credential to hold. It hands over a prompt; the session writes
    the JSON; this reads it back.
    """
    ph = next((p for p in model.get("phases", []) if str(p["id"]) == str(phase_id)), None)
    if ph is None:
        return {"ok": False, "error": "unknown phase " + repr(phase_id)}

    # This prompt asks a session to WRITE A FILE. A clipboard-mode launcher only
    # copies text and opens a GUI app — it cannot run anything, so the draft
    # would never appear and the button would be permanently, silently broken.
    # Refuse rather than pretend, and name a launcher that can do the job.
    spec = LAUNCHERS.get(tool)
    if spec is None:
        return {"ok": False, "error": "launcher " + repr(tool) + " is not available here"}
    if spec.get("mode") != "terminal":
        can = [v["label"] for k, v in LAUNCHERS.items() if v.get("mode") == "terminal"]
        return {"ok": False, "error":
                f"{spec['label']} can only receive a prompt on the clipboard — it cannot run "
                "and write the draft file. " +
                (f"Pick one of: {', '.join(can)}." if can else
                 "No terminal launcher (claude / opencode) was found on this machine.")}

    out = _draft_path(phase_id)
    open_items = [i["label"] for i in ph.get("items", []) if i["state"] != "done"]
    jira_cfg = (CFG.get("integrations", {}) or {}).get("jira", {}) or {}
    project = jira_cfg.get("project_key") or jira_cfg.get("project") or ""
    doc = ph.get("doc") or f"docs/PHASE-{ph['id']}.md"

    cap = int(jira_cfg.get("draft_max_chars", 1600) or 1600)
    prompt = ticket_prompt(ph, model['project'].get('plan', 'the plan'), doc,
                           open_items, project, out, cap)
    PROMPT_DIR.mkdir(exist_ok=True)
    try:
        out.unlink()                      # so a stale draft cannot look like a new one
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {"ok": False, "error": f"could not clear the old draft: {exc}"}

    r = open_session(f"{phase_id}-ticket", prompt, tool)
    if not r.get("ok"):
        return r
    # The session is INTERACTIVE on purpose: it will ask before writing, and a
    # person seeing the words that are about to become a ticket is the point.
    # Say so, or "it will write the draft" reads as a promise it keeps by itself.
    r["note"] = ("session opened in " + str(r.get("tool")) + " — approve the write in that "
                 "window, then press Load draft (" + out.name + ")")
    r["path"] = str(out)
    return r


def read_ticket_draft(phase_id: str) -> dict:
    """Read a draft a session wrote. Treated as DATA: it is model-written text
    that a person reviews and submits, never something acted on directly."""
    p = _draft_path(phase_id)
    if not p.exists():
        return {"ok": True, "draft": None, "path": str(p), "jira": jira_target()}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(p)}
    # A session may wrap the JSON in a ``` fence; take the outermost object.
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"ok": False, "error": f"{p.name} holds no JSON object", "path": str(p)}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"{p.name} is not valid JSON ({exc})", "path": str(p)}
    return {"ok": True, "path": str(p), "jira": jira_target(),
            "draft": {"summary": str(d.get("summary", ""))[:250],
                      "description": str(d.get("description", ""))}}


def project_secrets_path() -> Path:
    """This project's token file — the shared definition, bound to this repo."""
    return _pr.project_secrets_path(REPO, CFG)


def _secret_files() -> list[Path]:
    """Where a token may live. One file now; a list because callers iterate."""
    p = project_secrets_path()
    return [p] if p.exists() else []


def _read_secret(var: str) -> str | None:
    """Read one variable's value from the gitignored env files.

    This is the first place the dashboard reads a secret VALUE rather than
    passing a path. Creating an issue over the API cannot be done any other way.
    It is read on demand, used once, never cached, never logged, and never
    returned to the page — /api/setup still reports only which names are set.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var or ""):
        return None
    pat = re.compile(r"^\s*" + re.escape(var) + r"\s*=\s*(.*?)\s*$")
    for f in _secret_files():
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                m = pat.match(line)
                if m and m.group(1):
                    return m.group(1).strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def _adf(text: str) -> dict:
    """Plain text -> Atlassian Document Format, for Jira Cloud's v3 API.

    v3 refuses a plain string description. Only what the ticket skeleton
    actually produces is handled: our headings, "- " bullets, and paragraphs.
    Anything else degrades to a paragraph rather than being dropped.
    """
    heads = {"Scope", "Out of scope", "Acceptance criteria", "Exit test",
             "Blockers", "Open questions"}
    content, bullets = [], []

    def flush():
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": b}]}]}
                for b in bullets]})
            bullets.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.strip() in heads:
            flush()
            content.append({"type": "heading", "attrs": {"level": 3},
                            "content": [{"type": "text", "text": line.strip()}]})
        elif line.lstrip().startswith(("- ", "* ")):
            bullets.append(line.lstrip()[2:].strip())
        else:
            flush()
            content.append({"type": "paragraph",
                            "content": [{"type": "text", "text": line.strip()}]})
    flush()
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]
    return {"type": "doc", "version": 1, "content": content}


def jira_target() -> dict:
    """What API creation would do, and what is missing. Shown BEFORE the button
    is armed: an outward-facing write should never be a surprise."""
    j = (CFG.get("integrations", {}) or {}).get("jira", {}) or {}
    base = str(j.get("api_base", "")).rstrip("/")
    var = str(j.get("auth_env", "JIRA_PAT"))
    missing = []
    if not base:
        missing.append("[integrations.jira].api_base")
    elif not re.match(r"^https?://", base):
        missing.append("api_base must start with http(s)://")
    if not j.get("project_key"):
        missing.append("[integrations.jira].project_key")
    if base and not _read_secret(var):
        missing.append(f"${var} — store it on /setup → This machine → Tokens")
    return {"configured": not missing, "missing": missing,
            "base": base, "project": str(j.get("project_key", "")),
            "issue_type": str(j.get("issue_type", "Task")),
            "auth_env": var, "auth_mode": str(j.get("auth_mode", "bearer")),
            "api_version": int(j.get("api_version", 3) or 3),
            "insecure": bool(base.startswith("http://"))}


def create_jira_issue(phase_id: str, summary: str, description: str) -> dict:
    """Create the issue, then record its key on the phase.

    Outward-facing and effectively irreversible — you cannot un-create a ticket,
    only close it — so nothing here happens implicitly: the caller has reviewed
    an editable draft and pressed a button that names the project it will land
    in. A phase that already has a key is refused, so a double click or a
    retried request cannot raise a second ticket.
    """
    ph_cfg = next((p for p in CFG.get("phase", []) if str(p.get("id")) == str(phase_id)), None)
    if ph_cfg is None:
        return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r}"}
    if ph_cfg.get("jira"):
        return {"ok": False, "error": f"phase {phase_id} already has ticket "
                                      f"{ph_cfg['jira']} — unlink it first"}
    summary = str(summary or "").strip()
    description = str(description or "").strip()
    if not summary:
        return {"ok": False, "error": "the summary is empty"}
    if len(summary) > 255:
        return {"ok": False, "error": f"summary is {len(summary)} chars; JIRA's limit is 255"}

    t = jira_target()
    if not t["configured"]:
        return {"ok": False, "error": "not configured: " + "; ".join(t["missing"])}
    token = _read_secret(t["auth_env"])
    if not token:
        return {"ok": False, "error": f"${t['auth_env']} is not set"}

    fields = {"project": {"key": t["project"]},
              "issuetype": {"name": t["issue_type"]},
              "summary": summary,
              "description": _adf(description) if t["api_version"] >= 3 else description}
    body = json.dumps({"fields": fields}).encode("utf-8")
    url = f"{t['base']}/rest/api/{t['api_version']}/issue"

    if t["auth_mode"] == "basic":
        user = str(((CFG.get("integrations", {}) or {}).get("jira", {}) or {}).get("auth_user", ""))
        if not user:
            return {"ok": False, "error": "auth_mode = basic needs [integrations.jira].auth_user"}
        import base64
        auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    else:
        auth = "Bearer " + token

    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": auth, "User-Agent": "progress-control-center"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            created = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            msg = "; ".join(detail.get("errorMessages", []) or
                            [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()])
        except (ValueError, OSError):
            msg = ""
        return {"ok": False, "error": f"JIRA said {exc.code} {exc.reason}" +
                (f" — {msg}" if msg else "") +
                (" (check project_key, issue_type and the token's permissions)"
                 if exc.code in (400, 401, 403) else "")}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"could not reach {t['base']}: {exc.reason}. "
                "An internal JIRA behind a private CA needs that CA trusted by Python."}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    key = str(created.get("key", ""))
    if not key:
        return {"ok": False, "error": "JIRA accepted the request but returned no issue key"}
    linked = link_ticket(phase_id, key)
    browse = ((CFG.get("integrations", {}) or {}).get("jira", {}) or {}).get("browse_url", "")
    return {"ok": True, "key": key,
            "url": browse.replace("{key}", key) if browse else f"{t['base']}/browse/{key}",
            "linked": linked.get("ok", False),
            "link_error": linked.get("error", "")}


def link_ticket(phase_id: str, key: str) -> dict:
    """Record a ticket key on a phase — the missing half of `+ create ticket`.

    Scoped hard: it writes exactly one `jira` key into one `[[phase]]`, and the
    value must look like a ticket key or a URL. Same class of edit as the setup
    wizard's, nowhere near [[action]]."""
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "error": "no ticket key given"}
    if not (re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) or re.match(r"^https?://\S+$", key)):
        return {"ok": False, "error": "expected a ticket key like PROJ-123, or a full URL"}
    cfgp = REPO / "docs" / "progress.toml"
    try:
        text = cfgp.read_text(encoding="utf-8")
        new = _pr.set_phase_key(text, str(phase_id), "jira", key)
        tomllib.loads(new)                      # never write a file we just broke
        cfgp.write_text(new, encoding="utf-8")
    except KeyError:
        return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r} in progress.toml"}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    CFG.clear()
    CFG.update(tomllib.loads(cfgp.read_text(encoding="utf-8")))
    return {"ok": True, "key": key, "path": str(cfgp)}


# ---------------------------------------------------------------- action layer

CSS = """
#pcc-bar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;gap:8px;align-items:center;
 flex-wrap:wrap;padding:10px 14px;background:var(--panel);border-top:1px solid var(--line);
 box-shadow:0 -6px 24px -18px rgba(0,0,0,.6)}
#pcc-bar .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
 color:var(--ink-3);margin-right:2px}
.pcc-btn{appearance:none;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
 font:inherit;font-size:12.5px;padding:6px 12px;border-radius:7px;cursor:pointer}
.pcc-btn:hover{border-color:var(--accent);color:var(--accent)}
.pcc-btn[disabled]{opacity:.5;cursor:progress}
.pcc-btn.run{border-color:var(--accent);background:var(--accent);color:#fff}
.pcc-btn.run:hover{color:#fff;filter:brightness(1.08)}
#pcc-out{position:fixed;right:14px;bottom:58px;z-index:51;width:min(680px,calc(100vw - 28px));
 max-height:52vh;display:none;flex-direction:column;background:var(--panel);
 border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);overflow:hidden}
#pcc-out.on{display:flex}
#pcc-out header{display:flex;align-items:center;justify-content:space-between;gap:10px;
 padding:8px 12px;border-bottom:1px solid var(--line);background:var(--panel-2)}
#pcc-out h4{margin:0;font-size:12.5px;font-weight:650}
#pcc-out pre{margin:0;padding:11px 13px;overflow:auto;font-family:var(--mono);font-size:11.5px;
 line-height:1.55;white-space:pre-wrap;word-break:break-word}
#pcc-out .rc{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:999px;margin-left:auto}
#pcc-out .rc.ok{background:var(--done-soft);color:var(--done)}
#pcc-out .rc.bad{background:var(--crit-soft);color:var(--crit)}
#pcc-out .rc.run{background:var(--accent-soft);color:var(--accent)}
li[data-pcc] .box{cursor:pointer}
li[data-pcc] .box:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
li[data-pcc].busy{opacity:.55}
.pcc-local{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
 background:var(--done-soft);color:var(--done);padding:2px 7px;border-radius:999px;font-weight:700}
#pcc-svc{position:fixed;left:0;right:0;bottom:52px;z-index:49;display:flex;gap:10px;
 flex-wrap:wrap;padding:0 14px}
#pcc-svc:empty{display:none}
.pcc-svc-chip{display:flex;align-items:center;gap:7px;background:var(--panel);
 border:1px solid var(--line);border-radius:8px;padding:4px 8px;font-size:12.5px;
 box-shadow:var(--shadow)}
body{padding-bottom:104px}
"""

JS = r"""
(function(){
  var T = window.__ANU_TOKEN__, MAP = window.__ANU_ITEMS__ || {}, poll = null;

  function api(path, body){
    return fetch(path, {
      method:'POST',
      headers:{'Content-Type':'application/json','X-PCC-Token':T},
      body: JSON.stringify(body||{})
    }).then(function(r){ return r.json(); });
  }

  var out = document.getElementById('pcc-out'),
      pre = out.querySelector('pre'),
      ttl = out.querySelector('h4'),
      rcEl = out.querySelector('.rc'),
      btns = Array.prototype.slice.call(document.querySelectorAll('.pcc-btn[data-task]'));

  function enable(on){ btns.forEach(function(b){ b.disabled = !on; }); }

  function show(title){
    ttl.textContent = title; pre.textContent = '';
    rcEl.textContent = 'running'; rcEl.className = 'rc run';
    out.classList.add('on');
  }

  function tail(id){
    clearInterval(poll);
    poll = setInterval(function(){
      fetch('/api/run/' + id).then(function(r){ return r.json(); }).then(function(d){
        if(d.error){ clearInterval(poll); rcEl.textContent='error'; rcEl.className='rc bad';
                     pre.textContent = d.error; enable(true); return; }
        pre.textContent = d.lines.join('\n');
        pre.scrollTop = pre.scrollHeight;
        if(d.done){
          clearInterval(poll);
          rcEl.textContent = 'exit ' + d.rc;
          rcEl.className = 'rc ' + (d.rc === 0 ? 'ok' : 'bad');
          enable(true);
        }
      }).catch(function(){ clearInterval(poll); enable(true); });
    }, 400);
  }

  btns.forEach(function(b){
    b.addEventListener('click', function(){
      enable(false);
      show(b.dataset.label);
      api('/api/run', {task: b.dataset.task}).then(function(d){
        if(d.run_id){ tail(d.run_id); }
        else { rcEl.textContent='error'; rcEl.className='rc bad';
               pre.textContent = d.error || 'failed'; enable(true); }
      });
    });
  });

  out.querySelector('.x').addEventListener('click', function(){
    out.classList.remove('on'); clearInterval(poll);
  });

  // Context providers: reachability only, no start/stop. Whether a launched
  // session could reach its knowledge is worth a chip; owning the process is not.
  var svcWrap = document.getElementById('pcc-svc');
  function renderSvc(rows){
    if(!svcWrap) return;
    if(!rows.length){ svcWrap.innerHTML=''; return; }
    svcWrap.innerHTML = '<span class="lbl">context</span>' + rows.map(function(r){
      var cls = r.state === 'reachable' ? 'done' : (r.state === 'unreachable' ? 'crit' : '');
      return '<span class="pcc-svc-chip" title="' + (r.hint||'') + ' — ' + (r.url||'') + '">'
        + '<span class="pill ' + cls + '">' + r.state + '</span> ' + r.label + '</span>';
    }).join('');
  }
  function pollSvc(){
    fetch('/api/context').then(function(r){ return r.json(); })
      .then(function(d){ renderSvc(d.providers || []); }).catch(function(){});
  }
  if(svcWrap){ pollSvc(); setInterval(pollSvc, 15000); }

  // Open a real session on a phase in the tool of your choice, seeded with the
  // same prompt the artifact can only offer for copying. The select is built
  // from launchers DETECTED server-side; the page only ever sends a key.
  var LN = window.__ANU_LAUNCHERS__ || {};
  var lnKeys = Object.keys(LN);

  // Which tool to preselect: your last choice on this page, else the tool from
  // your setup profile. The wizard asked for a preferred tool and the launcher
  // used to ignore it, which made the question look decorative.
  function preferredTool(){
    try { if (localStorage.pccLauncher && LN[localStorage.pccLauncher]) return localStorage.pccLauncher; } catch(e){}
    var t = window.__ANU_PROFILE_TOOL__;
    if (t && LN[t]) return t;
    // Prefer a launcher that actually delivers the prompt into a session over
    // one that can only put it on the clipboard for you to paste.
    var term = lnKeys.filter(function(k){ return LN[k].mode === 'terminal'; });
    return term[0] || lnKeys[0];
  }
  // A launcher that can actually RUN something. Drafting a ticket means writing
  // a file, which a clipboard-mode GUI app can never do.
  function terminalTools(){
    return lnKeys.filter(function(k){ return LN[k].mode === 'terminal'; });
  }
  function toolSelect(){
    var sel = document.createElement('select');
    sel.className = 'pcc-btn';
    // A select with no label announces only its current value. There is no
    // visible label to point at here, so it carries its own.
    sel.setAttribute('aria-label', 'Coding tool to open the session in');
    lnKeys.forEach(function(k){
      var o = document.createElement('option');
      o.value = k; o.textContent = LN[k].label; sel.appendChild(o);
    });
    sel.value = preferredTool();
    return sel;
  }
  // Copy to the VIEWER's clipboard, not the server's — the right one when this
  // page is reached over a tunnel. The server also tries, as a fallback.
  function copyLocal(text){
    try {
      if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    } catch(e){}
    return Promise.reject();
  }
  // One launch routine, used by the start cards and by the phase drawer.
  function launch(btn, phaseId, prompt, tool, say){
    btn.disabled = true;
    var was = btn.textContent;
    btn.textContent = 'Opening…';
    try { localStorage.pccLauncher = tool; } catch(e){}
    var copied = false;
    copyLocal(prompt).then(function(){ copied = true; }, function(){}).then(function(){
      return api('/api/session', {phase: phaseId, prompt: prompt, tool: tool});
    }).then(function(d){
      btn.disabled = false; btn.textContent = was;
      if(!d.ok){ if(say) say(d.error, 'err'); else alert(d.error); return; }
      // Say exactly what happened and what is left for you to do. "paste it in"
      // was too vague to act on, and claiming a copy we had not verified was
      // worse than saying nothing.
      var name = LN[d.tool] ? LN[d.tool].label : (d.tool || d.via);
      var ok = copied || d.copied, msg, cls = 'ok';
      if(d.mode === 'clipboard'){
        msg = ok ? (name + ' opened — the prompt is on your clipboard, press Ctrl+V in it')
                 : (name + ' opened, but the clipboard copy FAILED — open “view prompt” ' +
                    'below and copy it by hand');
        if(!ok) cls = 'err';
      } else {
        msg = 'Session started in ' + name + ' with the prompt already in it' +
              (ok ? ' (also copied to your clipboard)' : '');
      }
      if(say) say(msg, cls); else { btn.textContent = ok ? 'Opened ✓' : 'Opened (no copy)';
        btn.title = msg; setTimeout(function(){ btn.textContent = was; }, 4000); }
    });
  }
  window.__pccLaunch__ = launch;
  window.__pccToolSelect__ = toolSelect;

  // The shared render draws a .launch row (prompt + Copy prompt + Details) on
  // every start card. The local layer used to ALSO append its own Open session
  // and tool picker here, and then the full action row was added below — two
  // rows, two independent tool selects that disagreed with each other. The
  // launcher belongs in exactly one place: the action row. Here we only make
  // the raw prompt collapsible, since it is reference material, not the control.

  // Per-item start. Each checklist item is a unit of work in its own right, so
  // it gets its own session prompt — scoped to that one line, with an explicit
  // instruction not to widen silently.
  function itemPrompt(p, label){
    if(!p.item_tmpl) return p.prompt;
    return p.item_tmpl.split(p.slot).join(label);
  }
  // For an item already ticked: verify, do not rebuild.
  function recheckPrompt(p, label){
    return 'In Phase ' + p.id + ' (' + p.name + '), this checklist item is marked DONE:\n\n' +
           '    ' + label + '\n\n' +
           'Verify that it really is. Check the code and the repo state rather than the plan ' +
           'text. If it holds, say so and change nothing. If it does not, say exactly what is ' +
           'missing and untick it — do not quietly re-do the work.';
  }
  // A session with no prompt at all, for when there is nothing to instruct.
  function launchBlank(btn, phaseId, tool, say){
    btn.disabled = true;
    var was = btn.textContent; btn.textContent = 'Opening…';
    api('/api/session', {phase: phaseId, prompt: '', tool: tool, blank: true})
      .catch(function(){ return {ok: false, error: 'could not reach the server'}; })
      .then(function(d){
        btn.disabled = false; btn.textContent = was;
        if(!d.ok){ if(say) say(d.error, 'err'); else alert(d.error); return; }
        var name = LN[d.tool] ? LN[d.tool].label : (d.tool || d.via);
        if(say) say(name + ' opened in the repo with no prompt', 'ok');
      });
  }
  // Each checklist item gets its own expandable action bar. A single greyed-out
  // "start" was both undiscoverable and under-powered: an item is a unit of
  // work, so it deserves the same choices a phase gets — which tool, a copyable
  // prompt, a command for your own machine, and an explicit state control
  // instead of a checkbox you have to know cycles on click.
  // Per-item actions. The disclosure itself is now a native <details> emitted
  // by the shared render; this only fills the bar, once, when it first opens.
  window.__pccItemOpened__ = function(p, label, li, bar){
    if(!bar || bar.dataset.built) return;
    bar.dataset.built = '1';
    buildItemBar(p, label, li, bar);
  };

  function buildItemBar(p, label, li, bar){
    // A finished item does not need a "go and build this" prompt. Handing one
    // out invites an agent to redo work that is already done, and the session
    // would open with instructions that contradict the checkbox next to them.
    var isDone = li.dataset.s === 'done';
    var prompt = itemPrompt(p, label);
    var msg = document.createElement('div'); msg.className = 'dstatus';
    function say(t, c){ msg.textContent = t || ''; msg.className = 'dstatus ' + (c||''); }

    var row = document.createElement('div'); row.className = 'dact';

    if(lnKeys.length){
      var sel = toolSelect();
      var go = document.createElement('button');
      go.className = 'pcc-btn run';
      go.textContent = isDone ? 'Open blank session here' : 'Open session on this item';
      go.title = isDone
        ? 'This item is done — opens the tool in the repo with no prompt at all'
        : 'Opens a session scoped to this one item';
      go.addEventListener('click', function(ev){
        ev.stopPropagation();
        if(isDone) launchBlank(go, p.id, sel.value, say);
        else launch(go, p.id + '-item', prompt, sel.value, say);
      });
      row.appendChild(go); row.appendChild(sel);
    }

    var cp = document.createElement('button');
    cp.className = 'pcc-btn';
    cp.textContent = isDone ? 'Copy re-check prompt' : 'Copy prompt';
    cp.title = isDone ? 'A prompt that VERIFIES this item, rather than rebuilding it' : '';
    cp.addEventListener('click', function(ev){
      ev.stopPropagation();
      var txt = isDone ? recheckPrompt(p, label) : prompt;
      copyLocal(txt).then(function(){ say('prompt copied', 'ok'); },
                          function(){ say('clipboard refused — open the prompt below', 'err'); });
    });
    row.appendChild(cp);

    // The same honest fallback the read-only surface uses: a command you paste.
    var CMD = window.__PCC_TOOLCMD__ || {};
    if(Object.keys(CMD).length){
      var cc = document.createElement('button');
      cc.className = 'pcc-btn'; cc.textContent = 'Copy command';
      cc.title = 'A shell command for YOUR machine, prompt embedded';
      cc.addEventListener('click', function(ev){
        ev.stopPropagation();
        var t = (window.__PCC_TOOL__ && CMD[window.__PCC_TOOL__]) ? window.__PCC_TOOL__
                                                                  : Object.keys(CMD)[0];
        var shell = window.__PCC_SHELL__ ||
                    (navigator.platform.indexOf('Win') === 0 ? 'powershell' : 'bash');
        var tmpl = CMD[t][shell] || CMD[t].bash;
        var cmd = tmpl.split('{repo}').join(window.__PCC_REPO__ || '.').split('{p}').join(prompt);
        copyLocal(cmd).then(function(){ say(t + ' command copied', 'ok'); },
                            function(){ say('clipboard refused', 'err'); });
      });
      row.appendChild(cc);
    }

    // Explicit state, because a checkbox that cycles on click is a secret.
    var rec = MAP[label];
    if(rec && !rec.ambiguous){
      var wrap = document.createElement('span');
      wrap.className = 'istate';
      [['todo', 'To do'], ['active', 'In progress'], ['done', 'Done']].forEach(function(s){
        var b = document.createElement('button');
        b.className = 'pcc-btn' + (li.dataset.s === s[0] ? ' on' : '');
        b.textContent = s[1];
        b.addEventListener('click', function(ev){
          ev.stopPropagation();
          if(li.dataset.s === s[0]) return;
          say('updating the plan…');
          api('/api/tick', {file: rec.file, raw: rec.raw, state: s[0]}).then(function(d){
            if(d.ok){ location.reload(); }
            else { say(d.error || 'could not update the plan', 'err'); }
          });
        });
        wrap.appendChild(b);
      });
      row.appendChild(wrap);
    }

    var det = document.createElement('details');
    var sum = document.createElement('summary'); sum.textContent = 'item prompt';
    var pre = document.createElement('pre'); pre.textContent = prompt;
    det.appendChild(sum); det.appendChild(pre);

    bar.appendChild(row); bar.appendChild(msg); bar.appendChild(det);
  }

  // ONE entry point. A phase is one object with one detail view, so there is
  // one place that fills it — this replaced three near-identical routines for
  // the drawer, the rail tree and the start-work card, each of which had to be
  // kept in step by hand.
  // The action row for one phase, built into that phase's own body when it is
  // first expanded. There used to be three callers — drawer, rail tree and
  // start-work card — rendering the same controls into three places.
  function actionRow(p, act, say, host){
    if(!act || act.dataset.wired) return;
    act.dataset.wired = '1';

    if(lnKeys.length){
      var sel = toolSelect();
      var open = document.createElement('button');
      open.className = 'pcc-btn run'; open.textContent = 'Open session';
      open.title = p.startable ? 'Open this phase in a coding session'
                               : 'This phase is blocked — the prompt says so';
      open.addEventListener('click', function(){ launch(open, p.id, p.prompt, sel.value, say); });
      act.appendChild(open); act.appendChild(sel);
    }

    if(p.test){
      var t = document.createElement('button');
      t.className = 'pcc-btn'; t.textContent = 'Test';
      t.title = 'Run the `' + p.test + '` action — this phase’s exit test';
      t.addEventListener('click', function(){ runInto(p.test, t, host, say); });
      act.appendChild(t);
    }

    var rg = document.createElement('button');
    rg.className = 'pcc-btn'; rg.textContent = 'Regenerate';
    rg.title = 'Re-read the plan and refresh this phase';
    rg.addEventListener('click', function(){
      rg.disabled = true; say('re-reading the plan…');
      fetch('/api/model').then(function(r){ return r.json(); }).then(function(m){
        rg.disabled = false;
        var np = (m.phases||[]).filter(function(x){ return String(x.id) === String(p.id); })[0];
        if(!np){ say('phase vanished from the model — reload', 'err'); return; }
        // Patching a few fields into the client model and re-rendering only the
        // drawer was wrong three ways: outside the drawer nothing changed at
        // all; it copied 6 of ~15 fields, so a phase showed fresh percentages
        // beside stale dependencies; and it never refreshed the tick write-back
        // index, so boxes redrawn afterwards pointed at source lines that had
        // moved. A reload re-derives everything from the plan — which is what
        // the button says it does. Expanded trees survive it.
        say('refreshed: ' + np.pct + '% · ' + np.done + '/' + np.total + ' — reloading', 'ok');
        try {
          var open = [];
          document.querySelectorAll('.gate[aria-expanded="true"]').forEach(function(g){
            open.push(g.getAttribute('data-tree-for'));
          });
          sessionStorage.pccOpenTrees = JSON.stringify(open);
          var tb = document.querySelector('.tab[aria-selected="true"]');
          if(tb) sessionStorage.pccTab = tb.dataset.panel;
        } catch(e){}
        location.reload();
      }, function(){ rg.disabled = false; say('could not reach the server', 'err'); });
    });
    act.appendChild(rg);

    ticketControls(p, act, say, host);
    if(!p.test) say('no test wired — set `test = "<action id>"` on this phase to run its ' +
                    'exit test from here');
  }

  // ---------------------------------------------------------- tickets -------
  // Drafting is delegated to a CODING SESSION, not done here. The session
  // already has the repo, the plan, the phase doc and the context providers,
  // and it already routes through whichever model you configured — so the
  // dashboard stays a stdlib renderer with no LLM client, no second model
  // config and no extra credential. It asks for a draft, the session writes
  // .pcc/ticket-<phase>.json, this picks it up.
  function ticketControls(p, act, say, host){
    if(p.jira) return;                       // already linked: nothing to create

    // Drafting needs a launcher that can RUN and write a file. Offering it with
    // a clipboard-only app selected produced a button that opened the app and
    // then waited forever for a draft that could never be written.
    var terms = terminalTools();
    var draft = document.createElement('button');
    draft.className = 'pcc-btn'; draft.textContent = 'Draft ticket';
    if(!terms.length){
      draft.disabled = true;
      draft.title = 'Needs a terminal launcher (claude or opencode) — none found on this machine';
    } else {
      draft.title = 'Ask the selected terminal tool to write a ticket from this phase, ' +
                    'then review it here before anything is created';
      draft.addEventListener('click', function(){
        // Resolve the tool AT CLICK TIME from the row's select, so changing it
        // takes effect. Frozen at wiring time it ignored the picker entirely.
        var sel = act.querySelector('select');
        var want = sel && sel.value;
        var dtool = (want && terms.indexOf(want) >= 0) ? want
                  : (terms.indexOf(preferredTool()) >= 0 ? preferredTool() : terms[0]);
        draft.disabled = true; say('asking ' + LN[dtool].label + ' to draft it…');
        api('/api/phase/draft-ticket', {phase: p.id, tool: dtool})
          .then(function(d){
            draft.disabled = false;
            if(!d.ok){ say(d.error, 'err'); return; }
            say(d.note || 'session started — it will write the draft; press Load draft when done');
            watchDraft(p, act, say, host);
          });
      });
    }
    act.appendChild(draft);

    var load = document.createElement('button');
    load.className = 'pcc-btn'; load.textContent = 'Load draft';
    load.title = 'Read .pcc/ticket-' + p.id + '.json if a session has written it';
    load.addEventListener('click', function(){ loadDraft(p, act, say, host, true); });
    act.appendChild(load);

    var inp = document.createElement('input');
    inp.type = 'text'; inp.placeholder = 'PROJ-123'; inp.className = 'pcc-btn';
    inp.style.width = '110px'; inp.style.cursor = 'text';
    var lk = document.createElement('button');
    lk.className = 'pcc-btn'; lk.textContent = 'Link ticket';
    lk.title = 'Record an EXISTING ticket key on this phase (docs/progress.toml)';
    function doLink(){
      if(!inp.value.trim()){
        say('type the key of a ticket that already exists (e.g. PROJ-123), or ' +
            'use Draft ticket to create one', 'err');
        inp.focus(); return;
      }
      lk.disabled = true;
      api('/api/phase/jira', {phase: p.id, key: inp.value}).then(function(d){
        lk.disabled = false;
        if(!d.ok){ say(d.error, 'err'); return; }
        say('linked ' + d.key + ' — reload to see the pill everywhere', 'ok');
        window.__PCC_PHASES__[p.id].jira = d.key;
      });
    }
    lk.addEventListener('click', doLink);
    inp.addEventListener('keydown', function(ev){ if(ev.key === 'Enter') doLink(); });
    act.appendChild(inp); act.appendChild(lk);

    loadDraft(p, act, say, host, false, function(found){
      if(!found) resumeWatch(p, act, say, host);   // a reload mid-drafting resumes
    });
  }

  // Watch for the draft the session is writing. Recorded in sessionStorage so a
  // reload mid-drafting resumes the watch instead of leaving you to remember to
  // press Load draft — the session takes minutes, and nobody sits on the page.
  function watchDraft(p, act, say, host){
    try { sessionStorage['pccDraftWatch:' + p.id] = String(Date.now()); } catch(e){}
    pollDraft(p, act, say, host);
  }
  function pollDraft(p, act, say, host){
    var tries = 0, iv = 4000;
    (function poll(){
      if(++tries > 300) { clearWatch(p); return; }     // ~20 min, then give up
      setTimeout(function(){
        loadDraft(p, act, say, host, false, function(found){
          if(found){ clearWatch(p); say('draft picked up automatically — review it below', 'ok'); }
          else poll();
        });
      }, iv);
    })();
  }
  function clearWatch(p){
    try { delete sessionStorage['pccDraftWatch:' + p.id]; } catch(e){}
  }
  function resumeWatch(p, act, say, host){
    var started;
    try { started = sessionStorage['pccDraftWatch:' + p.id]; } catch(e){}
    if(!started) return;
    if(Date.now() - Number(started) > 30*60*1000){ clearWatch(p); return; }
    say('a draft was requested for this phase — watching for it');
    pollDraft(p, act, say, host);
  }

  function loadDraft(p, act, say, host, loud, cb){
    api('/api/phase/ticket-draft', {phase: p.id}).then(function(d){
      if(!d.ok || !d.draft){
        if(loud) say(d.error || ('no draft yet at ' + (d.path || '.pcc/ticket-' + p.id + '.json')), 'err');
        if(cb) cb(false);
        return;
      }
      if(cb) cb(true);
      showDraft(p, act, say, host, d.draft, d.jira);
    });
  }

  // The draft is EDITABLE and nothing is created until you press the button.
  // A ticket is outward-facing: a model wrote the words, a person sends them.
  function showDraft(p, act, say, host, draft, jira){
    var box = (host || act.parentNode).querySelector('.tdraft');
    if(!box){
      box = document.createElement('div'); box.className = 'tdraft';
      (act.nextSibling ? act.parentNode.insertBefore(box, act.nextSibling.nextSibling)
                       : act.parentNode.appendChild(box));
    }
    box.innerHTML = '';
    // Real labels: these two fields become a ticket someone else reads, and an
    // unlabelled input announces only its current value.
    var sid = 'tsum-' + p.id, bid = 'tbody-' + p.id;
    var sl = document.createElement('label'); sl.htmlFor = sid; sl.textContent = 'Summary';
    var s = document.createElement('input');
    s.type = 'text'; s.className = 'tsummary'; s.id = sid; s.value = draft.summary || '';
    var bl = document.createElement('label'); bl.htmlFor = bid; bl.textContent = 'Description';
    var b = document.createElement('textarea');
    b.className = 'tbody'; b.rows = 12; b.id = bid; b.value = draft.description || '';
    var why = document.createElement('div'); why.className = 'dstatus';
    var row = document.createElement('div'); row.className = 'dact';

    // Route 1 — credential-free. Opens JIRA prefilled; you press Create there.
    // The TEMPLATE, not the pre-filled URL: jira_create already had its
    // placeholders substituted server-side, so replacing into it does nothing
    // and would quietly send the generic phase text instead of your draft.
    var tmpl = p.jira_create_tmpl || '';
    if(tmpl){
      var open = document.createElement('button');
      open.className = 'pcc-btn'; open.textContent = 'Open prefilled JIRA form';
      open.title = 'Opens JIRA with these fields; you press Create there. No token used.';
      open.addEventListener('click', function(){
        window.open(tmpl.split('{summary}').join(encodeURIComponent(s.value))
                        .split('{description}').join(encodeURIComponent(b.value)),
                    '_blank', 'noopener');
        say('JIRA opened with your draft — after creating it, paste the key into Link ticket');
      });
      row.appendChild(open);
    }

    // Route 2 — create it directly. Outward-facing and not undoable, so it is a
    // TWO-STEP: the first click only arms the button, and makes it name the
    // project the issue will actually land in.
    var apiBtn = document.createElement('button');
    apiBtn.className = 'pcc-btn run';
    var armed = false;
    if(jira && jira.configured){
      apiBtn.textContent = 'Create in JIRA…';
      apiBtn.title = 'Creates the issue over the API using ' + jira.auth_env;
      apiBtn.addEventListener('click', function(){
        if(!armed){
          armed = true;
          apiBtn.textContent = 'Confirm: create in ' + jira.project;
          apiBtn.style.background = 'var(--crit)'; apiBtn.style.borderColor = 'var(--crit)';
          say('this creates a real issue in ' + jira.project + ' at ' + jira.base + ' as a ' +
              jira.issue_type + '. Click again to confirm, or edit the text first.', 'err');
          return;
        }
        apiBtn.disabled = true; apiBtn.textContent = 'Creating…';
        api('/api/phase/create-ticket',
            {phase: p.id, summary: s.value, description: b.value}).then(function(d){
          if(!d.ok){
            apiBtn.disabled = false; armed = false;
            apiBtn.textContent = 'Create in JIRA…';
            apiBtn.style.background = ''; apiBtn.style.borderColor = '';
            say(d.error, 'err'); return;
          }
          box.innerHTML = '';
          var done = document.createElement('div'); done.className = 'dstatus ok';
          done.innerHTML = 'Created <b>' + d.key + '</b> and recorded it on this phase' +
            (d.linked ? '' : ' (writing it to progress.toml failed: ' + d.link_error + ')') +
            ' — <a href="' + d.url + '" target="_blank" rel="noopener">open it ↗</a>';
          box.appendChild(done);
          window.__PCC_PHASES__[p.id].jira = d.key;
          setTimeout(function(){ location.reload(); }, 2500);
        });
      });
    } else {
      apiBtn.textContent = 'Create in JIRA';
      apiBtn.disabled = true;
      apiBtn.title = 'Needs API configuration';
    }
    row.appendChild(apiBtn);

    why.innerHTML = (jira && jira.configured)
      ? 'Two routes. <b>Open prefilled JIRA form</b> uses your browser session and no token. ' +
        '<b>Create in JIRA</b> posts to <code>' + jira.base + '</code> as <b>' + jira.project +
        ' / ' + jira.issue_type + '</b> using <code>' + jira.auth_env + '</code>, then records ' +
        'the key here. Two clicks, because a ticket cannot be un-created.' +
        (jira.insecure ? ' <b>api_base is plain http — the token would cross the network ' +
                         'unencrypted.</b>' : '')
      : (tmpl ? 'Opens JIRA with these fields filled in; you press Create there. ' : '') +
        'Direct creation is off: ' +
        (((jira && jira.missing) || []).join('; ') || 'no JIRA API configured') +
        '. Set it on <a href="/setup">/setup</a> → This project.';

    var n = (draft.description || '').length;
    var head = el('h4', 'DRAFT TICKET  ·  ' + n + ' chars');
    if(n > 2200){
      head.textContent += '  ·  long for a ticket — trim before sending';
      head.style.color = 'var(--warn)';
    }
    box.appendChild(head);
    box.appendChild(sl); box.appendChild(s);
    box.appendChild(bl); box.appendChild(b);
    box.appendChild(row); box.appendChild(why);
  }
  function el(tag, text){ var n = document.createElement(tag); n.textContent = text; return n; }

  window.__pccPhaseOpened__ = function(p, det){
    var act = det.querySelector('.dact');
    var msg = det.querySelector('.pbody > .dstatus');
    if(!act || !msg) return;
    function say(t, c){ msg.textContent = t || ''; msg.className = 'dstatus ' + (c||''); }
    actionRow(p, act, say, det);
    wireTicks(det);

    // Work tree: what git says actually happened under this phase's modules.
    var box = det.querySelector('.pactivity');
    if(box){
      box.innerHTML = '<span class="quiet">loading activity…</span>';
      api('/api/phase/activity', {phase: p.id}).then(function(d){
        if(!d.ok){ box.innerHTML = '<span class="dstatus err">'+esc(d.error)+'</span>'; return; }
        if(d.note){ box.innerHTML = '<span class="quiet">'+esc(d.note)+'</span>'; return; }
        var rows = d.commits.map(function(c){
          return '<div><span class="num quiet">'+esc(c.date)+'  '+esc(c.sha)+'</span>  '+
                 esc(c.subject)+'</div>'; }).join('');
        box.innerHTML =
          '<span class="quiet">'+d.commits.length+' commit(s) touching '+d.paths.length+' path(s)</span>'+
          (rows ? '<div class="dout">'+rows+'</div>' : '') +
          (d.stat ? '<span class="quiet">uncommitted:</span><div class="dout">'+esc(d.stat)+'</div>' : '');
      });
    }
  };
  function esc(s){ var n=document.createElement('div'); n.textContent = s==null?'':s; return n.innerHTML; }

  // Catch up: this script loads AFTER the shared one, so a phase restored open
  // on page load fired its toggle before this layer existed. Fill those now.
  if(window.__pccFillOpenPhases__) window.__pccFillOpenPhases__();
  document.querySelectorAll('details.idet[open]').forEach(function(d){
    if(d.dataset.filled) return;
    d.dataset.filled = '1';
    var li = d.closest('.item'), ph = d.closest('details.phase');
    var P = window.__PCC_PHASES__ || {};
    if(ph && P[ph.getAttribute('data-phase')]){
      window.__pccItemOpened__(P[ph.getAttribute('data-phase')],
                               li.getAttribute('data-item'), li, d.querySelector('.ibar'));
    }
  });

  // Say which surface this is. Without it a stale published snapshot and the
  // live dashboard are pixel-identical, and the only visible difference is that
  // buttons are "missing" — which reads as a bug, not as a different page.
  (function(){
    var b = document.getElementById('surface-badge');
    if(!b) return;
    b.textContent = 'live · actions enabled';
    b.className = 'pill done';
    b.title = 'Local dashboard at ' + location.host + ' — Run, Test and Open session work here.';
  })();

  // This script is appended AFTER the shared one, so on a large page there is a
  // window where phases are already clickable but their actions do not exist yet.
  // Clicking in it produced a drawer with an empty action row and no hint that
  // anything was missing — so re-render whatever is already open.

  // Run an allowlisted action and stream it into the drawer.
  function runInto(task, btn, body, say){
    // Every phase body carries a .dact; refuse rather than throw if it is absent.
    var anchor = body && body.querySelector('.dact');
    if(!anchor){ say('cannot show output here — reload the page', 'err'); return; }
    btn.disabled = true; say('running ' + task + '…');
    var out = body.querySelector('.drun');
    if(!out){ out = document.createElement('div'); out.className = 'dout drun';
      anchor.insertAdjacentElement('afterend', out); }
    out.textContent = '';
    api('/api/run', {task: task}).catch(function(){
      btn.disabled = false; say('could not reach the server', 'err');
      return {};
    }).then(function(d){
      if(!d.run_id){ btn.disabled = false; say(d.error || 'refused', 'err'); return; }
      (function poll(){
        fetch('/api/run/' + d.run_id).then(function(r){ return r.json(); }).then(function(s){
          out.textContent = (s.lines||[]).join('\n');
          out.scrollTop = out.scrollHeight;
          if(!s.done){ setTimeout(poll, 700); return; }
          btn.disabled = false;
          say(s.rc === 0 ? 'passed (rc 0)' : 'failed (rc ' + s.rc + ')', s.rc === 0 ? 'ok' : 'err');
        });
      })();
    });
  }

  // Clicking a box rewrites the plan file — which is what the report derives
  // from — so the dashboard edits the source of truth instead of shadowing it.
  var NEXT = {todo:'done', done:'active', active:'todo'};
  // Callable on any subtree, not just once at load. The phase drawer builds its
  // checklist fresh on every open, so a load-time binding left those boxes
  // looking exactly like the tickable ones on the page while doing nothing —
  // the same "identical appearance, different behaviour" trap as the surfaces.
  // The tick is now a real <button> the shared render emits, with an accessible
  // name that states the item and what pressing it does. It used to be a <span>
  // whose only affordance was a title tooltip — invisible to keyboard and touch.
  function wireTicks(root){
    (root || document).querySelectorAll('li.item .tick').forEach(function(btn){
      var li = btn.closest('li.item');
      if(!li || li.getAttribute('data-pcc')) return;
      var lbl = li.querySelector('.lbl');
      if(!lbl) return;
      var rec = MAP[lbl.textContent.trim()];
      if(!rec || rec.ambiguous){ btn.disabled = true;
        btn.setAttribute('aria-label', 'Cannot change: this line appears twice in the plan');
        return; }
      li.setAttribute('data-pcc','1');
      btn.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        if(li.classList.contains('busy')) return;
        li.classList.add('busy'); btn.disabled = true;
        api('/api/tick', {file: rec.file, raw: rec.raw,
                          state: btn.dataset.next || NEXT[li.dataset.s] || 'done'})
          .catch(function(){ return {ok:false, error:'could not reach the server'}; })
          .then(function(d){
            if(d.ok){ location.reload(); }
            else { li.classList.remove('busy'); btn.disabled = false;
                   alert(d.error || 'tick failed'); }
          });
      });
    });
  }
  wireTicks(document);
  window.__pccWireTicks__ = wireTicks;
})();
"""


def action_layer(token: str, model: dict) -> str:
    """Everything the local build adds on top of the shared render()."""
    # label -> source line, so a click can find its way back into the markdown.
    idx: dict[str, dict] = {}
    for ph in model["phases"]:
        for it in ph.get("items", []):
            if not it.get("file"):
                continue
            k = it["label"]
            if k in idx and idx[k]["raw"] != it["raw"]:
                idx[k]["ambiguous"] = True      # same text twice: refuse rather than guess
            else:
                idx.setdefault(k, {"file": it["file"], "raw": it["raw"]})

    buttons = "".join(
        '<button class="pcc-btn{cls}" data-task="{k}" data-label="{lab}" title="{hint}">{lab}</button>'.format(
            cls=" run" if v["primary"] else "", k=k, lab=v["label"], hint=v["hint"])
        for k, v in ACTIONS.items())

    return (
        "<style>" + CSS + "</style>"
        '<div id="pcc-out"><header><h4></h4><span class="rc"></span>'
        '<button class="pcc-btn x">close</button></header><pre></pre></div>'
        '<div id="pcc-svc"></div>'
        '<div id="pcc-bar"><span class="lbl">local</span>' + buttons +
        '<a class="pcc-btn" href="/setup" style="text-decoration:none;margin-left:auto"'
        ' title="configure this machine and this project">Setup</a>'
        '<span class="pcc-local">actions live</span></div>'
        "<script>window.__ANU_TOKEN__=" + _pr.js(token) + ";"
        "window.__ANU_ITEMS__=" + _pr.js(idx) + ";"
        "window.__ANU_PROFILE_TOOL__=" + _pr.js(
            (_pr.load_user_profile() or {}).get("tool", "")) + ";"
        "window.__ANU_LAUNCHERS__=" + _pr.js(
            {k: {"label": v["label"], "mode": v.get("mode", "")}
             for k, v in LAUNCHERS.items()}) + ";</script>"
        "<script>" + JS + "</script>")


# ------------------------------------------------------------ setup wizard ---
# The browser front end for the two CLI wizards (--setup / --discover). Same
# engine, same rules; the difference is that every assumption those wizards
# would default silently is rendered here WITH ITS EVIDENCE and a switch.
#
# What it deliberately cannot do: write [[action]] or [[launcher]]. Those name
# executables that this server later runs, so they stay a deliberate edit to a
# file you own, gated by the trust prompt at the next start. A form post is the
# wrong authority for "here is a new command to run".

def theme_tokens() -> str:
    """Just the palette from the report's stylesheet — the reset and the four
    :root token blocks, stopping before the first component rule.

    Importing all of CSS looked tidy and was wrong: the report styles `.bar` as a
    5px progress bar with overflow:hidden, so the wizard's button rows silently
    collapsed to a sliver. Shared TOKENS, separate COMPONENTS.
    """
    head = _pr.CSS.split("body{", 1)[0]
    return head if "--accent" in head else _pr.CSS


SETUP_CSS = """
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.55;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.sw{max-width:960px;margin:0 auto;padding:28px 22px 80px}
.sw h1{font-size:22px;letter-spacing:-.01em;margin:0 0 4px}
.sw .sub{color:var(--ink-3);font-size:13px;font-family:var(--mono);margin-bottom:20px}
.sw .sub a{color:var(--accent);text-decoration:none}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:22px}
.tabs button{appearance:none;background:none;border:0;border-bottom:2px solid transparent;
  padding:9px 14px;font:inherit;font-size:14px;color:var(--ink-3);cursor:pointer}
.tabs button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.pane{display:none}.pane.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow);padding:18px 20px;margin-bottom:16px}
.card > h2{font-size:14px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);
  margin:0 0 4px;font-family:var(--mono)}
.card > .note{font-size:13px;color:var(--ink-2);margin:0 0 14px}
.card > .note code{font-family:var(--mono);font-size:12px;background:var(--panel-2);
  padding:1px 5px;border-radius:4px;word-break:break-all}
.row{display:grid;grid-template-columns:26px 190px 1fr;gap:12px;align-items:start;
  padding:11px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.row.off > *:not(:first-child){opacity:.4}
.row label.k{font-size:14px;padding-top:5px}
/* a divider inside a card: these rows are one optional feature, not more of the
   same list, and running them together made the card read as endless */
.subhead{margin:22px 0 2px;padding-top:16px;border-top:1px solid var(--line);
  font-size:14px;font-weight:600}
.subhead .quiet{font-weight:400;color:var(--ink-3);font-size:12.5px}
.advfold{margin:4px 0 0;border-top:1px solid var(--line)}
.advfold > summary{cursor:pointer;font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-3);padding:10px 0;min-height:24px;
  display:flex;align-items:center}
.advfold > summary:hover{color:var(--accent)}
.derived{padding:10px 0 2px;line-height:1.7}
.ready{margin-top:6px;padding:6px 9px;border-radius:6px;line-height:1.55}
.ready.yes{background:var(--done-soft);color:var(--done)}
.ready.no{background:var(--warn-soft);color:var(--warn)}
.derived code{background:var(--panel-2);padding:1px 5px;border-radius:4px;
  font-family:var(--mono);font-size:11.5px}
.row .v input[type=text],.row .v input[type=date],.row .v input[type=password],.row .v select{
  width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;
  background:var(--bg);color:var(--ink);font:inherit;font-size:13.5px}
.row .v input:disabled,.row .v select:disabled{cursor:not-allowed}
.why{font-size:12px;color:var(--ink-3);margin-top:5px;font-family:var(--mono);
  line-height:1.5;word-break:break-word}
.why b{color:var(--ink-2);font-weight:600}
.sw input[type=checkbox]{width:16px;height:16px;margin-top:8px;accent-color:var(--accent)}
.chip{display:inline-block;font-family:var(--mono);font-size:11px;padding:1px 7px;
  border-radius:999px;border:1px solid var(--line);color:var(--ink-3);vertical-align:1px}
.chip.up{background:var(--done-soft);color:var(--done);border-color:transparent}
.chip.down{background:var(--todo-soft);color:var(--todo);border-color:transparent}
.chip.have{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.picker{display:inline-block}
.pbrowse{margin-top:8px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel-2);overflow:hidden;max-width:640px}
.phere{padding:8px 11px;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);
  border-bottom:1px solid var(--line);word-break:break-all;background:var(--panel)}
.plist{max-height:260px;overflow:auto;padding:4px}
.pent{display:block;width:100%;text-align:left;appearance:none;border:0;background:none;
  color:var(--ink);font:inherit;font-size:13px;padding:6px 9px;border-radius:6px;
  cursor:pointer;min-height:24px}
.pent:hover{background:var(--accent-soft);color:var(--accent)}
.pent.pup{color:var(--ink-3)}
.pfile{font-family:var(--mono);font-size:12px}
.pempty,.perr{padding:10px 11px;font-size:12.5px;color:var(--ink-3)}
.perr{color:var(--crit)}
.pbar{display:flex;gap:6px;flex-wrap:wrap;padding:8px;border-top:1px solid var(--line)}
.quietbtn{font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.planboxes.nobox{color:var(--warn);background:var(--warn-soft);
  padding:7px 10px;border-radius:7px;margin-top:6px}
.row.stranded{background:var(--warn-soft);border-radius:8px;
  padding:8px 10px;margin-top:6px;align-items:start}
.row.stranded .k{color:var(--warn)}
.row.stranded code{font-size:.92em;word-break:break-all}
.chip.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.sw button.act{appearance:none;font:inherit;font-size:13px;padding:7px 14px;border-radius:8px;
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink);cursor:pointer}
.sw button.act:hover{border-color:var(--accent);color:var(--accent)}
.sw button.act.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.sw button.act.pri:hover{filter:brightness(1.08);color:#fff}
.sw button.act[disabled]{opacity:.5;cursor:not-allowed}
/* Save sticks to the bottom of the viewport: the fields it saves are a long
   scroll above it, and a save button you have to go looking for is one people
   assume is missing. */
.sw .card.sticky-save{position:sticky;bottom:0;z-index:5;margin-top:18px;
  border-color:var(--accent-soft);box-shadow:0 -8px 24px -18px rgba(0,0,0,.4),var(--shadow)}
.sw .card.sticky-save.dirty{border-color:var(--accent)}
.sw .card.sticky-save.dirty .msg{color:var(--accent);font-weight:600}
/* armed = the next click writes. Colour alone would not say that, so the label
   changes too. */
.sw button.act.warnbtn{background:var(--crit);border-color:var(--crit);color:#fff}
.sw button.act.warnbtn:hover{filter:brightness(1.08);color:#fff}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px}
.bar .msg{font-size:13px;color:var(--ink-2)}
.bar .msg.err{color:var(--crit)}.bar .msg.ok{color:var(--done)}
pre.diff{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;overflow:auto;max-height:340px;font-family:var(--mono);
  font-size:12px;line-height:1.55;margin:14px 0 0;white-space:pre}
pre.diff .a{color:var(--done)}pre.diff .d{color:var(--crit)}pre.diff .h{color:var(--ink-3)}
table.svc{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
table.svc col.c0{width:30px}table.svc col.c1{width:23%}table.svc col.c2{width:78px}
table.svc col.c3{width:auto}table.svc col.c4{width:150px}table.svc col.c5{width:120px}
table.svc th{text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-3);font-weight:500;padding:0 8px 8px 0}
table.svc td{padding:7px 8px 7px 0;border-top:1px solid var(--line);vertical-align:middle}
table.svc input[type=text]{width:100%;padding:5px 8px;border:1px solid var(--line);
  border-radius:6px;background:var(--bg);color:var(--ink);font:inherit;font-size:12.5px;
  font-family:var(--mono)}
table.svc tr.off td:not(:first-child){opacity:.45}
.mono{font-family:var(--mono);font-size:12px}
.tool-list{font-family:var(--mono);font-size:12px;color:var(--ink-2);line-height:1.8}
.tool-list .p{color:var(--ink-3)}
"""

SETUP_JS = r"""
(function(){
  var T = window.__SW_TOKEN__, E = null;
  function $(s,r){return (r||document).querySelector(s)}
  function el(t,a,kids){var n=document.createElement(t);a=a||{};
    for(var k in a){ if(k==='text') n.textContent=a[k]; else if(k==='html') n.innerHTML=a[k];
      else n.setAttribute(k,a[k]); }
    (kids||[]).forEach(function(c){n.appendChild(c)}); return n}
  function api(p,b){return fetch(p,{method:'POST',headers:{'Content-Type':'application/json',
    'X-PCC-Token':T},body:JSON.stringify(b||{})}).then(function(r){return r.json()})}
  function say(sel,msg,cls){var m=$(sel); m.textContent=msg||''; m.className='msg '+(cls||'')}

  // A row = [enable] [label] [control + why]. The switch decides whether the
  // value is SENT, so unticking is "leave whatever is there alone", never "erase".
  function row(key,label,ctl,why,on){
    var cb=el('input',{type:'checkbox'}); cb.checked=on!==false; cb.dataset.k=key;
    var v=el('div',{class:'v'}); v.appendChild(ctl);
    if(why) v.appendChild(el('div',{class:'why',html:why}));
    var r=el('div',{class:'row'+(cb.checked?'':' off')},[cb,el('label',{class:'k',text:label}),v]);
    // Typing IS the intent to set a value. A row starts unticked when its field
    // is empty, and only ticked rows are sent — so without this, filling in a
    // blank field and pressing write silently discarded what you typed.
    v.addEventListener('input',function(){
      if(cb.checked) return;
      cb.checked = true;
      r.classList.remove('off');
    });
    cb.addEventListener('change',function(){
      r.classList.toggle('off',!cb.checked);
      v.querySelectorAll('input,select').forEach(function(i){i.disabled=!cb.checked});
    });
    return r;
  }
  function inp(id,val,ph,type){var i=el('input',{type:type||'text',id:id});
    i.value=val==null?'':val; if(ph)i.placeholder=ph; return i}
  function sel(id,val,opts){var s=el('select',{id:id});
    opts.forEach(function(o){var t=typeof o==='string'?o:o.v, lab=typeof o==='string'?o:o.l;
      var op=el('option',{value:t,text:lab}); if(t===val)op.selected=true; s.appendChild(op)});
    return s}
  function val(id){var n=$('#'+id); return n?n.value.trim():''}
  function esc(x){var n=document.createElement('div'); n.textContent=x==null?'':x;
    return n.innerHTML}
  // Scope matters: key 'name' exists in BOTH panes ("Your name" / "Project
  // name"), and a document-wide lookup returns whichever is first in the DOM
  // \u00b7 the machine tab. Unscoped, unticking "Your name" silently dropped
  // the project name from the config save.
  function on(key,scope){
    var r=scope?$(scope):document;
    var c=r?r.querySelector('input[data-k="'+key+'"]'):null; return !c||c.checked}

  function diff(pre,text){
    pre.textContent='';
    if(!text){pre.style.display='none';return}
    pre.style.display='block';
    text.split('\n').forEach(function(l){
      var c=l[0]==='+'?'a':(l[0]==='-'?'d':(l[0]==='@'?'h':''));
      pre.appendChild(el('span',{class:c,text:l+'\n'}));
    });
  }

  // ---------------------------------------------------------------- local ---
  // Evidence line. A saved answer is labelled as saved AND still shows what was
  // detected, so you can always see the assumption you are overriding.
  function why(a,extra,savedAs){
    // savedAs names what the value was saved FOR. "saved in your profile"
    // reads as machine-wide; "saved for <project>" is the word that carries the
    // per-project scope the user could not otherwise see.
    return (a.saved?'<b>saved '+(savedAs||'in your profile')+'</b> \u00b7 detected: '
                   :'assumed from: ')+
           a.why+(extra?' \u00b7 '+extra:'');
  }

  // --------------------------------------------------------------- switch ---
  function renderProjects(){
    var tb = $('#sw-projects tbody'); tb.textContent = '';
    $('#sw-preg').textContent = E.project_registry || '';
    var ps = E.projects || [];
    if(!ps.length){
      tb.appendChild(el('tr',{},[el('td',{colspan:'4',
        text:'No projects recorded yet \u2014 open one by path below.'})]));
      return;
    }
    ps.forEach(function(p){
      // State is resolved server-side BEFORE you click: a checkout that has been
      // moved or deleted says so, and an unapproved one is labelled read-only
      // here rather than surprising you with missing buttons afterwards.
      var LABEL = {
        missing:      ['chip warn', 'path missing',    'the checkout has moved or been deleted'],
        unconfigured: ['chip',      'not initialized', 'no docs/progress.toml — Init it after switching'],
        broken:       ['chip crit', 'config broken',   'docs/progress.toml does not parse'],
        'read-only':  ['chip warn', 'read-only',       'its commands have never been approved on this machine, or have changed since'],
        ready:        ['chip up',   'ready',           'commands approved — Run and Test will work']
      }[p.state] || ['chip', p.state, ''];
      var state = el('span',{class:LABEL[0],text:LABEL[1]});
      state.title = LABEL[2];

      var right = el('div',{class:'bar'});
      if(p.current){
        right.appendChild(el('span',{class:'chip have',text:'current'}));
      } else {
        var go = el('button',{class:'act pri',text:'Switch'});
        if(!p.exists) go.disabled = true;
        go.addEventListener('click',function(){
          go.disabled = true; say('#sw-pmsg2','switching\u2026');
          api('/api/setup/switch',{path:p.path}).then(function(d){
            go.disabled = false;
            if(!d.ok){ say('#sw-pmsg2',d.error,'err'); return; }
            say('#sw-pmsg2','now serving ' + d.name + (d.trusted ? '' :
                ' \u2014 read-only: ' + (d.blocked.join(', ') || 'its commands') +
                ' need approval at a restart'), d.trusted ? 'ok' : '');
            load();
          });
        });
        right.appendChild(go);
      }
      var drop = el('button',{class:'act',text:'Forget'});
      drop.title = 'Remove from this list. Does not touch the project itself.';
      drop.disabled = !!p.current;
      drop.addEventListener('click',function(){
        api('/api/setup/forget-project',{path:p.path}).then(function(d){
          if(d.ok) load(); else say('#sw-pmsg2',d.error,'err');
        });
      });
      right.appendChild(drop);

      var tr = el('tr',{},[
        el('td',{},[el('div',{text:p.name || '(unnamed)'}),
                    el('div',{class:'why',text:p.last_opened ? 'last opened ' + p.last_opened : ''})]),
        el('td',{},[el('div',{class:'mono',text:p.path})]),
        el('td',{},[state]),
        el('td',{},[right])]);
      if(p.current) tr.style.background = 'var(--accent-soft)';
      tb.appendChild(tr);
    });
  }

  function renderLocal(){
    var L=E.local, box=$('#sw-local'); box.textContent='';
    box.appendChild(row('name','Your name',inp('f-name',L.name.value,'roster name'),
      why(L.name,'must match a [[developer]] name for the "only my phases" filter')));
    box.appendChild(row('tool','Preferred tool',sel('f-tool',L.tool.value,L.tool.options),
      why(L.tool)));
    box.appendChild(row('shell','Shell',sel('f-shell',L.shell.value,L.shell.options),
      why(L.shell,'decides the prompt-transport syntax (here-string vs heredoc)')));

    var tl=$('#sw-tools'); tl.textContent='';
    var names=Object.keys(E.tools);
    if(!names.length) tl.appendChild(el('div',{text:'nothing detected on PATH \u2014 prompts stay copyable'}));
    names.forEach(function(k){ tl.appendChild(el('div',{},[
      el('span',{text:k+'  '}), el('span',{class:'p',text:E.tools[k]})])) });

  }

  // Personal, but scoped to THIS project: the value lives in your profile's
  // [repos] map keyed by this repo's path, so switching projects switches it.
  // Its own card and its own Save on purpose \u00b7 the sticky "Save config"
  // below writes docs/progress.toml, which must never carry a personal path.
  // A separate container, not an append into #sw-proj: renderProject() clears
  // that box on every load(), so anything another renderer put there is wiped.
  // A path picker. The page cannot see the filesystem and a native file input
  // withholds real paths on purpose, so naming a directory meant typing it from
  // memory. The server is on this machine and answers /api/setup/browse.
  //   target : the <input> whose value it sets
  //   want   : 'dir' to pick a folder, 'md' to pick a markdown file
  //   rel    : if set, store the path relative to this root (the plan file is
  //            repo-relative in config; the checkout is absolute)
  function pathPicker(target, want, rel){
    var wrap = el('div',{class:'picker'});
    var btn  = el('button',{class:'act',text:'Browse\u2026'});
    var panel= el('div',{class:'pbrowse',style:'display:none'});
    wrap.appendChild(btn); wrap.appendChild(panel);
    var open = false;

    function relTo(root, p){
      if(!root) return p;
      var r = root.replace(/[\\/]+$/,'');
      if(p.toLowerCase().indexOf(r.toLowerCase()+'\\')===0 ||
         p.toLowerCase().indexOf(r.toLowerCase()+'/')===0){
        return p.slice(r.length+1).split('\\').join('/');
      }
      return p;                       // outside the root: keep it absolute and honest
    }

    function draw(d){
      panel.textContent='';
      if(!d || !d.ok){
        panel.appendChild(el('div',{class:'perr',text:(d&&d.error)||'could not read that folder'}));
        return;
      }
      panel.appendChild(el('div',{class:'phere',text:d.path}));
      var list = el('div',{class:'plist'});
      if(d.parent){
        var up = el('button',{class:'pent pup',text:'\u2191  ..'});
        up.addEventListener('click',function(){ go(d.parent) });
        list.appendChild(up);
      }
      (d.dirs||[]).forEach(function(x){
        var b = el('button',{class:'pent pdir',text:'\u{1F4C1}  '+x.name});
        b.addEventListener('click',function(){ go(x.path) });
        list.appendChild(b);
      });
      (d.files||[]).forEach(function(x){
        var b = el('button',{class:'pent pfile',text:'\u{1F4C4}  '+x.name});
        b.addEventListener('click',function(){ choose(x.path) });
        list.appendChild(b);
      });
      if(!(d.dirs||[]).length && !(d.files||[]).length){
        list.appendChild(el('div',{class:'pempty',text:want==='md'
          ? 'no sub-folders and no .md files here' : 'no sub-folders here'}));
      }
      panel.appendChild(list);
      var bar = el('div',{class:'pbar'});
      if(want==='dir'){
        var use = el('button',{class:'act pri',text:'Use this folder'});
        use.addEventListener('click',function(){ choose(d.path) });
        bar.appendChild(use);
      }
      (d.roots||[]).slice(0,6).forEach(function(r){
        var j = el('button',{class:'act quietbtn',text:r});
        j.addEventListener('click',function(){ go(r) });
        bar.appendChild(j);
      });
      panel.appendChild(bar);
    }

    function go(p){
      api('/api/setup/browse',{path:p,want:want}).then(draw);
    }
    function choose(p){
      var v = relTo(rel, p);
      // A <select> silently ignores a value with no matching option, so browsing
      // to a file outside the shortlist left the field EMPTY - the one outcome
      // worse than not offering the browser at all.
      if(target.tagName === 'SELECT' &&
         ![].some.call(target.options, function(o){ return o.value === v })){
        var op = document.createElement('option');
        op.value = v; op.textContent = v + '   (browsed)';
        target.insertBefore(op, target.firstChild);
      }
      target.value = v;
      target.dispatchEvent(new Event('input',{bubbles:true}));
      panel.style.display='none'; open=false; btn.textContent='Browse\u2026';
    }
    btn.addEventListener('click',function(){
      open = !open;
      panel.style.display = open?'block':'none';
      btn.textContent = open?'Close':'Browse\u2026';
      if(open) go(target.value || rel || '');
    });
    return wrap;
  }

  function renderMine(){
    var L=E.local, box=$('#sw-mine'); if(!box) return; box.textContent='';
    var repoIn = inp('f-repo',L.repo_path.value,'C:\\src\\project');
    box.appendChild(row('repo_path','Your checkout',
      el('div',{},[repoIn, pathPicker(repoIn,'dir','')]),
      why(L.repo_path,'switching projects switches this. Stored in your profile, '+
        'outside this repo, so it is never committed and never published. On a '+
        'shared dashboard it describes the machine running the server, which may '+
        'not be yours.', 'for '+esc(E.project.name||E.repo))));
  }

  function renderSecrets(){
    var sb=$('#sw-secrets'); if(!sb) return;
    sb.textContent='';
    $('#sw-secpath').textContent = E.context_env_path || '';
    var set = E.context_secrets_set || [];
    // The JIRA row follows whatever auth_env the config names, so renaming the
    // variable does not orphan the token you already stored.
    var jiraVar = ((E.project.jira_api||{}).auth_env) || 'JIRA_PAT';
    var seen = {};
    [[jiraVar,'JIRA — only needed to create issues over the API'],
     ['GIT_PAT','git / forge PAT, for sessions that push']].forEach(function(s){
      if(seen[s[0]]) return; seen[s[0]]=1;
      mkSecret(sb,s[0],s[1],set.indexOf(s[0])>=0);
    });
    (E.project.contexts||[]).forEach(function(c){
      if(c.auth_env && !seen[c.auth_env]){ seen[c.auth_env]=1;
        mkSecret(sb,c.auth_env,'token for provider "'+c.name+'"',set.indexOf(c.auth_env)>=0); }
    });
    strandedRow(sb);
  }
  // Tokens used to live in a user-level file. Moving the store here would leave
  // any of those reading as "not set" with nothing saying a value still exists
  // somewhere, so say so, and offer to move it rather than make you find it.
  function strandedRow(box){
    var L = E.legacy_secrets || {}, vars = (L.vars||[]).filter(function(v){
      return (E.context_secrets_set||[]).indexOf(v) < 0; });
    if(!vars.length) return;
    var msg=el('span',{class:'msg'});
    var move=el('button',{class:'act primary',text:'Move '+(vars.length>1?vars.length+' tokens':vars[0])+' here'});
    var row=el('div',{class:'row stranded'},[el('span',{}),
      el('label',{class:'k',text:'left behind'}),
      el('div',{class:'v'},[el('div',{class:'bar'},[move,msg]),
        el('div',{class:'why',html:esc(vars.join(', '))+' still sits in the old user-level store'+
          ' (<code>'+esc(L.path||'')+'</code>), which nothing reads any more.'+
          ' Moving writes the value here first and only then drops the original.'})])]);
    box.appendChild(row);
    move.addEventListener('click',function(){
      move.disabled=true; say2(msg,'moving…','');
      api('/api/setup/migrate-secrets',{vars:vars}).then(function(d){
        if(!d || !d.ok){ move.disabled=false;
          say2(msg,(d&&(d.failed||[]).join('; '))||(d&&d.error)||'failed','err'); return; }
        say2(msg,'moved','ok');
        setTimeout(load, 600);        // re-read: the rows above now say "stored"
      });
    });
  }
  function mkSecret(box,varname,label,isSet){
    var i=inp('s-'+varname,'',isSet?'stored \u2014 type to replace':'paste to store','password');
    i.setAttribute('autocomplete','new-password'); i.setAttribute('spellcheck','false');
    var save=el('button',{class:'act',text:'Save'});
    var clr=el('button',{class:'act',text:'Clear'}); if(!isSet) clr.disabled=true;
    var st=el('span',{class:'chip '+(isSet?'have':''),text:isSet?'stored':'not set'});
    var msg=el('span',{class:'msg'});
    var wrap=el('div',{class:'row'},[el('span',{}),
      el('label',{class:'k',text:varname}),
      el('div',{class:'v'},[i,
        el('div',{class:'bar'},[save,clr,st,msg]),
        el('div',{class:'why',html:label+' \u00b7 never displayed back, never committed, '+
          'never on a command line'})])]);
    save.addEventListener('click',function(){
      if(!i.value){say2(msg,'nothing typed','err');return}
      save.disabled=true;
      api('/api/setup/secret',{var:varname,value:i.value}).then(function(d){
        save.disabled=false;
        if(!d.ok){say2(msg,d.error,'err');return}
        i.value=''; i.placeholder='stored \u2014 type to replace';
        st.textContent='stored'; st.className='chip have'; clr.disabled=false;
        say2(msg,'saved','ok');
      });
    });
    clr.addEventListener('click',function(){
      api('/api/setup/secret',{var:varname,value:null}).then(function(d){
        if(!d.ok){say2(msg,d.error,'err');return}
        st.textContent='not set'; st.className='chip'; clr.disabled=true;
        i.placeholder='paste to store'; say2(msg,'cleared','ok');
      });
    });
    box.appendChild(wrap);
  }
  function say2(n,m,c){n.textContent=m||''; n.className='msg '+(c||'')}

  // -------------------------------------------------------------- project ---
  var SCAN=[];
  function renderProject(){
    var P=E.project, box=$('#sw-proj'); box.textContent='';
    box.appendChild(row('name','Project name',inp('p-name',P.name,'shown in every report'),
      'currently <b>'+(P.name||'unset')+'</b>'));
    var best=P.plan_candidates[0];
    // The checkbox counts live on P.plan_candidates as {file,checkboxes}.
    var RAW = P.plan_candidates || [];
    function boxesFor(f){
      for(var i=0;i<RAW.length;i++){ if(RAW[i].file === f) return RAW[i].checkboxes }
      return null;
    }
    // Type-to-search across EVERYTHING the scan found. An <input> backed by
    // a <datalist> is the native combobox: the browser filters the entries as
    // you type, so the shortlist cap a plain <select> needed goes away, and a
    // path can also simply be pasted. Browse and Rescan stay for the rest.
    var planSel = inp('p-plan', P.plan, 'type to search '+RAW.length+' files…');
    planSel.setAttribute('list','p-plan-dl');
    planSel.setAttribute('autocomplete','off');
    planSel.setAttribute('spellcheck','false');
    var dl = el('datalist',{id:'p-plan-dl'});
    RAW.forEach(function(c){
      var o = document.createElement('option');
      o.value = c.file;
      o.label = c.checkboxes + ' checkbox' + (c.checkboxes===1?'':'es');
      dl.appendChild(o);
    });
    var planWrap = el('div',{},[planSel,dl]);
    var planBar = el('div',{class:'bar'});
    planWrap.appendChild(planBar);
    planBar.appendChild(pathPicker(planSel,'md',E.repo));
    var rescan = el('button',{class:'act',text:'Rescan'});
    rescan.title = 'Re-read the repo - a plan file added since this page loaded is '+
                   'not in the list until something looks again';
    rescan.addEventListener('click',function(){
      rescan.disabled=true; rescan.textContent='Scanning\u2026';
      load(function(){ rescan.disabled=false; rescan.textContent='Rescan' });
    });
    planBar.appendChild(rescan);
    var boxNote = el('div',{class:'why planboxes'});
    planWrap.appendChild(boxNote);
    function planBoxes(){
      var f = planSel.value, n = boxesFor(f);
      if(!f){ boxNote.textContent=''; boxNote.className='why planboxes'; return }
      if(n === null){
        boxNote.className='why planboxes';
        boxNote.innerHTML='not in the last scan \u2014 press <b>Rescan</b> if you just added it';
        return;
      }
      // The one failure this field exists to prevent, said BEFORE it happens
      // rather than after it has read 0% for a week.
      boxNote.className = 'why planboxes' + (n ? '' : ' nobox');
      boxNote.innerHTML = n
        ? '<b>'+n+'</b> checkbox'+(n===1?'':'es')+' in this file'
        : '<b>No checkboxes in this file.</b> Progress is derived only from '+
          '<code>- [ ]</code> / <code>- [x]</code> lines, so this plan would read '+
          '<b>0% forever</b>. Choose a file that has them, or add them there.';
    }
    planSel.addEventListener('change', planBoxes);
    planSel.addEventListener('input', planBoxes);
    planBoxes();
    box.appendChild(row('plan','Plan file', planWrap,
      'the guess is <b>the .md with the most checkboxes</b>'+
      (best?' \u2014 '+best.file+' ('+best.checkboxes+')':'')+
      ' \u00b7 scanned <b>'+RAW.length+'</b> file(s) under this repo'));
    box.appendChild(row('owner','Default owner',inp('p-owner',P.owner,'optional'),
      'used for phases with no explicit owner'));
    box.appendChild(row('start_date','Start date',inp('p-start',P.start_date,'YYYY-MM-DD','date'),
      'the schedule is projected forward from here'));

    var pub=el('input',{type:'checkbox',id:'p-pub'}); pub.checked=!!P.allow_artifact_publish;
    pub.style.marginTop='6px';
    box.appendChild(row('allow_artifact_publish','Sharing policy',
      el('div',{},[pub,el('span',{class:'mono',text:'  cleared to share this report outside this machine'})]),
      'A recorded answer, <b>not an enforced one</b>: this tool has no publish '+
      'button, so nothing here can stop a share. It is the committed note a '+
      'person \u2014 or an agent acting for you \u2014 checks before putting the '+
      'generated HTML somewhere others can read it. Off for every new project, '+
      'so clearing it is a deliberate change with a name on it in git.'));

    // ONE JIRA section. There were nine fields, seven of which are derivable
    // from the site URL and the project key: the browse and create URLs, the
    // API base, the API version and the auth style all follow from them. Asking
    // for each separately made a two-field job look like a configuration
    // project, and invited exactly the mismatches it then had to warn about.
    var A = P.jira_api || {};
    var site = (A.api_base || (P.jira_browse||'').replace(/\/browse\/.*$/,'') || '').replace(/\/+$/,'');
    var cloud = /\.atlassian\.net/i.test(site);
    box.appendChild(el('div',{class:'subhead',
      html:'JIRA <span class="quiet">— optional. Everything below is derived from these '+
           'two; open Advanced only if your instance differs.</span>'}));
    box.appendChild(row('jira_site','Site URL',
      inp('p-jsite',site,'https://yoursite.atlassian.net'),
      'the site root, no path \u00b7 gives you ticket links, a prefilled create form, '+
      'and (with a token) direct creation', !!site));
    box.appendChild(row('jira_project_key','Project key',
      inp('p-jpk',A.project_key,'PROJ'),
      'the prefix on every issue, e.g. <b>PROJ</b> in PROJ-123', !!A.project_key));

    var adv = el('details',{class:'advfold'});
    adv.appendChild(el('summary',{text:'Advanced — issue type, auth, URL overrides'}));
    var abox = el('div',{});
    adv.appendChild(abox);
    box.appendChild(adv);

    abox.appendChild(row('jira_issue_type','Issue type',
      inp('p-jit',A.issue_type||'Task','Task'),
      'must be a type your project accepts \u2014 Task, Story, Bug'));
    abox.appendChild(row('jira_auth_user','Account email',
      inp('p-jau',A.auth_user,'you@example.com'),
      'JIRA Cloud identifies an API token by the account it belongs to. Leave empty '+
      'for a self-hosted instance, which uses a bearer token instead.', !!A.auth_user));
    abox.appendChild(row('jira_auth_env','Token variable',
      inp('p-jae',A.auth_env||'JIRA_PAT','JIRA_PAT'),
      'the NAME of the variable holding the token \u00b7 store its value under '+
      '<b>Tokens</b> below'));
    abox.appendChild(row('jira_browse','Browse URL override',
      inp('p-jb',P.jira_browse,''),
      'left empty this is <b>{site}/browse/{key}</b>', false));
    abox.appendChild(row('jira_create','Create URL override',
      inp('p-jc',P.jira_create,''),
      'a prefilled create form \u00b7 needs the numeric <b>pid</b>, which the key alone '+
      'cannot give, so paste one here if you want that route', !!P.jira_create));

    // Derivation, shown as it happens so nothing is silently invented.
    var derived = el('div',{class:'why derived'});
    box.appendChild(derived);
    function redraw(){
      var u = val('p-jsite').replace(/\/+$/,''), k = val('p-jpk');
      var c = /\.atlassian\.net/i.test(u);
      if(!u){ derived.innerHTML = '<b>No site URL</b> \u2014 ticket keys will show as plain '+
        'text, and creating a ticket is not offered.'; return; }
      var line = 'Derived: browse <code>'+esc(u)+'/browse/'+esc(k||'{key}')+
        '</code> \u00b7 API <code>'+esc(u)+'</code> v'+(c?3:2)+
        ' \u00b7 auth <b>'+(c?'basic, with the account email':'bearer token')+'</b>'+
        (c?' \u2014 Cloud rejects bearer tokens':'');
      // Say whether creating an issue can ACTUALLY work. Listing what is derived
      // and stopping there reads as readiness; a missing account email would
      // then surface only as a 401, at the moment you tried to raise a ticket.
      var envv = val('p-jae') || 'JIRA_PAT', miss = [];
      if(!k) miss.push('a <b>project key</b>');
      if((E.context_secrets_set||[]).indexOf(envv) < 0)
        miss.push('a token in <b>'+esc(envv)+'</b> (Tokens, below)');
      if(c && !val('p-jau')) miss.push('the <b>account email</b> the token belongs to');
      derived.innerHTML = line + '<div class="ready '+(miss.length?'no':'yes')+'">'+
        (miss.length
          ? 'Create in JIRA stays off until you add '+miss.join(', and ')+'.'
          : 'Create in JIRA is ready \u2014 issues will be raised in <b>'+esc(k)+'</b>.')+
        '</div>';
    }
    ['p-jsite','p-jpk','p-jau','p-jae'].forEach(function(id){
      var n=$('#'+id); if(n) n.addEventListener('input',redraw);
    });
    redraw();

    if(P.actions.length) $('#sw-actions').innerHTML =
      'This project defines <b>'+P.actions.length+'</b> run command(s): <code>'+
      P.actions.join('</code> <code>')+'</code>. The wizard cannot add or change those \u2014 '+
      'they name executables this server runs, so they stay a file edit plus the trust prompt.';

    // An unconfigured repo gets Init instead of Write: there is nothing to diff
    // against yet, and offering "preview changes" against a file that does not
    // exist would be a dead end on exactly the machine that needs this most.
    $('#sw-initcard').style.display = E.configured?'none':'block';
    $('#sw-writecard').style.display = E.configured?'block':'none';
    if(!E.configured) $('#sw-ppath').textContent = E.config_path+'  (does not exist yet)';
  }

  function renderScan(rows){
    SCAN=rows; var t=$('#sw-svc tbody'); t.textContent='';
    rows.forEach(function(s,i){
      var cb=el('input',{type:'checkbox'});
      cb.checked = s.up && !s.configured; cb.disabled = s.configured;
      var st = s.configured ? el('span',{class:'chip have',text:'configured'})
             : s.up ? el('span',{class:'chip up',text:'up '+s.ms+'ms'})
                    : el('span',{class:'chip down',text:'no answer'});
      var url=inp('u-'+i,s.url); url.disabled=s.configured;
      var kind=sel('k-'+i,s.kind,['mcp-stateless-http','mcp-stateful-http','prompt-only']);
      kind.disabled=s.configured;
      var auth=inp('a-'+i,s.auth_env,'env var name'); auth.disabled=s.configured;
      var tr=el('tr',{class:cb.checked?'':'off'},[
        el('td',{},[cb]),
        el('td',{},[el('div',{text:s.label}),el('div',{class:'why',text:s.what})]),
        el('td',{},[st]), el('td',{},[url]), el('td',{},[kind]), el('td',{},[auth])]);
      cb.addEventListener('change',function(){tr.classList.toggle('off',!cb.checked)});
      tr.dataset.i=i; t.appendChild(tr);
    });
    $('#sw-svc').style.display = rows.length?'table':'none';
  }
  function pickedContexts(){
    var out=[];
    document.querySelectorAll('#sw-svc tbody tr').forEach(function(tr){
      var i=tr.dataset.i, cb=tr.querySelector('input[type=checkbox]');
      if(!cb.checked||cb.disabled) return;
      var s=SCAN[i];
      out.push({name:s.name,label:s.label,kind:val('k-'+i),url:val('u-'+i),
                auth_env:val('a-'+i),probe:true});
    });
    return out;
  }
  function projFields(){
    var f={};
    if(on('name','#sw-proj')) f.name=val('p-name');
    if(on('plan')) f.plan=val('p-plan');
    if(on('owner')) f.owner=val('p-owner');
    if(on('start_date')) f.start_date=val('p-start');
    if(on('allow_artifact_publish')) f.allow_artifact_publish=$('#p-pub').checked;
    // The two merged fields expand here, so progress.toml keeps its explicit
    // keys and nothing downstream has to know they were derived.
    var site = val('p-jsite').replace(/\/+$/,'');
    var key  = val('p-jpk');
    if(on('jira_site') && site){
      var c = /\.atlassian\.net/i.test(site);
      f.jira_api_base   = site;
      f.jira_api_version= c ? '3' : '2';
      f.jira_auth_mode  = (on('jira_auth_user') && val('p-jau')) ? 'basic' : 'bearer';
      if(!val('p-jb')) f.jira_browse = site + '/browse/{key}';
    }
    if(on('jira_browse') && val('p-jb')) f.jira_browse = val('p-jb');
    if(on('jira_create') && val('p-jc')) f.jira_create = val('p-jc');
    if(on('jira_project_key')) f.jira_project_key = key;
    if(on('jira_issue_type')) f.jira_issue_type = val('p-jit');
    if(on('jira_auth_user')) f.jira_auth_user = val('p-jau');
    if(on('jira_auth_env')) f.jira_auth_env = val('p-jae');
    return f;
  }

  // ------------------------------------------------------------------ wire ---
  function load(done){
    fetch('/api/setup',{headers:{'X-PCC-Token':T}}).then(function(r){return r.json()})
      .then(function(d){
        E=d;
        var pname = (d.project && d.project.name) ? d.project.name : '';
        $('#sw-where').innerHTML = (pname ? '<b>'+pname+'</b> \u00b7 ' : '')+
          d.repo+' \u00b7 '+d.platform+' \u00b7 python '+d.python+
          ' \u00b7 <a href="/">back to dashboard</a>';
          // Every confirmation on this page is about the project that was
          // being served when it was written. load() runs on a project
          // switch, so leaving one standing lets "saved for this project"
          // sit under a row belonging to a different project.
          ['#sw-lmsg','#sw-mmsg','#sw-smsg','#sw-imsg'].forEach(function(s){
            var n=$(s); if(n){ n.textContent=''; n.className='msg' }
          });
          say('#sw-pmsg','No unsaved changes.','');
        $('#sw-lpath').textContent=d.profile_path;
        $('#sw-mpath').textContent=d.profile_path;
        $('#sw-ppath').textContent=d.config_path;
        if(d.config_error) say('#sw-pmsg','config unreadable: '+d.config_error,'err');
        $('#sw-host').value=(d.host_default||'127.0.0.1');
        renderLocal(); renderProject(); renderMine(); renderProjects(); renderSecrets();
        if(typeof done === 'function') done();
      });
  }
  document.addEventListener('click',function(ev){
    var b=ev.target.closest('.tabs button'); if(!b) return;
    document.querySelectorAll('.tabs button').forEach(function(x){x.classList.toggle('on',x===b)});
    document.querySelectorAll('.pane').forEach(function(p){p.classList.toggle('on',p.id===b.dataset.p)});
  });
  document.addEventListener('DOMContentLoaded',function(){
    load();
    $('#sw-save-local').addEventListener('click',function(){
      var b={}; ['name','tool','shell'].forEach(function(k){
        if(!on(k,'#sw-local')) return;  // repo_path has its own Save, on the project tab
        b[k]=val({name:'f-name',tool:'f-tool',shell:'f-shell'}[k]);
      });
      api('/api/setup/local',b).then(function(d){
        say('#sw-lmsg', d.ok?('written to '+d.path+' \u2014 reload the dashboard to see it'):d.error,
            d.ok?'ok':'err');
      });
    });

      // The checkout lives on the project tab now, so it needs a Save there.
      // Same endpoint: the value still goes to the profile's [repos] map,
      // keyed by this repo. Only its position on screen changed.
      $('#sw-save-mine').addEventListener('click',function(){
        var b={};
        if(!on('repo_path','#sw-mine')){ say('#sw-mmsg','row unticked \u2014 nothing sent',''); return }
        b.repo_path=val('f-repo');
        api('/api/setup/local',b).then(function(d){
          say('#sw-mmsg', d.ok?('saved for this project in '+d.path):d.error, d.ok?'ok':'err');
        });
      });
    $('#sw-add').addEventListener('click',function(){
      var v = $('#sw-addpath').value.trim();
      if(!v){ say('#sw-pmsg2','type a path first','err'); return; }
      var b = this; b.disabled = true; say('#sw-pmsg2','opening…');
      api('/api/setup/switch',{path:v}).then(function(d){
        b.disabled = false;
        if(!d.ok){ say('#sw-pmsg2',d.error,'err'); return; }
        $('#sw-addpath').value = '';
        say('#sw-pmsg2','now serving ' + d.name +
            (d.configured ? '' : ' — no config yet: use Initialize on the project tab') +
            (d.trusted ? '' : ' — read-only until its commands are approved at a restart'),
            d.trusted ? 'ok' : '');
        load();
      });
    });
    $('#sw-addpath').addEventListener('keydown',function(ev){
      if(ev.key === 'Enter') $('#sw-add').click();
    });
    $('#sw-scan').addEventListener('click',function(){
      var btn=this; btn.disabled=true; say('#sw-smsg','scanning\u2026');
      api('/api/setup/scan',{host:$('#sw-host').value.trim()||'127.0.0.1'}).then(function(d){
        btn.disabled=false;
        if(!d.ok){say('#sw-smsg',d.error,'err');return}
        var up=d.services.filter(function(s){return s.up}).length;
        say('#sw-smsg',up+' of '+d.services.length+' answered \u00b7 a port that answers proves '+
            'something is listening, not that it is what we named it','');
        renderScan(d.services);
      });
    });
    $('#sw-init').addEventListener('click',function(){
      var b=this; b.disabled=true; say('#sw-imsg','writing…');
      api('/api/setup/init',{name:val('p-name'),plan:val('p-plan'),owner:val('p-owner'),
                             start_date:val('p-start'),jira_base:val('p-jb')})
        .then(function(d){
          b.disabled=false;
          if(!d.ok){say('#sw-imsg',d.error,'err');return}
          say('#sw-imsg','created '+d.path,'ok');
          var pre=$('#sw-ilog'); pre.style.display='block'; pre.textContent=d.log||'';
          load();     // re-read: the page now has a config to edit rather than create
        });
    });
    // Save is ONE button that arms, not a disabled gate behind Preview. The rule
    // it enforces is unchanged - a committed file is never written without
    // showing the diff first - but that diff is now a confirmation step inside
    // the save, rather than a separate button you had to find first. A greyed
    // out "Write" sitting two cards below the field you just edited reads as
    // broken, and people reasonably went looking for Save.
    // No arm state any more: a click saves. Kept as the one place that
    // invalidates a diff the user should no longer trust.
    function disarm(){ diff($('#sw-diff'),''); }
    $('#sw-preview').addEventListener('click',function(){
      api('/api/setup/project',{fields:projFields(),contexts:pickedContexts(),apply:false})
        .then(function(d){
          if(!d.ok){say('#sw-pmsg',d.error,'err');diff($('#sw-diff'),'');return}
          diff($('#sw-diff'),d.diff);
          say('#sw-pmsg', d.changed?'this is what would be saved':'nothing would change','');
        });
    });
    $('#sw-apply').addEventListener('click',function(){
      // One click writes. The confirm step existed so a write was never a
      // surprise, but this is a local file, Preview is still one click away,
      // and the diff shown afterwards is the one that LANDED rather than the
      // one a preview predicted - which is the stronger guarantee anyway.
      var b = this;
      b.disabled = true;
      api('/api/setup/project',{fields:projFields(),contexts:pickedContexts(),apply:true})
        .then(function(d){
          b.disabled = false;
          if(!d.ok){ say('#sw-pmsg',d.error,'err'); return }
          if(!d.changed){
            diff($('#sw-diff'),'');
            say('#sw-pmsg','Nothing to save - already up to date.','');
            $('#sw-writecard').classList.remove('dirty');
            return;
          }
          diff($('#sw-diff'),d.diff);
          $('#sw-writecard').classList.remove('dirty');
          say('#sw-pmsg','Saved. Below is what landed.','ok');
        });
    });
    // Editing after arming invalidates the diff you were just shown.
    // Bind to the containers that actually feed the committed save, not the
    // whole pane: the Tokens and checkout cards live here too, and neither
    // writes docs/progress.toml. Unscoped, typing a token claimed the config
    // was dirty, and cleared a diff the user was still reading.
    ['#sw-proj','#sw-svc'].forEach(function(sel){
      var node=$(sel); if(!node) return;
      node.addEventListener('input',function(){
        disarm();                       // the shown diff is now stale
        say('#sw-pmsg','Unsaved changes.','');
        $('#sw-writecard').classList.add('dirty');
      });
    });
  });
})();
"""


def setup_page(token: str) -> str:
    """The wizard shell. Everything inside is filled from /api/setup so the page
    and the CLI can never disagree about what was detected."""
    return (
        "<!doctype html><meta charset=utf-8><title>Control Center — Setup</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>" + theme_tokens() + SETUP_CSS + "</style>"
        '<div class="sw">'
        "<h1>Control Center — Setup</h1>"
        '<div class="sub" id="sw-where">loading…</div>'
        '<div class="tabs">'
        '<button class="on" data-p="pane-local">This machine</button>'
        '<button data-p="pane-proj">This project</button>'
        '<button data-p="pane-switch">Projects</button></div>'

        '<div class="pane" id="pane-switch">'
        '<div class="card"><h2>Switch project</h2>'
        '<p class="note">One installed copy serves any number of projects. This list fills '
        'itself from what you open, and lives in <code id="sw-preg">…</code> — outside every '
        'repo, because a list of your projects belongs to you and not to any one of them.</p>'
        '<table class="svc" id="sw-projects">'
        '<colgroup><col style="width:34%"><col><col style="width:150px"><col style="width:170px">'
        '</colgroup><thead><tr><th>project</th><th>path</th><th>state</th><th></th></tr></thead>'
        '<tbody></tbody></table>'
        '<div class="bar" style="margin-top:14px">'
        '<input type="text" id="sw-addpath" placeholder="C:\\path\\to\\another\\project" '
        'style="flex:1;min-width:260px;padding:7px 9px;border:1px solid var(--line);'
        'border-radius:7px;background:var(--bg);color:var(--ink);font:inherit;font-size:13px">'
        '<button class="act" id="sw-add">Open this path</button>'
        '<span class="msg" id="sw-pmsg2"></span></div></div>'

        '<div class="card"><h2>What switching does — and does not</h2>'
        '<p class="note">Switching re-points this dashboard: the plan, the phases and the '
        'context providers all come from the project you pick. <b>It never grants command '
        'execution.</b> A project whose <code>[[action]]</code> commands you have already '
        'approved keeps its Run and Test buttons; one you have not is served <b>read-only</b> '
        'and its commands are named but stripped. Approving them needs a restart '
        '(<code>--repo &lt;path&gt;</code>), where the exact argv set can be printed and '
        'answered for at a console — a form post is the wrong authority for "here is a new '
        'command to run".</p></div></div>'

        '<div class="pane on" id="pane-local">'
        '<div class="card"><h2>Your profile</h2>'
        '<p class="note">Personal and never committed. Written to '
        '<code id="sw-lpath">…</code>, then overlaid on the team roster in '
        '<code>docs/progress.toml</code> — your name, your tool and your shell, '
        'without proposing them as a commit. Your checkout is personal too, but '
        'it is saved <i>per project</i>, so it is on the <b>This project</b> tab. '
        'Untick a row to leave it as it is.</p>'
        '<div id="sw-local"></div>'
        '<div class="bar" style="margin-top:14px">'
        '<button class="act pri" id="sw-save-local">Save profile</button>'
        '<span class="msg" id="sw-lmsg"></span></div></div>'

        '<div class="card"><h2>Detected on PATH</h2>'
        '<p class="note">Evidence for the tool guess above. A tool that is not here '
        'can still be selected — the session prompt is always copyable.</p>'
        '<div class="tool-list" id="sw-tools"></div></div>'

        '</div>'

        '<div class="pane" id="pane-proj">'
        '<div class="card"><h2>Project</h2>'
        '<p class="note">Shared settings, written to <code id="sw-ppath">…</code> — a '
        '<b>committed</b> file. Save shows you the change and asks once before writing.</p>'
        '<div id="sw-proj"></div></div>'

        '<div class="card"><h2>Your checkout</h2>'
        '<p class="note"><b>Personal, not shared.</b> Saved per project to '
        '<code id="sw-mpath">…</code> — outside this repo, so it is never '
        'committed and never reaches a published report. Switching projects '
        'switches this value. <b>Save config</b> below writes the committed '
        'file only and does not carry this field — use <b>Save checkout</b> here.</p>'
        '<div id="sw-mine"></div>'
        '<div class="bar" style="margin-top:14px">'
        '<button class="act pri" id="sw-save-mine">Save checkout</button>'
        '<span class="msg" id="sw-mmsg"></span></div></div>'

        '<div class="card"><h2>Services on this host</h2>'
        '<p class="note">Reachability only: a TCP connect, no credential sent, no protocol '
        'spoken. Tick what you want adopted as a <code>[[context]]</code> provider and edit '
        'anything that is wrong.</p>'
        '<div class="bar"><span class="mono">host</span>'
        '<input type="text" id="sw-host" value="127.0.0.1" style="width:180px;padding:6px 9px;'
        'border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);'
        'font:inherit;font-size:13px">'
        '<button class="act" id="sw-scan">Scan</button>'
        '<span class="msg" id="sw-smsg"></span></div>'
        '<table class="svc" id="sw-svc" style="display:none;margin-top:14px">'
        '<colgroup><col class="c0"><col class="c1"><col class="c2"><col class="c3">'
        '<col class="c4"><col class="c5"></colgroup><thead><tr>'
        '<th></th><th>service</th><th>status</th><th>url</th><th>kind</th><th>auth env</th>'
        '</tr></thead><tbody></tbody></table></div>'

        '<div class="card"><h2>Run commands</h2>'
        '<p class="note" id="sw-actions">This project defines no run commands. The wizard '
        'cannot add them: they name executables this server runs, so they stay a deliberate '
        'edit to <code>docs/progress.toml</code> plus the one-time trust prompt.</p></div>'

        '<div class="card" id="sw-initcard" style="display:none"><h2>Initialize</h2>'
        '<p class="note">This repo has no <code>docs/progress.toml</code> yet. Init writes one '
        'from the fields above, appends the <code>.gitignore</code> entries, creates '
        '<code>secrets/context.env.example</code>, and records this repo in the trust store. '
        'Publishing starts <b>off</b> and run commands start <b>commented out</b> — both are '
        'later, deliberate choices.</p>'
        '<div class="bar"><button class="act pri" id="sw-init">Create docs/progress.toml</button>'
        '<span class="msg" id="sw-imsg"></span></div>'
        '<pre class="diff" id="sw-ilog" style="display:none"></pre></div>'

        '<div class="card"><h2>Tokens</h2>'
        '<p class="note">Stored in <code id="sw-secpath">…</code> — gitignored, mode 0600, '
        'and <b>never sent back to this page</b>: only "stored" or "not set". The config '
        'above holds the variable <i>name</i>; the value reaches a launched session by '
        'file path, never on a command line. <b>Ticket links need no token</b> — browse '
        'and the prefilled create form open in your browser, which already has your '
        'session. A token is only needed to create issues over the API.</p>'
        '<div id="sw-secrets"></div></div>'

        '<div class="card sticky-save" id="sw-writecard">'
        '<div class="bar"><button class="act pri" id="sw-apply">Save config</button>'
        '<button class="act" id="sw-preview">Preview</button>'
        '<span class="msg" id="sw-pmsg">No unsaved changes.</span></div>'
        '<pre class="diff" id="sw-diff" style="display:none"></pre></div></div>'

        "</div><script>window.__SW_TOKEN__=" + _pr.js(token) + ";</script>"
        "<script>" + SETUP_JS + "</script>")


def setup_local(body: dict) -> dict:
    """Save the personal profile. Only the keys that were ticked arrive, so an
    unticked row keeps whatever the profile already held."""
    prof = _pr.load_user_profile()
    out = {"name": prof.get("name", ""), "tool": prof.get("tool", "claude"),
           "shell": prof.get("shell", "bash"), "repos": dict(prof.get("repos") or {})}
    for k in ("name", "tool", "shell"):
        if k in body:
            v = str(body[k]).strip()
            if k == "shell" and v not in ("powershell", "bash"):
                return {"ok": False, "error": "shell must be powershell or bash"}
            if v:
                out[k] = v
    if body.get("repo_path"):
        out["repos"][str(REPO)] = str(body["repo_path"]).strip()
    try:
        p = _pr.write_user_profile(out)
    except OSError as exc:
        return {"ok": False, "error": f"could not write profile: {exc}"}
    return {"ok": True, "path": str(p)}


def legacy_secrets() -> dict:
    """Tokens still sitting in the old user-level file.

    Moving storage to the project would otherwise strand them silently: the
    variable would read as unset with no hint that a value exists elsewhere.
    Names only — the values are not read here.
    """
    p = _pr.user_secrets_path()
    return {"path": str(p), "vars": _pr.loaded_secret_names(p) if p.exists() else []}


def migrate_secrets(names: list) -> dict:
    """Move named tokens from the old user file into the project's.

    The value passes through this process and is written straight out; it is
    never logged, never returned, and never shown. The source line is removed
    only after the destination write succeeds, so a failure cannot lose it.
    """
    src, dst = _pr.user_secrets_path(), project_secrets_path()
    if not src.exists():
        return {"ok": False, "error": "there is no user-level secrets file"}
    have = set(_pr.loaded_secret_names(src))
    moved, failed = [], []
    for var in [str(n) for n in (names or [])]:
        if var not in have:
            failed.append(f"{var}: not in the old file")
            continue
        try:
            val = None
            pat = re.compile(r"^\s*" + re.escape(var) + r"\s*=\s*(.*?)\s*$")
            for line in src.read_text(encoding="utf-8").splitlines():
                m = pat.match(line)
                if m and m.group(1):
                    val = m.group(1).strip().strip('"').strip("'")
            if val is None:
                failed.append(f"{var}: empty")
                continue
            _pr.write_secret(dst, var, val)          # destination first
            _pr.write_secret(src, var, None)         # then drop the original
            moved.append(var)
        except (OSError, ValueError) as exc:
            failed.append(f"{var}: {exc}")
    return {"ok": not failed, "moved": moved, "failed": failed, "path": str(dst)}


def setup_secret(body: dict) -> dict:
    """Store or clear one token. Values go in, names come out — never the value.

    One destination now: the project's gitignored env file. There is no scope
    to pick, because there was never a good answer to which one a given token
    belonged in — and two files meant two places to look when one was empty.
    """
    path = project_secrets_path()
    val = body.get("value")
    if val is not None:
        val = str(val).strip()
        if not val:
            return {"ok": False, "error": "empty value — use Clear to remove it"}
    try:
        _pr.write_secret(path, str(body.get("var", "")), val)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(path), "set": val is not None}


def setup_init(body: dict) -> dict:
    """Stand the control center up in a repo that has none — the Init stage,
    from the browser. Same scaffolder as `--init`, so the two cannot diverge:
    publishing stays off, trust is recorded here, commands stay commented out."""
    import contextlib
    import io
    if (REPO / "docs" / "progress.toml").exists():
        return {"ok": False, "error": "this repo is already initialized — edit it below instead"}
    name = str(body.get("name") or "").strip() or REPO.name
    jira_base = str(body.get("jira_base") or "").strip()
    if jira_base and not re.match(r"^https?://", jira_base):
        return {"ok": False, "error": "JIRA base must start with http(s)://"}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = _pr.scaffold_init(REPO, name,
                                   owner=str(body.get("owner") or "").strip() or None,
                                   jira_base=jira_base or None,
                                   jira_project=str(body.get("jira_project") or "").strip() or None)
    except OSError as exc:
        return {"ok": False, "error": f"could not write: {exc}"}
    if rc != 0:
        return {"ok": False, "error": out.getvalue().strip() or "init failed"}

    # The scaffolder detects the plan; honour an explicit override from the form.
    extra = {k: v for k, v in (("plan", body.get("plan")),
                               ("start_date", body.get("start_date"))) if v}
    if extra:
        _pr.apply_project_edits(REPO, extra, [], dry_run=False)
    init_repo(REPO)
    return {"ok": True, "path": str(REPO / "docs" / "progress.toml"),
            "log": out.getvalue().strip()}


def setup_project(body: dict) -> dict:
    """Preview or write the shared config. Writing reloads context providers but
    NOT the run-command allowlist: that one was approved at startup and a new
    command must go through the trust prompt on a restart, not a form post."""
    r = _pr.apply_project_edits(REPO, body.get("fields") or {},
                                body.get("contexts") or [],
                                dry_run=not body.get("apply"))
    if r.get("ok") and r.get("written"):
        try:
            CFG.clear()
            CFG.update(tomllib.loads((REPO / "docs" / "progress.toml").read_text(encoding="utf-8")))
            LAUNCHERS.clear()
            LAUNCHERS.update(build_launchers(CFG))
            _merge_config_launchers(LAUNCHERS, CFG)
            s = sync_context(CFG)
            r["reload"] = ("context reloaded" +
                           (f", .mcp.json +{len(s['added'])} ~{len(s['updated'])}"
                            if s.get("ok") else ", .mcp.json sync failed"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            r["reload"] = f"written, but reload failed ({exc}) — restart the server"
    return r


class Handler(BaseHTTPRequestHandler):
    server_version = "progress-control-center/1.0"
    token = ""
    verbose = False

    def log_message(self, fmt, *args):
        if Handler.verbose:
            super().log_message(fmt, *args)

    # -- guards ---------------------------------------------------------------
    def _loopback_host(self) -> bool:
        """Blocks DNS rebinding: a hostile page resolving its own name to
        127.0.0.1 still sends its own Host header, which will not be loopback."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _authed(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-PCC-Token", ""), Handler.token)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._loopback_host():
            self._json({"error": "loopback only"}, 421)
            return
        path = urlparse(self.path).path

        if path == "/":
            if not (REPO / "docs" / "progress.toml").exists():
                self._redirect("/setup")     # nothing to render yet; go configure
                return
            try:
                model = build(REPO)
                # render() returns an artifact-safe FRAGMENT (no doctype, html,
                # head or body — the artifact wrapper supplies those). Served
                # directly it therefore had no <html lang>, which screen readers
                # need to pick a pronunciation. Wrap it here, splitting at the
                # title so the head bits stay in the head.
                frag = render(model)
                cut = frag.index("</title>") + len("</title>")
                page = ('<!doctype html><html lang="en"><head>' + frag[:cut] +
                        "</head><body>" + frag[cut:] +
                        action_layer(Handler.token, model) + "</body></html>")
            except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
                # A broken config must not blank the page: say what broke and
                # keep the one route that can fix it reachable.
                page = ("<!doctype html><meta charset=utf-8><style>" + _pr.CSS +
                        "</style><div style='max-width:720px;margin:60px auto;padding:0 24px'>"
                        "<h1>This project will not render</h1><p><code>" +
                        _pr.e(f"{type(exc).__name__}: {exc}") + "</code></p>"
                        "<p><a href='/setup'>Open setup</a> to fix the configuration, "
                        "or run <code>--check</code> for the full list of findings.</p></div>")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/setup":
            # Deliberately reachable even when the repo has NO config yet — a
            # wizard you can only open once you are already configured is no use
            # on the machine that needs it most.
            body = setup_page(Handler.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/setup":
            # GET, so it must never carry a secret VALUE: detect_environment
            # returns which variables are set, and nothing more about them.
            d = _pr.detect_environment(REPO)
            d["host_default"] = "127.0.0.1"
            # The picker needs each project's real state, not just its path: a
            # checkout that has since been deleted or moved must say so rather
            # than fail on click, and an untrusted one must be labelled BEFORE
            # you switch, so "no run buttons" is expected rather than surprising.
            projs = []
            for e in _pr.load_projects():
                pp = Path(e["path"])
                alive = pp.is_dir()
                projs.append({**e,
                              "exists": alive,
                              "configured": alive and (pp / "docs" / "progress.toml").exists(),
                              "state": project_trust(pp.resolve()) if alive else "missing",
                              "current": pp.resolve() == REPO if alive else False})
            d["legacy_secrets"] = legacy_secrets()
            d["projects"] = projs
            d["project_registry"] = str(_pr.projects_path())
            self._json(d)
            return

        if path == "/api/context":
            self._json({"providers": probe_status()})
            return

        if path == "/api/model":
            self._json(build(REPO))
            return

        if path.startswith("/api/run/"):
            rid = path.rsplit("/", 1)[-1]
            with RUNS_LOCK:
                r = RUNS.get(rid)
                if r is None:
                    self._json({"error": "unknown run"}, 404)
                    return
                self._json({"lines": list(r["lines"]), "done": r["done"], "rc": r["rc"]})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._loopback_host():
            self._json({"error": "loopback only"}, 421)
            return
        if not self._authed():
            self._json({"error": "bad or missing token"}, 403)
            return

        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            # UnicodeDecodeError too: a non-UTF8 byte in the body must be a 400,
            # not an unhandled exception that drops the connection.
            self._json({"error": "bad json"}, 400)
            return
        path = urlparse(self.path).path

        if path == "/api/run":
            task = body.get("task")
            if task not in ACTIONS:
                self._json({"error": "task " + repr(task) + " is not in the allowlist"}, 400)
                return
            self._json({"run_id": start_run(task)})
            return

        if path == "/api/tick":
            self._json(tick(body.get("file", ""), body.get("raw", ""), body.get("state", "done")))
            return

        if path == "/api/sync-context":
            self._json(sync_context(CFG))
            return

        if path == "/api/setup/browse":
            # Read-only, but it enumerates this machine's disk. On a non-loopback
            # bind the page belongs to someone else, and so would the listing.
            if not _pr.LOCAL_SURFACE:
                self._json({"ok": False, "error":
                            "browsing is disabled on a non-loopback dashboard"})
                return
            self._json(browse(str(body.get("path", "")), str(body.get("want", "dir"))))
            return

        if path == "/api/setup/switch":
            self._json(switch_project(str(body.get("path", ""))))
            return

        if path == "/api/setup/forget-project":
            try:
                _pr.forget_project(str(body.get("path", "")))
                self._json({"ok": True})
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)})
            return

        if path == "/api/setup/scan":
            host = str(body.get("host", "127.0.0.1")).strip() or "127.0.0.1"
            if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,255}", host):
                self._json({"ok": False, "error": "not a hostname or address"})
                return
            have = {c.get("name") for c in CFG.get("context", [])}
            rows = _pr.scan_services(host)
            for r in rows:
                r["configured"] = r["name"] in have
            self._json({"ok": True, "host": host, "services": rows})
            return

        if path == "/api/setup/local":
            self._json(setup_local(body))
            return

        if path == "/api/setup/migrate-secrets":
            self._json(migrate_secrets(body.get("vars") or []))
            return

        if path == "/api/setup/secret":
            self._json(setup_secret(body))
            return

        if path == "/api/phase/activity":
            self._json(phase_activity(str(body.get("phase", "")), build(REPO)))
            return

        if path == "/api/phase/draft-ticket":
            self._json(draft_ticket(str(body.get("phase", "")),
                                    str(body.get("tool", "claude")), build(REPO)))
            return

        if path == "/api/phase/ticket-draft":
            self._json(read_ticket_draft(str(body.get("phase", ""))))
            return

        if path == "/api/phase/create-ticket":
            self._json(create_jira_issue(str(body.get("phase", "")),
                                         body.get("summary", ""),
                                         body.get("description", "")))
            return

        if path == "/api/phase/jira":
            self._json(link_ticket(str(body.get("phase", "")), body.get("key", "")))
            return

        if path == "/api/setup/init":
            self._json(setup_init(body))
            return

        if path == "/api/setup/project":
            self._json(setup_project(body))
            return

        if path == "/api/session":
            self._json(open_session(str(body.get("phase", "x")), body.get("prompt", ""),
                                    str(body.get("tool", "claude")),
                                    blank=bool(body.get("blank"))))
            return

        self._json({"error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="Local, actionable Visual Progress dashboard.")
    ap.add_argument("--repo", default=None,
                    help="project root to serve (default: PROGRESS_REPO env, then the "
                         "cwd's git repo if it has docs/progress.toml, then this install's repo)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default — leave it there; this endpoint runs commands")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--trust-yes", action="store_true", dest="trust_yes",
                    help="approve this repo's configured commands without asking")
    a = ap.parse_args()
    global BIND_HOST
    BIND_HOST = a.host

    init_repo(_pr.resolve_repo(a.repo))
    fresh = not (REPO / "docs" / "progress.toml").exists()
    if not check_trust(REPO, ACTIONS, a.trust_yes, config_launchers(CFG)):
        return 1
    post_trust_setup()
    Handler.token = secrets.token_urlsafe(24)
    Handler.verbose = a.verbose
    # Refuse to share the port. On Windows SO_REUSEADDR lets a SECOND server
    # bind one that is already listening; both then run, requests go to whichever
    # the OS picks, and you read stale pages from an old build while believing
    # you restarted. Failing loudly here is worth more than the convenience.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as exc:
        print(f"cannot bind {a.host}:{a.port} — {exc}", file=sys.stderr)
        print("  another dashboard is probably already running there. Stop it, or "
              "pass --port.", file=sys.stderr)
        return 1
    url = "http://{}:{}/".format(a.host, a.port)
    if fresh:
        url += "setup"
        print("Control Center SETUP  " + url, flush=True)
        print("  repo    : " + str(REPO) + "  (no docs/progress.toml yet)")
    else:
        print("Control Center dashboard  " + url, flush=True)
        print("  repo    : " + str(REPO))
    print("  setup   : http://{}:{}/setup".format(a.host, a.port))
    print("  actions : " + (", ".join(ACTIONS) or "none"))
    print("  distro  : " + DISTRO)
    print("  launchers: " + (", ".join(LAUNCHERS) or "none detected"))
    print("  ctrl-c to stop")
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

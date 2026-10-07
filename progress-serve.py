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
SERVE_PORT = 0                                  # set by main(); recorded in the projects list
DISTRO = os.environ.get("PCC_DISTRO", "Ubuntu-24.04")
PROMPT_DIR = REPO / _pr.WORK_DIR                # generated, gitignored; one name, shared
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
    return _py_for(REPO, *args)


def _py_for(repo: Path, *args: str) -> list[str]:
    return [sys.executable, str(SELF_DIR / "progress-report.py"), "--repo", str(repo), *args]


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


def tick(rel_file: str, raw: str, state: str, repo: Path | None = None) -> dict:
    """Flip one checkbox in the plan. Matches the verbatim line, never a number.
    `repo` is another registered project (Today's Mark done); default this one."""
    if state not in MARK:
        return {"ok": False, "error": "unknown state " + repr(state)}
    root = Path(repo).resolve() if repo else REPO
    own = _pkey(root) == _pkey(REPO)

    target = (root / rel_file).resolve()
    if root.resolve() not in target.parents or target.suffix != ".md":
        return {"ok": False, "error": "refusing to edit outside the repo's markdown"}
    if not target.exists():
        return {"ok": False, "error": rel_file + " does not exist"}

    # Bytes, not text mode: text mode on Windows rewrites every line ending,
    # and one tick in an LF plan would put the whole file in the git diff.
    lines = target.read_bytes().decode("utf-8").splitlines(keepends=True)
    hits = [n for n, ln in enumerate(lines) if ln.rstrip("\r\n") == raw]
    if len(hits) != 1:
        found = "no" if not hits else str(len(hits))
        return {"ok": False, "stale": True,
                "error": found + " matching lines in " + rel_file
                         + " — the file moved on; refresh and try again"}

    n = hits[0]
    line = lines[n]
    if _pr.CHECK.match(line):
        ob = line.index("[")
        cb = line.index("]", ob)
        lines[n] = line[:ob + 1] + MARK[state] + line[cb:]
    else:
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _pr.LIST_ITEM.match(body) or (
            _pr.TABLE_ROW.match(body) if body.startswith("|") else None)
        # A plain list entry is an item only when the model says so (items =
        # "lists", under a phase heading). Any other list line in the repo's
        # markdown is not this endpoint's to edit.
        model = build(root)
        if not m or model.get("items_mode") != "lists" or not any(
                it.get("file") == rel_file and it.get("raw") == raw
                for ph in model["phases"] for it in ph.get("items", [])):
            return {"ok": False, "error": "matched line is not a plan item"}
        if body.startswith("|") and m.group(2) is None:
            # the mark goes at the start of the first cell's text
            s = m.start(3)
            m = None
        # Record the mode BEFORE the first box is written: once a line carries
        # `[x]` the plan has a checkbox, and detection alone would switch the
        # whole plan back to checkbox mode and hide every other entry.
        cfg_now = CFG if own else tomllib.loads((root / "docs" / "progress.toml").read_text(encoding="utf-8"))
        if not (cfg_now.get("project") or {}).get("items"):
            r = _pr.apply_project_edits(root, {"items": "lists"}, dry_run=False)
            if not r.get("ok"):
                return {"ok": False, "error": "could not record items = \"lists\" in "
                        "docs/progress.toml: " + str(r.get("error"))}
            if own:
                CFG.setdefault("project", {})["items"] = "lists"
        if m is None:
            lines[n] = body[:s] + "[" + MARK[state] + "] " + body[s:] + eol
        elif m.group(2) is not None:
            s = m.start(2)
            lines[n] = body[:s] + MARK[state] + body[s + 1:] + eol
        else:
            s = m.start(3)
            lines[n] = body[:s] + "[" + MARK[state] + "] " + body[s:] + eol
    target.write_bytes("".join(lines).encode("utf-8"))

    # Keep the artifact-bound HTML in step. The PostToolUse hook only fires on
    # Claude's edits, and this edit came from a browser. The plan file — the
    # source of truth — is already written above, so a regeneration failure is
    # reported rather than fatal: the tick DID happen.
    r = subprocess.run(_py_for(root, "--quiet"), cwd=str(root), capture_output=True,
                       creationflags=NO_WINDOW)
    out = {"ok": True, "raw": lines[n].rstrip("\r\n")}
    if r.returncode != 0:
        out["warning"] = ("the plan was updated, but regenerating the report failed (rc "
                          f"{r.returncode}): "
                          + (r.stderr or b"")[:200].decode("utf-8", "replace").strip())
        print("  regen after tick: " + out["warning"], file=sys.stderr)
    return out


def _focus_app_window(needle: str, timeout: float = 6.0) -> bool:
    """Pull a visible top-level window of a matching process to the foreground.

    `needle` is matched against the owning process's executable PATH, not its
    title: the packaged Claude app and the Claude Code CLI are both `claude.exe`
    and a title match would be a coin toss, while the path carries the package
    identity.

    SetForegroundWindow is refused for a process that does not own the
    foreground - which is exactly this server, since the click happened in the
    browser. Attaching our input queue to the foreground thread lifts that for
    the duration of the call. This is the documented workaround rather than a
    way around a security boundary: the user asked for this window by pressing
    a button, and the alternative is a page that says "opened" about a window
    still buried behind the browser.

    Windows only. Everything is best-effort: a False return costs the caller
    nothing but a more careful message.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        PROC_QUERY_LIMITED, SW_RESTORE, SW_SHOW = 0x1000, 9, 5

        def exe_of(pid: int) -> str:
            h = k32.OpenProcess(PROC_QUERY_LIMITED, False, pid)
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(32768)
                n = wintypes.DWORD(len(buf))
                return buf.value if k32.QueryFullProcessImageNameW(
                    h, 0, buf, ctypes.byref(n)) else ""
            finally:
                k32.CloseHandle(h)

        def find() -> int:
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _):
                if found or not u32.IsWindowVisible(hwnd):
                    return True
                if not u32.GetWindowTextLengthW(hwnd):
                    return True            # a title-less window is not the app
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if needle.lower() in exe_of(pid.value).lower():
                    found.append(hwnd)
                return True

            u32.EnumWindows(cb, 0)
            return found[0] if found else 0

        # A cold start needs time to put a window up; an app already running is
        # found on the first pass.
        deadline = time.monotonic() + timeout
        hwnd = find()
        while not hwnd and time.monotonic() < deadline:
            time.sleep(0.25)
            hwnd = find()
        if not hwnd:
            return False

        fg = u32.GetForegroundWindow()
        fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = k32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != cur_tid and \
            bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            u32.ShowWindow(hwnd, SW_RESTORE if u32.IsIconic(hwnd) else SW_SHOW)
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(cur_tid, fg_tid, False)
        # Report what HAPPENED, not what was attempted: the window is only
        # focused if it is now the foreground window.
        return bool(u32.GetForegroundWindow() == hwnd)
    except (OSError, AttributeError, ValueError):
        return False


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
    # Placeholders: {pf} the prompt file (already a quoted PowerShell literal),
    # {sid} the phase session id, {sys} the optional pinned brief - all three
    # filled in open_session, which is where launcher and prompt shape meet.
    #
    # `base` groups a tool's launchers; `warm` marks the one that appends to a
    # conversation that already holds the phase context, and so receives the
    # WARM prompt (the item alone) instead of the full brief again.
    if shutil.which("claude"):
        # A COLD launch mints the session id (--session-id) and a WARM one
        # resumes exactly it (--resume <id>) - both verified interactively on
        # this machine - so "continue" means THIS phase's conversation, not
        # whichever one was most recent in the directory (which after a ticket
        # draft or a re-plan is not it). With no id on record the warm launcher
        # falls back to --continue, and the result says so.
        L["claude"] = {"label": "Claude Code — new session", "mode": "terminal",
                       "base": "claude",
                       "cmd": "claude {sys}{agent}--session-id {sid} (Get-Content -Raw -Encoding UTF8 {pf})",
                       "cmd_blank": "claude"}
        L["claude-continue"] = {"label": "Claude Code — continue phase session",
                                "mode": "terminal", "base": "claude", "warm": True,
                                "cmd": "claude {sys}--resume {sid} (Get-Content -Raw -Encoding UTF8 {pf})",
                                "cmd_fallback": "claude {sys}--continue (Get-Content -Raw -Encoding UTF8 {pf})",
                                "cmd_blank": "claude --continue"}
    if shutil.which("opencode"):
        # --prompt, -s/--session and -c/--continue are TUI flags per
        # `opencode --help`; `run -s <id> <msg>` was verified to append a turn to
        # a named session here, the TUI form is taken from --help. opencode mints
        # its own ids, so a cold launch records the session that appears in
        # `opencode session list` for this directory right after it.
        L["opencode"] = {"label": "opencode — new session", "mode": "terminal",
                         "base": "opencode",
                         "cmd": "opencode {agent}--prompt (Get-Content -Raw -Encoding UTF8 {pf})",
                         "cmd_blank": "opencode"}
        L["opencode-continue"] = {"label": "opencode — continue phase session",
                                  "mode": "terminal", "base": "opencode", "warm": True,
                                  "cmd": "opencode -s {sid} --prompt (Get-Content -Raw -Encoding UTF8 {pf})",
                                  "cmd_fallback": "opencode -c --prompt (Get-Content -Raw -Encoding UTF8 {pf})",
                                  "cmd_blank": "opencode -c"}
    if shutil.which("codex"):
        # Positional prompt per the Codex CLI docs. The resume flags are NOT
        # verified against --help on this machine (codex absent here) - if the
        # continue launcher misbehaves, that is the first thing to check. Codex
        # resumes "last" only, so its warm launcher has no id to address.
        L["codex"] = {"label": "Codex - new session", "mode": "terminal",
                      "base": "codex",
                      "cmd": "codex (Get-Content -Raw -Encoding UTF8 {pf})",
                      "cmd_blank": "codex"}
        L["codex-continue"] = {"label": "Codex - resume last session",
                               "mode": "terminal", "base": "codex", "warm": True,
                               "cmd": "codex resume --last (Get-Content -Raw -Encoding UTF8 {pf})",
                               "cmd_blank": "codex resume --last"}
    appid = _detect_claude_app()
    if appid:
        # The publisher hash out of the AppID (Claude_pzs8sxrjxfjjc!Claude)
        # appears in the installed package path, so it identifies the app's own
        # windows across versions - and never matches the Claude Code CLI,
        # which is the same executable name in a different place.
        L["claude-app"] = {"label": "Claude app (prompt → clipboard)",
                           "mode": "clipboard",
                           "focus": appid.split("!")[0].split("_")[-1],
                           "open": ["explorer.exe", "shell:AppsFolder\\" + appid]}
    code_path = shutil.which("code")
    if code_path:
        # Full resolved path on purpose: CreateProcess does not do PATHEXT
        # resolution, so Popen(["code", ...]) cannot find the code.cmd shim.
        L["vscode"] = {"label": "VS Code (repo + prompt → clipboard)",
                       "mode": "clipboard",
                       "focus": "Microsoft VS Code",
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
            # `warm = true` marks a launcher that appends to an existing session
            # (it then receives the warm prompt); `{sid}` in its cmd is filled
            # with the phase session id, and refused when none is recorded.
            # `base` reaches a tab title and picks session behaviour, so it is
            # held to the same charset as an id and hashed when it is declared.
            base = str(c.get("base") or lid)
            if not _ID.match(base):
                print(f"  config: skipping launcher {lid!r} with bad base {base!r}", file=sys.stderr)
                continue
            L[lid] = {"label": c.get("label", lid), "mode": "terminal", "cmd": cmd,
                      "base": base, "warm": bool(c.get("warm"))}
            if c.get("cmd_fallback"):
                L[lid]["cmd_fallback"] = str(c["cmd_fallback"]).replace(
                    "'{pf}'", "{pf}").replace('"{pf}"', "{pf}")
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
        # A launcher's warm flag and fallback change what runs, so they are
        # hashed too - appended only when present, so launchers written before
        # they existed keep the digest they were approved under.
        payload["l"] = {k: (v.get("open") or [v.get("cmd", "")]
                            + ([v["cmd_fallback"]] if v.get("cmd_fallback") else [])
                            + (["warm"] if v.get("warm") else [])
                            + (["base=" + str(v["base"])] if v.get("base") and v["base"] != k else []))
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
    was = REPO
    try:
        init_repo(p)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"{p} has an unreadable config ({exc})"}
    if SERVE_PORT:
        _pr.clear_served(was, os.getpid())
        _pr.mark_served(REPO, SERVE_PORT, os.getpid())
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
    PROMPT_DIR = REPO / _pr.WORK_DIR
    cfgp = REPO / "docs" / "progress.toml"
    # An unconfigured repo must still START, because /setup is the thing that
    # configures it — a wizard you can only reach once you no longer need it
    # would be useless on exactly the machine that needs it.
    CFG = tomllib.loads(cfgp.read_text(encoding="utf-8")) if cfgp.exists() else {}
    global _CFG_STAMP
    _CFG_STAMP = _cfg_stamp()
    ACTIONS.clear()
    ACTIONS.update(build_actions(CFG))
    LAUNCHERS.clear()
    LAUNCHERS.update(build_launchers(CFG))
    _merge_config_launchers(LAUNCHERS, CFG)
    _pr.remember_project(REPO, (CFG.get("project", {}) or {}).get("name", ""))
    # Sync at startup so a session launched seconds later already has its MCP
    # servers. Reported, never silent: this writes a committed file.
    return CFG


_CFG_STAMP: tuple | None = None


def _cfg_stamp() -> tuple | None:
    try:
        st = (REPO / "docs" / "progress.toml").stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def refresh_cfg() -> bool:
    """Reload the config DATA if docs/progress.toml changed on disk since it
    was last read. In place, so every holder of CFG sees it. A file caught
    half-written (unparsable) keeps the previous config and is retried on the
    next request. Returns whether a reload happened."""
    global _CFG_STAMP
    stamp = _cfg_stamp()
    if stamp is None or stamp == _CFG_STAMP:
        return False
    try:
        new = tomllib.loads((REPO / "docs" / "progress.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    CFG.clear()
    CFG.update(new)
    _CFG_STAMP = stamp
    return True


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


# Variables that mark a process as living INSIDE a Claude Code session. A
# terminal opened by this server inherits the server's environment when the
# server itself was started from such a session (a Bash tool, an agent), and
# the launched claude then ran as a child of that session: its turns went to
# the parent's transcript, not to the id on record. Cleared in every launch
# script, so a launched session is a top-level session whoever started us.
_SESSION_LINK_VARS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                      "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                      "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT",
                      "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE")


def _unlink_prelude() -> str:
    return ("foreach($n in " + ",".join("'" + v + "'" for v in _SESSION_LINK_VARS) +
            "){Remove-Item -Path \"env:$n\" -ErrorAction SilentlyContinue}\n")


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


# ------------------------------------------------------------- sessions ---
# One conversation per phase that the dashboard can ADDRESS, recorded outside
# git under WORK_DIR (a coding tool's sessions are per directory anyway, so the
# checkout is the right scope). What is recorded is a launch fact - which id,
# which item was last SENT and when - never progress: the checkboxes remain the
# only store of that, and "last sent" says nothing about done.
_SESS_LOCK = threading.Lock()
PIN_BRIEF_DEFAULT = True     # profile.toml `pin_protocol = false` switches it off


def _single_plan() -> bool:
    return len(_pr.known_plans(CFG)) <= 1


def _sessions_path() -> Path:
    """The session record of the ACTIVE plan. Phase ids repeat across plans,
    so one shared file would send a new plan's Phase 0 to an old plan's
    conversation. A pre-plan `sessions.json` is adopted while the project has
    one plan - it can only be that plan's; with several it is left alone."""
    per = PROMPT_DIR / f"sessions-{_pr.plan_slug(_pr.active_plan(CFG))}.json"
    legacy = PROMPT_DIR / "sessions.json"
    if not per.exists() and legacy.exists() and _single_plan():
        try:
            legacy.replace(per)
        except OSError:
            return legacy
    return per


def load_sessions() -> dict:
    try:
        d = json.loads(_sessions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    # The record is a file under the repo, so a clone could ship one. An id
    # that is not id-shaped is dropped at the boundary and never reaches a
    # command line, a WQL filter or a launch script.
    for ph in list((d.get("phases") or {}).values()):
        for rec in list((ph or {}).values()):
            if not isinstance(rec, dict):
                continue
            if rec.get("id") and not _SID_OK.match(str(rec["id"])):
                rec.pop("id", None)
                rec["id_malformed"] = True
            if rec.get("previous"):
                rec["previous"] = [x for x in rec["previous"]
                                   if isinstance(x, dict) and _SID_OK.match(str(x.get("id") or ""))]
    return d


def _save_sessions(d: dict) -> None:
    PROMPT_DIR.mkdir(exist_ok=True)
    tmp = _sessions_path().with_suffix(".tmp")
    tmp.write_bytes(json.dumps(d, indent=1, sort_keys=True).encode("utf-8"))
    tmp.replace(_sessions_path())


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def session_record(phase_id: str, base: str) -> dict | None:
    return ((load_sessions().get("phases") or {}).get(str(phase_id)) or {}).get(base)


def record_session(phase_id: str, base: str, sid: str | None = None, item: str = "",
                   kind: str = "item", new: bool = False, count: bool = True,
                   via: str = "", route: str | None = None, pending: str = "",
                   on_cmdline: bool | None = None) -> dict:
    """Update the record for (phase, tool) after a launch or a paste.

    `new` retires the current id into previous[] first: the old conversation
    still exists and is not deleted, it is just no longer where Send goes.
    `count` is False when nothing was launched (a paste, a --continue
    fallback); `via` marks a delivery the record cannot vouch for ("paste":
    copied, not yet pasted; "continue": the tool's own latest conversation,
    destination unknown); `route` is how the tab was opened ("wt.exe" names
    a tab, a plain shell does not); `on_cmdline` says whether a live process
    can be recognised by the id at all (opencode chooses its own ids and its
    cold launch carries none).
    """
    with _SESS_LOCK:
        d = load_sessions()
        ph = d.setdefault("phases", {}).setdefault(str(phase_id), {})
        rec = ph.get(base) or {}
        now = _now_iso()
        if new and rec.get("id") and rec.get("id") != sid:
            rec.setdefault("previous", []).insert(
                0, {"id": rec["id"], "started": rec.get("started"), "retired": now})
            rec["previous"] = rec["previous"][:10]
        if new:
            # A fresh session has received nothing yet and is nobody's attachment.
            for k in ("id", "started", "attached", "route", "pending", "id_on_cmdline",
                      "last_sent", "last_sync", "id_malformed"):
                rec.pop(k, None)
            rec["launches"] = 0
        if sid and rec.get("id") != sid:
            rec["id"], rec["started"] = sid, now
            rec.pop("attached", None)
        if route:
            rec["route"] = route
        if pending:
            rec["pending"] = pending
        if on_cmdline is not None:
            rec["id_on_cmdline"] = on_cmdline
        if count:
            rec["launches"] = int(rec.get("launches") or 0) + 1
        stamp = {"item": item or "", "kind": kind, "at": now}
        if via:
            stamp["via"] = via
        if kind == "phase":
            # a re-sync is not an item send - and a COLD phase launch is not a
            # re-sync either: "started" already says what happened
            if not new:
                rec["last_sync"] = stamp
        else:
            rec["last_sent"] = stamp
        ph[base] = rec
        _save_sessions(d)
        return rec


def forget_session(phase_id: str, base: str) -> dict:
    with _SESS_LOCK:
        d = load_sessions()
        rec = ((d.get("phases") or {}).get(str(phase_id)) or {}).get(base)
        if not rec:
            return {"ok": False, "error": f"no {base} session recorded for Phase {phase_id}"}
        if rec.get("id"):
            rec.setdefault("previous", []).insert(
                0, {"id": rec["id"], "started": rec.get("started"), "retired": _now_iso()})
            rec["previous"] = rec["previous"][:10]
        for k in ("id", "started", "last_sent", "last_sync", "attached", "route",
                  "pending", "id_on_cmdline", "id_malformed"):
            rec.pop(k, None)
        rec["launches"] = 0
        _save_sessions(d)
        return {"ok": True}


_SID_OK = re.compile(r"^(?:[0-9a-fA-F-]{36}|ses_[A-Za-z0-9]{6,})$")


def attach_session(phase_id: str, base: str, sid: str) -> dict:
    """Point Send at a conversation the developer started themselves."""
    sid = (sid or "").strip()
    if not _SID_OK.match(sid):
        return {"ok": False, "error": "that does not look like a session id "
                                      "(claude: a uuid; opencode: ses_...)"}
    # Only a tool with a warm launcher that takes an id can be sent to by id.
    resumable = {v.get("base") or k for k, v in LAUNCHERS.items()
                 if v.get("warm") and "{sid}" in str(v.get("cmd", ""))}
    if base not in resumable:
        return {"ok": False, "error": f"no launcher on this machine can resume a {base!r} "
                                      "session by id"}
    with _SESS_LOCK:
        d = load_sessions()
        ph = d.setdefault("phases", {}).setdefault(str(phase_id), {})
        rec = ph.get(base) or {}
        if rec.get("id") and rec["id"] != sid:
            rec.setdefault("previous", []).insert(
                0, {"id": rec["id"], "started": rec.get("started"), "retired": _now_iso()})
            rec["previous"] = rec["previous"][:10]
            # The attached conversation never received what the old one did.
            for k in ("last_sent", "last_sync", "pending", "id_on_cmdline", "id_malformed"):
                rec.pop(k, None)
        rec["id"], rec["started"], rec["launches"] = sid, _now_iso(), 0
        rec["attached"], rec["route"] = True, "attached"
        ph[base] = rec
        _save_sessions(d)
    # Say what was checked, not what was hoped: a uuid with no transcript here
    # will fall back to --continue on the next Send.
    return {"ok": True, "id": sid, "transcript": _transcript_state(base, sid)}


def _procs_with(needles: list[str]) -> dict | None:
    """Which of these ids appear on a LIVE process's command line.

    One WMI query for all of them. The querying shell is excluded - its own
    command line contains every needle. None means the check itself failed,
    which the page shows as "unknown", never as "not running".
    """
    if not needles:
        return {}
    if os.name != "nt":
        return None
    # Ids are id-shaped or they are not looked for: nothing else may be
    # interpolated into a filter that PowerShell will read. The filter is a
    # single-quoted PowerShell literal (no $(...) expansion) and '_' is a WQL
    # wildcard, so it is bracketed.
    needles = [n for n in needles if re.fullmatch(r"[A-Za-z0-9_-]+", n)]
    if not needles:
        return None
    conds = " OR ".join("CommandLine LIKE '%" + n.replace("_", "[_]") + "%'" for n in needles)
    cmd = ("Get-CimInstance Win32_Process -Filter " + _ps_lit(conds) + " | "
           "Where-Object { $_.Name -notmatch '^(powershell|pwsh)' } | "
           "Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=25, creationflags=NO_WINDOW)
        out = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0:
            return None
        rows = json.loads(out) if out else []
        rows = rows if isinstance(rows, list) else [rows]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found: dict[str, list[str]] = {}
    for row in rows:
        cl = str((row or {}).get("CommandLine") or "")
        for n in needles:
            if n in cl:
                found.setdefault(n, []).append(str(row.get("Name")))
    return found


def _claude_transcript(sid: str) -> Path:
    """Where Claude Code keeps this directory's conversations - the layout as
    observed here (~/.claude/projects/<cwd with every non-alnum as '-'>/<id>.jsonl)."""
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(REPO.resolve()))
    return Path.home() / ".claude" / "projects" / slug / (sid + ".jsonl")


def _transcript_state(base: str, sid: str) -> bool | None:
    if base != "claude" or not sid:
        return None          # only Claude's layout is known; unknown, not "missing"
    try:
        return _claude_transcript(sid).exists()
    except OSError:
        return None


def _mint_claude_id() -> str:
    import uuid
    for _ in range(5):
        sid = str(uuid.uuid4())
        if not _claude_transcript(sid).exists():
            return sid
    return str(uuid.uuid4())


def _opencode_ids() -> set[str]:
    """Session ids `opencode session list` shows from this directory."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "opencode session list"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW,
                           cwd=str(REPO))
        return set(re.findall(r"\bses_[A-Za-z0-9]+", r.stdout.decode("utf-8", "replace")))
    except (OSError, subprocess.SubprocessError):
        return set()


def _opencode_dir(sid: str) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "opencode export " + sid],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW,
                           cwd=str(REPO))
        return str((json.loads(r.stdout.decode("utf-8", "replace")).get("info") or {})
                   .get("directory") or "")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return ""


def _discover_opencode(phase_id: str, base: str, before: set[str], pending: str) -> None:
    """opencode names its own sessions: watch the list for one that appeared
    after our launch AND belongs to this directory, then record it. Bounded;
    silence if nothing shows - the warm launcher then falls back honestly.

    `pending` is this launch's token: a later cold launch, a forget or an
    attach replaces or removes it, and a superseded watcher exits without
    writing - so two launches within the window cannot record each other's
    session. The id is marked as not carried by any process: opencode chose
    it, so liveness cannot be read off a command line and is shown as unknown.
    """
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        time.sleep(3)
        new = _opencode_ids() - before
        for sid in sorted(new):
            d = _opencode_dir(sid)
            if d and Path(d).resolve() == REPO.resolve():
                with _SESS_LOCK:
                    cur = load_sessions()
                    ph = cur.setdefault("phases", {}).setdefault(str(phase_id), {})
                    rec = ph.get(base) or {}
                    if rec.get("id") or rec.get("pending") != pending:
                        return
                    rec["id"], rec["started"] = sid, _now_iso()
                    rec["id_on_cmdline"] = False
                    rec.pop("pending", None)
                    ph[base] = rec
                    _save_sessions(cur)
                return


def _split_tag(tag: str) -> tuple[str, str]:
    """'0' -> ('0','phase'); '0-item' -> ('0','item'); '0-ticket' -> ('0','ticket');
    'replan-item-0' -> ('0','replan'); 'replan-plan-all' -> ('all','replan')."""
    if tag.startswith("replan-"):
        parts = tag.split("-", 2)
        return (parts[2] if len(parts) > 2 else "all"), "replan"
    for k in ("item", "ticket"):
        if tag.endswith("-" + k):
            return tag[:-len(k) - 1], k
    return tag, "phase"


def _pin_brief() -> bool:
    prof = _pr.load_user_profile() or {}
    v = prof.get("pin_protocol", PIN_BRIEF_DEFAULT)
    return bool(v) if isinstance(v, bool) else PIN_BRIEF_DEFAULT


def _write_briefs_quietly() -> None:
    """The per-phase brief is what a launcher pins and what /next-item reads;
    keep it current, and never let a brief-writing hiccup block a launch."""
    try:
        _pr.write_briefs(build(REPO), REPO)
    except Exception as exc:          # noqa: BLE001 - reported, never fatal
        print(f"  briefs: not written ({type(exc).__name__}: {exc})", file=sys.stderr)


def _tab_title(phase_id: str, base: str) -> str:
    """The tab a tracked launch is given. Built from sanitised parts: wt.exe
    splits its argv on ';', so nothing repo-authored may reach it raw."""
    tb = "".join(c for c in str(base) if c.isalnum() or c in "-_") or "x"
    # The project's mark first: with two projects open, two "Phase 1 - claude"
    # tabs could not be told apart.
    proj = CFG.get("project") or {}
    mk = _pr.project_mark(str(proj.get("name") or REPO.name), str(proj.get("mark") or ""))
    return f"{mk} \u00b7 Phase {_pr.safe_id(phase_id)} - {tb}"


def _where(phase_id: str, base: str, rec: dict, sid: str) -> str:
    """Where the session is, as far as the record knows: a tab this server
    named, or 'the terminal where it runs' for one it never opened."""
    if (rec or {}).get("route") == "wt.exe":
        return f"the terminal tab '{_tab_title(phase_id, base)}'"
    return f"the terminal where session {sid[:8]}… is running"


def _age_s(iso: str) -> float | None:
    try:
        return time.time() - time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def sessions_view() -> dict:
    """Every recorded phase session with what the page may honestly say about
    it: id, when, what was last SENT, whether a process carrying the id is live
    (or unknown), whether Claude's transcript is on disk, and what Send will do."""
    d = load_sessions()
    phases = d.get("phases") or {}
    ids = [rec["id"] for ph in phases.values() for rec in ph.values()
           if rec.get("id") and rec.get("id_on_cmdline", True)]
    alive = _procs_with(ids) if ids else {}
    warm_of = {v.get("base") or k: k for k, v in LAUNCHERS.items() if v.get("warm")}
    out: dict = {}
    for pid, tools in phases.items():
        for base, rec in tools.items():
            sid = rec.get("id") or ""
            live = None
            if sid and alive is not None and rec.get("id_on_cmdline", True):
                live = bool(alive.get(sid))
            tr = _transcript_state(base, sid) if sid else None
            where = _where(pid, base, rec, sid)
            age = _age_s(rec.get("started") or "") if sid else None
            if not sid:
                will = ("continue the most recent conversation here — PCC cannot tell "
                        "which one" if base in warm_of else "start a new session")
            elif base not in warm_of:
                will = f"nothing — no launcher on this machine can resume a {base} session"
            elif live:
                will = f"paste into {where}"
            elif tr is False and age is not None and age < 120:
                will = (f"wait — session {sid[:8]}… started {int(age)}s ago and has no "
                        "transcript yet; if its tab shows an error, forget it")
            elif tr is False:
                will = ("continue the most recent conversation here: no transcript on disk "
                        f"for the recorded session {sid[:8]}… (it may never have started, "
                        "or was deleted)")
            elif live is None:
                will = (f"resume {sid[:8]}… in a new terminal tab — PCC cannot see "
                        f"whether {where} is still open; close it first, or paste there yourself")
            else:
                will = f"resume {sid[:8]}… in a new terminal tab"
            out.setdefault(str(pid), {})[base] = {
                **rec, "alive": live, "transcript": tr, "send_will": will,
                "warm_launcher": warm_of.get(base), "where": where,
                "tab": _tab_title(pid, base) if rec.get("route") == "wt.exe" else None}
    return {"ok": True, "phases": out}


def open_session(phase_tag: str, prompt: str, tool: str = "claude",
                 blank: bool = False, item: str = "", prompt_warm: str = "") -> dict:
    """Open a development session in the chosen tool, seeded with a prompt.

    Which prompt is decided HERE, where the launcher is known: a cold launcher
    gets `prompt` (the full brief), a warm one gets `prompt_warm` (the delta)
    and is pointed at this phase's recorded session - pasted into its live tab,
    resumed by id in a new tab, or, with nothing on record, the tool's own
    "continue the latest" with a note that says exactly that.

    Both things always happen - the session opens AND the prompt is available
    to paste - and the result says which route delivered it.
    """
    spec = LAUNCHERS.get(tool)
    if spec is None:
        return {"ok": False, "error": "launcher " + repr(tool) + " not available on this machine"}

    phase_id, kind = _split_tag(phase_tag)
    base = str(spec.get("base") or tool)
    warm = bool(spec.get("warm"))
    tracked = kind in ("item", "phase") and not blank and spec["mode"] == "terminal"
    rec = session_record(phase_id, base) if tracked else None
    notes: list[str] = []
    sid = str((rec or {}).get("id") or "")
    if sid and not _SID_OK.match(sid):          # load_sessions drops these; belt and braces
        notes.append(f"the recorded {base} session id for Phase {phase_id} is malformed and "
                     "was ignored — forget it or attach a valid one")
        sid = ""
    text = prompt_warm if (warm and prompt_warm) else prompt

    safe = _pr.safe_id(phase_id)
    PROMPT_DIR.mkdir(exist_ok=True)
    pf = PROMPT_DIR / f"prompt-{safe}-{kind}.txt"
    pf.write_bytes(text.encode("utf-8"))
    copied = False if blank else _copy_clipboard(pf)

    if spec["mode"] != "terminal":
        # clipboard mode: the tool takes no prompt argument, so paste is the delivery.
        try:
            subprocess.Popen(spec["open"], cwd=str(REPO), creationflags=NO_WINDOW)
            # Launching activates the app, but a background process may not take
            # the foreground - so an already-running app just blinked in the
            # taskbar while this said "opened". Pull it forward, and say which.
            focused = _focus_app_window(spec["focus"]) if spec.get("focus") else False
            where = ("— paste it into the session" if focused
                     else "— the app is in your taskbar; paste it there")
            return {"ok": True, "via": spec["open"][0], "tool": tool, "copied": copied,
                    "mode": "clipboard", "focused": focused, "shape": "cold",
                    "note": ("prompt on your clipboard " + where if copied
                             else "opened, but the clipboard copy failed — use `view prompt`")}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}

    import shutil
    cmd = spec.get("cmd_blank") if blank and spec.get("cmd_blank") else spec["cmd"]
    before_oc: set[str] | None = None
    pending, via, count = "", "", True
    if tracked:
        _write_briefs_quietly()
        if warm:
            if sid:
                where = _where(phase_id, base, rec or {}, sid)
                # An id opencode chose is on no command line: liveness unknown.
                alive = _procs_with([sid]) if (rec or {}).get("id_on_cmdline", True) else None
                if alive is None:
                    notes.append(f"could not check whether {where} is still open — if it "
                                 "is, paste the copied prompt there instead of using the new tab")
                elif alive.get(sid):
                    # The conversation is on screen: a second process attached to
                    # the same transcript is not what anyone wants. Paste instead.
                    # Recorded as a paste, not a launch: nothing was launched and
                    # the paste itself is the developer's.
                    focused = _focus_app_window("WindowsTerminal", timeout=2.0)
                    record_session(phase_id, base, item=item, kind=kind, count=False, via="paste")
                    return {"ok": True, "mode": "paste", "tool": tool, "copied": copied,
                            "focused": focused, "session": sid, "shape": "warm",
                            "phase": phase_id, "kind": kind, "where": where, "note": ""}
                if _transcript_state(base, sid) is False:
                    if spec.get("cmd_fallback"):
                        cmd, via, count = spec["cmd_fallback"], "continue", False
                        notes.append(f"no transcript on disk for the recorded session "
                                     f"{sid[:8]}… (it may never have started, or was deleted) "
                                     "— continuing the most recent conversation here instead")
                        sid = ""
                    else:
                        return {"ok": False, "error": f"no transcript on disk for the recorded "
                                f"{base} session {sid[:8]}…; forget it and start a new one"}
            else:
                if spec.get("cmd_fallback"):
                    cmd, via, count = spec["cmd_fallback"], "continue", False
                    notes.append(f"no session recorded for Phase {phase_id} — continuing "
                                 "the most recent conversation here, which may not be it")
                elif "{sid}" in cmd:
                    return {"ok": False, "error": f"no {base} session recorded for Phase "
                            f"{phase_id} — start one first, or attach an id"}
        else:
            if base == "claude" and "{sid}" in cmd:
                sid = _mint_claude_id()
            elif base == "opencode":
                import uuid
                before_oc, pending, sid = _opencode_ids(), uuid.uuid4().hex, ""
            elif "{sid}" in cmd:
                return {"ok": False, "error": f"launcher {tool!r} needs a session id but "
                        "starts a new session; give it a warm sibling or drop {sid}"}
            else:
                sid = ""        # the tool picks its own id; the record must not claim one
    else:
        # Untracked launches (ticket drafts, re-plans, blank sessions) get a
        # usable command and no record: a warm launcher continues the latest
        # conversation, a cold claude template gets a throwaway id, and only a
        # launcher that cannot run without a phase id is refused.
        if warm and spec.get("cmd_fallback") and not blank:
            cmd = spec["cmd_fallback"]
        if base == "claude" and "{sid}" in cmd:
            sid = _mint_claude_id()
        elif "{sid}" in cmd:
            return {"ok": False, "error": f"launcher {tool!r} needs a phase session and this "
                    "launch is not a phase or item"}
        else:
            sid = ""
    cmd_template = cmd

    # Layer 4: pin the generated phase brief as appended system prompt so the
    # protocol survives compaction. Claude Code only (the flag is its), and
    # only when the brief exists - never a flag pointing at nothing.
    sys_arg = ""
    if tracked and base == "claude" and _pin_brief():
        brief = PROMPT_DIR / _pr.brief_name(phase_id)
        if brief.exists():
            sys_arg = "--append-system-prompt-file " + _ps_lit(brief) + " "
    # The plan's agent, on COLD launches of a tool that has agent files: the
    # session starts AS the agent (its body, memory and sources), and the
    # pinned brief still adds the phase's protocol on top. A resumed session
    # keeps the agent it started with. The flag is passed only when the file
    # the tool will look for is there - a name with no file fails at startup.
    agent_arg, agent_name = "", ""
    ag = _pr.plan_agent(CFG) if tracked and not warm else None
    if ag and base in ("claude", "opencode"):
        f = REPO / (".claude" if base == "claude" else ".opencode") / "agents" / f"{ag['name']}.md"
        if f.is_file():
            agent_arg, agent_name = f"--agent {ag['name']} ", ag["name"]
        else:
            notes.append(f"the plan's agent {ag['name']} has no {f.parent.name} file for {base} "
                         "yet - starting without it")
    try:
        cmd = cmd.format(pf=_ps_lit(pf), sid=sid, sys=sys_arg, agent=agent_arg)
    except (KeyError, IndexError, ValueError) as exc:
        return {"ok": False, "error": f"launcher {tool!r} has a bad template: {exc}"}

    # The command goes in a .ps1 run with -File, never on the command line.
    # wt.exe treats `;` as a command separator and PowerShell -Command needs a
    # second level of quoting; a generated prompt or an env prelude hits both.
    # A script file has neither problem.
    body = ("# Generated by the control center. Safe to delete.\n"
            "$ErrorActionPreference = 'Continue'\n" + _unlink_prelude() + _env_prelude()
            + cmd + "\n")
    ps1 = PROMPT_DIR / f"launch-{safe}-{kind}.ps1"
    # utf-8-SIG: Windows PowerShell 5.1 reads a .ps1 as ANSI unless it finds a
    # BOM, which would mangle any non-ASCII path inside it.
    ps1.write_text(body, encoding="utf-8-sig")
    # Do not hard-code pwsh: Windows 11 ships Windows Terminal but PowerShell 7
    # is a separate install, so `pwsh` is often absent while `powershell` works.
    shell = "pwsh" if shutil.which("pwsh") else "powershell"
    # A tracked launch names its tab, so "paste it into the Phase 0 tab" points
    # at something you can see; the app's own title would overwrite it.
    tab = ["--title", _tab_title(phase_id, base), "--suppressApplicationTitle"] if tracked else []
    tried = []
    for argv in (
        ["wt.exe", "-w", "0", "nt", *tab, "-d", str(REPO), shell,
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
            if tracked:
                record_session(phase_id, base, sid=sid or None, item=item, kind=kind,
                               new=not warm, count=count, via=via, route=argv[0],
                               pending=pending,
                               on_cmdline=(True if sid and "{sid}" in cmd_template else None))
                if before_oc is not None:
                    threading.Thread(target=_discover_opencode,
                                     args=(phase_id, base, before_oc, pending),
                                     daemon=True).start()
            return {"ok": True, "via": argv[0], "tool": tool, "copied": copied,
                    "prompt_file": str(pf), "mode": "terminal", "blank": blank,
                    "shape": "warm" if warm else "cold", "session": sid or None,
                    "phase": phase_id, "kind": kind, "pinned": bool(sys_arg),
                    "tab": _tab_title(phase_id, base) if tracked and argv[0] == "wt.exe" else None,
                    "agent": agent_name or None,
                    "note": "; ".join(notes)}
        tried.append(f"{argv[0]}: exited {rc}")
    return {"ok": False, "error":
            "could not start a terminal session (" + "; ".join(tried) + "). The prompt is "
            + ("on your clipboard and " if copied else "") + "in " + str(pf) +
            " — open the tool yourself."}


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
        return {"ok": True, "paths": [], "commits": [], "stat": "", "branch": _pr.git_branch(REPO),
                "note": "add `modules` to this phase to see its commits here"}
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
    return {"ok": True, "paths": paths, "commits": commits, "stat": stat,
            "branch": _pr.git_branch(REPO)}


def replan_prompt(scope: str, phase_id: str, item: str, comment: str,
                  provider_names: list) -> dict:
    """The re-plan prompt: a code session reassesses the plan and EDITS it.

    "Regenerate" only re-read the files; it could never change a plan that had
    drifted from reality. This hands the rethink to a session — which has the
    repo, the docs and the configured context providers — with the requester's
    steering attached, and with hard rules so a reassessment cannot destroy
    history: done stays done, headings stay parseable, and the tool's own
    --check must pass before the session is finished.
    """
    proj = CFG.get("project", {}) or {}
    plan_rel = proj.get("plan", _pr.DEFAULT_PLAN)
    if not isinstance(plan_rel, str):
        plan_rel = _pr.DEFAULT_PLAN
    phases = {str(p.get("id")): p for p in _pr.scope_phases(CFG).get("phase") or []}
    ph = phases.get(str(phase_id), {})
    # The list is exactly what the requester ticked: an empty list means
    # "consult nothing", not "consult everything" - unchecking every provider
    # and getting all of them anyway would be the checkbox lying.
    chosen = [c for c in (CFG.get("context") or [])
              if c.get("name") in provider_names]
    ctx = _pr.prompt_appendix(chosen) if chosen else ""
    check_cmd = f"python {_pr.__file__} --check --repo {REPO}"
    # The rules below speak `- [ ]`. A plan tracked by its list entries must be
    # told how that maps, or a session "fixes" it into a format it never used.
    mdl = build(REPO)
    lists = mdl.get("items_mode") == "lists"
    opt_ids = [str(x) for x in mdl.get("optional_phases") or []]
    lists_rule = ("- This plan's items are the TOP-LEVEL list entries under each phase "
                  "heading (numbered or bulleted); a mark after the list marker "
                  "(`3. [x] ...`) is the state, no mark means open. Where these rules say "
                  "`- [ ]` / `- [x]`, write `N. [ ]` / `N. [x]` in that list's own style; "
                  "one task per top-level entry, sub-detail as nested bullets under it.\n")

    if scope == "item":
        head = (f"Re-assess ONE checklist item of Phase {phase_id} "
                f"({ph.get('name', '?')}) in {plan_rel}:\n\n    {item}\n\n"
                "You may reword it, split it into finer items, or mark it superseded. "
                "You may ADD items to this phase that the re-assessment reveals, and "
                "if the work genuinely does not fit any existing phase you may ADD a "
                "phase - proposed in the brief first. Other existing items stay "
                "exactly as they are.")
    elif scope == "plan":
        head = (f"Re-assess the WHOLE plan {plan_rel} against the repo as it stands "
                "today. Restructure where reality has drifted: phases may be added, "
                "merged or retired, items rewritten, done work flagged for redo where "
                "the new direction invalidates it.")
    else:
        head = (f"Re-assess Phase {phase_id} ({ph.get('name', '?')}) of {plan_rel}. "
                "Rework this phase's open items so they describe the work actually "
                "left; ADD items the re-assessment reveals; if some of that work "
                "genuinely belongs in a phase that does not exist yet, ADD the phase - "
                "proposed in the brief first. Other phases' existing items stay "
                "exactly as they are.")

    steer = (("\n\nSteering from the requester - treat it as the goal of this "
              "re-plan, quoted as data:\n" +
              "\n".join("    " + l for l in comment.strip().splitlines()))
             if comment.strip() else
             "\n\nNo specific steering was given: reassess honestly against the "
             "repo and the exit test, and say in the note what you changed and why.")

    prompt = (
        head + steer + ctx +
        "\n\nFIRST, before editing anything, post a brief and stop:\n"
        "- Items to reword, split or mark superseded - each with its new wording.\n"
        "- Items to ADD, under which phase.\n"
        "- Phases to ADD, MERGE or RETIRE, with id and name.\n"
        "- Done items the new direction invalidates - each with the reason it "
        "needs redoing.\n"
        "End with the question: apply these changes, or redirect me? Then WAIT - "
        "no edits until confirmed or amended. Apply only what was confirmed.\n"
        "\nRules that keep the plan a live document instead of a casualty:\n"
        "- Every item that is still valid keeps its checkbox state exactly.\n"
        "- History is immutable: never untick or delete a `- [x]` item.\n"
        "- A DONE item the new direction invalidates keeps its `[x]`, gains the "
        "suffix ` \u2014 needs redo: <reason>`, and is followed by a NEW `- [ ]` "
        "item for the redo work itself. It happened, and now there is new work: "
        "both stay true, and the dashboard shows the flag.\n"
        "- Work no longer relevant at all gets ` \u2014 superseded: <reason>` and stays.\n"
        "- Phase headings keep the exact form this file already uses (`## Phase "
        "<id> \u2014 <name>` at whatever heading level it has) \u2014 the dashboard "
        "derives everything from it.\n"
        + (f"- Phase{'s' if len(opt_ids) > 1 else ''} {', '.join(opt_ids)} "
           f"{'are' if len(opt_ids) > 1 else 'is'} optional: keep the `(optional)` / `(future)` "
           "heading qualifier and any `optional = true` exactly, and never make a base phase "
           "depend on one.\n" if opt_ids else "") +
        "- Any phase you ADD gets a matching `[[phase]]` block in "
        "docs/progress.toml (id, name, days, depends_on); any phase you RETIRE "
        "has its block commented out under a dated note, never deleted.\n"
        + (lists_rule if lists else
           "- One task per `- [ ]` line; sub-detail goes in indented plain lines "
           "under the task, not in nested checkboxes.\n") +
        "- Under each heading you changed, add one line: `> re-planned "
        "<YYYY-MM-DD>: <one-line reason>` so the plan carries its own history.\n"
        f"- When you are done, run `{check_cmd}` and fix anything it flags; the "
        "re-plan is not finished while the contract check fails.\n"
        "- Do not create tickets, push, or touch anything outside the plan "
        "document and docs/progress.toml."
    )
    return {"ok": True, "prompt": prompt}
    return {"ok": True, "prompt": prompt}


# ------------------------------------------------------------------ the hub --
# Every dashboard can answer "what needs me, across my projects?" without a
# central server. It reads the other registered projects' files directly -
# plan, config, plan-change proposals, Claude transcripts - and finds their
# running dashboards through the ports they record in the projects list,
# confirmed by asking each port which project it serves. Read-only for other
# repos, except the one deliberate write Today offers: ticking an item marked
# for you, which goes through the same verbatim-line tick as everywhere else.

PROTOCOL_ASK = re.compile(r"(?:confirm these steps|apply these changes),\s*or redirect me\?", re.I)
PHASE_ASK = re.compile(r"Phase ([0-9A-Za-z]+) \(([^\n]*)\n\n {4}(\S[^\n]*)")
_WHERE = {"cli": "a terminal", "claude-vscode": "VS Code", "claude-desktop": "the Claude app",
          "claude-jetbrains": "JetBrains"}
HUB_PORTS = range(8765, 8780)


def _pkey(p) -> str:
    try:
        return os.path.normcase(str(Path(str(p)).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def _probe(port: int, timeout: float = 0.4) -> dict | None:
    """Ask a port which project its dashboard serves. Loopback, and never
    through a proxy: a corporate HTTP_PROXY must not see this request."""
    import urllib.request
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f"http://127.0.0.1:{int(port)}/api/whoami", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if isinstance(d, dict) and d.get("repo") else None
    except Exception:  # noqa: BLE001 - "nothing there" is the common answer
        return None


_RUN = {"at": 0.0, "map": {}}
_RUN_LOCK = threading.Lock()


def running_dashboards(force: bool = False) -> dict:
    """{project key: port} for every dashboard answering on this machine: the
    ports the projects list records, plus the usual range (a dashboard started
    by hand on another port, or before ports were recorded)."""
    now = time.time()
    with _RUN_LOCK:
        if not force and now - _RUN["at"] < 8:
            return dict(_RUN["map"])
    ports = set(HUB_PORTS) | {int(e["port"]) for e in _pr.load_projects() if e.get("port")}
    ports.discard(SERVE_PORT)
    found = {_pkey(REPO): SERVE_PORT} if SERVE_PORT else {}
    order = sorted(ports)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        for port, who in zip(order, ex.map(_probe, order)):
            if who:
                found.setdefault(_pkey(who["repo"]), port)
    with _RUN_LOCK:
        _RUN.update(at=now, map=found)
    return dict(found)


def _transcripts_dir(repo: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(Path(repo).resolve()))


def _read_ends(f: Path, head: int = 65536, tail: int = 393216) -> str:
    """The start (where the phase prompt is) and the end (the last turns) of a
    transcript, without reading a long one whole."""
    size = f.stat().st_size
    with open(f, "rb") as fh:
        data = fh.read(min(size, head))
        if size > head:
            fh.seek(max(head, size - tail))
            data += b"\n" + fh.read()
    return data.decode("utf-8", "replace")


def session_states(repo: Path, now: float | None = None) -> list[dict]:
    """This repo's Claude sessions that matter today, read from their
    transcripts wherever they run (a terminal, VS Code, the Claude app):
    "brief" - stopped at the protocol's confirmation question; "turn" - a
    finished reply in the last 12 hours, waiting for you; "working" - written
    to in the last 5 minutes and mid-turn."""
    now = now or time.time()
    out = []
    try:
        files = list(_transcripts_dir(repo).glob("*.jsonl"))
    except OSError:
        return out
    for f in files:
        try:
            age = now - f.stat().st_mtime
            if age > 72 * 3600:
                continue
            text = _read_ends(f)
        except OSError:
            continue
        title = where = phase = item = None
        last_asst, last_kind = None, None
        for ln in text.splitlines():
            if not ln.startswith("{"):
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("isSidechain"):
                continue
            t = o.get("type")
            if t == "ai-title" and o.get("aiTitle"):
                title = str(o["aiTitle"])
            if o.get("entrypoint"):
                where = _WHERE.get(str(o["entrypoint"]), "a Claude session")
            m = o.get("message") if isinstance(o.get("message"), dict) else {}
            c = m.get("content")
            if t == "assistant" and isinstance(c, list):
                last_asst = {"stop": m.get("stop_reason"), "at": str(o.get("timestamp") or ""),
                             "text": " ".join(b.get("text", "") for b in c
                                              if isinstance(b, dict) and b.get("type") == "text")}
                last_kind = "assistant"
            elif t == "user":
                if isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "tool_result"
                                                     for b in c):
                    last_kind = "tool_result"
                    continue
                s = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                last_kind = "user"
                hit = PHASE_ASK.search(s or "")
                if hit:
                    phase, item = hit.group(1), hit.group(3).strip()
        state = None
        if age < 300 and (last_kind in ("user", "tool_result")
                          or (last_kind == "assistant" and last_asst and last_asst["stop"] == "tool_use")):
            state = "working"
        elif last_kind == "assistant" and last_asst and last_asst["stop"] == "end_turn":
            if PROTOCOL_ASK.search(last_asst["text"][-600:]):
                state = "brief"
            elif age < 12 * 3600:
                state = "turn"
        if state:
            out.append({"sid": f.stem, "state": state, "title": title or "a Claude session",
                        "where": where or "a Claude session", "phase": phase or "", "item": item or "",
                        "at": (last_asst or {}).get("at", "")})
    return out


_MODELS: dict = {}
_MODELS_LOCK = threading.Lock()


def _repo_stamp(p: Path) -> tuple:
    files = [p / "docs" / "progress.toml"]
    try:
        cfg = tomllib.loads(files[0].read_text(encoding="utf-8"))
        files.append(p / _pr.active_plan(cfg))
        files += [p / str(x["doc"]) for x in (cfg.get("phase") or []) if x.get("doc")]
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        pass
    out = []
    for f in files:
        try:
            st = f.stat()
            out.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(f), 0, 0))
    return tuple(out)


def _model_of(p: Path) -> dict:
    """build() for any registered project, cached until its files change - or
    for 90 seconds, since dates and the branch move on their own."""
    k, stamp = _pkey(p), _repo_stamp(p)
    with _MODELS_LOCK:
        hit = _MODELS.get(k)
        if hit and hit[0] == stamp and time.time() - hit[1] < 90:
            return hit[2]
    m = build(p)
    with _MODELS_LOCK:
        _MODELS[k] = (stamp, time.time(), m)
    return m


def _registered(path: str) -> dict | None:
    """A project on this machine's list - the only ones Today may act on."""
    if not str(path).strip():
        return None
    k = _pkey(path)
    hit = next((e for e in _pr.load_projects() if _pkey(e["path"]) == k), None)
    if hit is None and k == _pkey(REPO):
        hit = {"path": str(REPO), "name": (CFG.get("project") or {}).get("name", "")}
    return hit


def project_card(e: dict, live: dict, now: float) -> dict:
    """One project as Today sees it: identity, where it stands, and what waits."""
    p = Path(e["path"])
    k = _pkey(p)
    cur = k == _pkey(REPO)
    port = live.get(k)
    card = {"path": str(p), "name": e.get("name") or p.name, "current": cur,
            "running": bool(port) or cur, "url": f"http://127.0.0.1:{port}/" if port else "",
            "configured": (p / "docs" / "progress.toml").exists(), "error": "",
            "color": e.get("color") or _pr.MARK_COLORS[0], "pct": 0, "phase": "", "phase_id": "",
            "finish": "", "waiting": [], "working": [], "coming": []}
    cfg = {}
    if card["configured"]:
        try:
            cfg = tomllib.loads((p / "docs" / "progress.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            card["error"] = f"config unreadable ({exc})"
    proj = cfg.get("project") or {}
    card["name"] = str(proj.get("name") or card["name"])
    card["mark"] = _pr.project_mark(card["name"], str(proj.get("mark") or ""))
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(proj.get("color") or "")):
        card["color"] = proj["color"]
    if not card["configured"] or card["error"]:
        return card
    try:
        m = _model_of(p)
    except Exception as exc:  # noqa: BLE001 - one broken project must not blank Today
        card["error"] = f"{type(exc).__name__}: {exc}"
        return card
    card["pct"] = m.get("overall", 0)
    # a project that has started nothing has no current phase: show the first
    # one that can start
    curp = m.get("current") or next((x for x in m.get("phases", [])
                                     if x.get("startable") and not x.get("optional")), None) or {}
    if curp:
        card["phase_id"] = str(curp.get("id", ""))
        card["phase"] = f"Phase {curp.get('id')} \u00b7 {curp.get('name', '')}"
    card["finish"] = (m.get("pace") or {}).get("finish") or m.get("finish_date") or ""
    names = {str(x["id"]): x["name"] for x in m.get("phases", [])}

    for s in session_states(p, now):
        if s["phase"] and s["item"]:
            what = f"Phase {s['phase']} \u00b7 {s['item'][:140]}"
        elif s["phase"]:
            what = f"Phase {s['phase']} \u00b7 {names.get(s['phase'], '')}"
        else:
            what = s["title"]
        row = {"kind": s["state"], "phase": s["phase"], "title": what, "at": s["at"],
               "resume": f'cd "{p}"; claude --resume {s["sid"]}'}
        if s["state"] == "brief":
            row["detail"] = (f"Its brief waits for your confirmation in {s['where']} \u00b7 "
                             f"\u201c{s['title']}\u201d")
        elif s["state"] == "turn":
            row["detail"] = f"Replied in {s['where']} and waits for you \u00b7 \u201c{s['title']}\u201d"
        else:
            row["detail"] = f"Working in {s['where']} \u00b7 \u201c{s['title']}\u201d"
        (card["working"] if s["state"] == "working" else card["waiting"]).append(row)

    plan = _pr.active_plan(cfg)
    jf = p / _pr.WORK_DIR / _pr.proposals_name(plan)
    st = _prop_state(jf.with_name(jf.name[: -len(".jsonl")] + ".state.json"))
    today = _pr.date.today().isoformat()
    for r in _pr.read_proposals(jf):
        if r["kind"] == "unreadable" or (st.get(r["id"]) or {}).get("status", "open") != "open":
            continue
        pl = _pr.plan_proposal(p, m, r, today)
        card["waiting"].append({
            "kind": "change", "phase": r.get("phase", ""),
            "title": pl["summary"] if pl.get("ok") else (r.get("text") or r.get("target") or r["kind"]),
            "detail": (f"Proposed by {r['from']}" if r.get("from") else "Proposed by a working session")
                      + ("" if pl.get("ok") else " \u00b7 needs judgment: re-plan or dismiss it")})

    marker = str(proj.get("you_marker") or "[You]")
    for ph in m.get("phases", []):
        if not (ph.get("startable") or ph.get("status") == "active"):
            continue
        mine = [i for i in ph.get("items") or [] if i["state"] != "done"
                and i["label"].lower().startswith(marker.lower())]
        for n, i in enumerate(mine[:3]):
            more = len(mine) - 3 if n == 2 and len(mine) > 3 else 0
            card["waiting"].append({
                "kind": "task", "phase": str(ph["id"]),
                "title": i["label"][len(marker):].strip(" :-") or i["label"],
                "detail": f"Phase {ph['id']} \u00b7 marked {marker} in the plan, so no session takes it"
                          + (f" \u00b7 {more} more like it in this phase" if more else ""),
                "file": i.get("file") or "", "raw": i.get("raw") or ""})

    for r in m.get("risks") or []:
        if r.get("severity") == "critical" and "blocker" in str(r.get("source", "")):
            card["coming"].append({"kind": "blocker", "phase": "",
                                   "title": str(r.get("risk", "")).split(" \u2014 ")[0],
                                   "detail": str(r.get("detail", ""))})
            if sum(1 for x in card["coming"] if x["kind"] == "blocker") >= 2:
                break
    live_ids = {str(x["id"]) for x in m.get("phases", []) if x.get("startable") or x.get("status") == "active"}
    for ph in m.get("phases", []):
        un = [str(x) for x in ph.get("blocked_by") or []]
        if un and set(un) <= live_ids and not ph.get("optional"):
            card["coming"].append({"kind": "next", "phase": str(ph["id"]),
                                   "title": f"Phase {ph['id']} \u00b7 {ph['name']}",
                                   "detail": f"Unlocks when Phase {', Phase '.join(un)} is done"})
            break
    return card


def today_view() -> dict:
    """Every project on this machine's list, as Today and the project bar show it."""
    now = time.time()
    live = running_dashboards()
    entries = _pr.ensure_colors()
    if not any(_pkey(e["path"]) == _pkey(REPO) for e in entries):
        entries = [{"path": str(REPO), "name": (CFG.get("project") or {}).get("name", "")}] + entries
    seen, cards = set(), []
    for e in entries:
        k = _pkey(e["path"])
        if k in seen or not Path(e["path"]).is_dir():
            continue
        seen.add(k)
        cards.append(project_card(e, live, now))
    return {"ok": True, "generated": _now_iso(), "projects": cards,
            "totals": {"waiting": sum(len(c["waiting"]) for c in cards),
                       "working": sum(len(c["working"]) for c in cards),
                       "projects_waiting": sum(1 for c in cards if c["waiting"])}}


def today_tick(body: dict) -> dict:
    """Mark done from Today: the same verbatim-line tick, in the item's repo."""
    e = _registered(str(body.get("path", "")))
    if not e:
        return {"ok": False, "error": "that project is not on this machine's list"}
    r = tick(str(body.get("file", "")), str(body.get("raw", "")), str(body.get("state", "done")),
             repo=Path(e["path"]))
    with _MODELS_LOCK:
        _MODELS.pop(_pkey(e["path"]), None)
    return r


def _free_port(exclude: set) -> int | None:
    import socket
    for port in range(8765, 8800):
        if port in exclude:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return None


def start_dashboard(path: str) -> dict:
    """Start another project's dashboard in a window of its own: visible, so
    the one-time approval of its commands can be answered there, and stopped
    with Ctrl-C like one you started by hand."""
    e = _registered(path)
    if not e:
        return {"ok": False, "error": "that project is not on this machine's list - open it once "
                                      "from Setup > Projects"}
    p = Path(e["path"])
    live = running_dashboards(force=True)
    if _pkey(p) in live:
        return {"ok": True, "url": f"http://127.0.0.1:{live[_pkey(p)]}/", "already": True}
    port = _free_port(set(live.values()) | {SERVE_PORT})
    if port is None:
        return {"ok": False, "error": "no free port between 8765 and 8799"}
    argv = [sys.executable, str(SELF_DIR / "progress-serve.py"), "--repo", str(p),
            "--port", str(port), "--no-open"]
    cmd = " ".join(f'"{a}"' if " " in a else a for a in argv)
    name = re.sub(r"[^A-Za-z0-9 _.-]", "", str(e.get("name") or p.name))[:40] or "project"
    try:
        if os.name == "nt":
            import shutil
            wt = shutil.which("wt.exe")
            if wt and ";" not in str(p):
                subprocess.Popen([wt, "-w", "0", "nt", "--title", f"{name} control center",
                                  "-d", str(p), *argv], creationflags=NO_WINDOW)
            else:
                subprocess.Popen(argv, cwd=str(p), creationflags=NEW_CONSOLE)
        else:
            log = p / _pr.WORK_DIR / "dashboard.log"
            log.parent.mkdir(exist_ok=True)
            subprocess.Popen(argv, cwd=str(p), stdin=subprocess.DEVNULL, stdout=open(log, "ab"),
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "error": f"could not start it ({exc}); run it yourself: {cmd}"}
    deadline = time.time() + 12
    while time.time() < deadline:
        who = _probe(port, 0.5)
        if who and _pkey(who["repo"]) == _pkey(p):
            with _RUN_LOCK:
                _RUN["at"] = 0.0
            return {"ok": True, "url": f"http://127.0.0.1:{port}/"}
        time.sleep(0.4)
    return {"ok": False, "pending": True,
            "error": f"started in a new window, but nothing answers on :{port} yet - if it asks you to "
                     f"approve the project's commands, answer there, then reload. Or run: {cmd}"}


def today_page(token: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Today \u00b7 Control Center</title><style>' + _pr.CSS + CSS + '</style></head><body>'
            '<div class="wrap"><div id="pcc-today"><p class="quiet">Reading your projects\u2026</p></div></div>'
            '<script>window.__ANU_TOKEN__=' + _pr.js(token) + ';window.__PCC_HUB_HERE__="today";</script>'
            '<script>' + TODAY_JS + '</script></body></html>')


def fresh_stamp() -> str:
    """One string that changes when the plan changes on disk.

    A git pull rewrites the plan under a page that was rendered from the old
    one; every read on this dashboard already comes from disk, so the only
    missing piece is the page NOTICING. Mtimes of the files the render depends
    on, hashed - cheap enough to poll."""
    parts = []
    proj = CFG.get("project", {}) or {}
    plan_rel = proj.get("plan", _pr.DEFAULT_PLAN)
    if not isinstance(plan_rel, str):
        plan_rel = _pr.DEFAULT_PLAN     # plan = 3 is legal TOML; a 500ing poll is not
    watch = [REPO / "docs" / "progress.toml", REPO / plan_rel]
    for p in CFG.get("phase", []) or []:
        if p.get("doc"):
            watch.append(REPO / str(p["doc"]))
    for f in watch:
        try:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f.name + ":absent")
    import hashlib
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


# ------------------------------------------------------------ plan proposals --
# Rule 5's other half: sessions append proposals, this page applies them. The
# proposals file is the sessions' (append-only); the verdicts live beside it in
# a state file only this server writes, under one lock with the plan edit.
_PROP_LOCK = threading.Lock()


def _proposal_paths() -> tuple[Path, Path, str]:
    plan = _pr.active_plan(CFG)
    name = _pr.proposals_name(plan)
    wd = REPO / _pr.WORK_DIR
    return wd / name, wd / (name[: -len(".jsonl")] + ".state.json"), plan


def _prop_state(sf: Path) -> dict:
    try:
        d = json.loads(sf.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_prop_state(sf: Path, d: dict) -> None:
    sf.parent.mkdir(exist_ok=True)
    tmp = sf.with_name(sf.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, sf)


def proposals_stamp() -> str:
    """Changes when a session appends a proposal or a verdict is written; the
    freshness poll carries it so the page refreshes the list, not the page."""
    jf, sf, _ = _proposal_paths()
    parts = []
    for f in (jf, sf):
        try:
            st = f.stat()
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def _regen_after_edit(out: dict) -> dict:
    """Keep the artifact-bound HTML in step after a plan edit from the page."""
    r = subprocess.run(_py("--quiet"), cwd=str(REPO), capture_output=True,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        out["warning"] = ("the plan was updated, but regenerating the report failed (rc "
                          f"{r.returncode}): "
                          + (r.stderr or b"")[:200].decode("utf-8", "replace").strip())
    return out


def proposals_view() -> dict:
    """Every proposal for the active plan, open ones with the exact edit Apply
    would make (or why it cannot), handled ones with their verdict."""
    jf, sf, plan = _proposal_paths()
    recs = _pr.read_proposals(jf)
    state = _prop_state(sf)
    model = build(REPO) if recs else {}
    names = {str(p["id"]): p.get("name", "") for p in model.get("phases", [])}
    today = _pr.date.today().isoformat()
    out = []
    for r in recs:
        st = state.get(r["id"]) or {}
        v = dict(r, status=st.get("status", "open"), handled_at=st.get("at", ""),
                 summary=st.get("summary", ""))
        if v["status"] == "open":
            pl = _pr.plan_proposal(REPO, model, r, today)
            if pl["ok"]:
                v.update(summary=pl["summary"], ops=pl["ops"], digest=pl["digest"])
            else:
                v["problem"] = pl["problem"]
        v["phase_name"] = names.get(v.get("phase", ""), "")
        out.append(v)
    try:
        rel = jf.relative_to(REPO).as_posix()
    except ValueError:
        rel = str(jf)
    return {"ok": True, "file": rel, "plan": plan,
            "open": sum(1 for v in out if v["status"] == "open"), "items": out}


def apply_proposal(pid: str, digest: str) -> dict:
    """The confirm of Apply change: re-derive the edit against the files as
    they are now and write it only if it is still the one that was previewed."""
    if not digest:
        return {"ok": False, "error": "apply confirms a previewed change - open its preview first"}
    with _PROP_LOCK:
        jf, sf, _ = _proposal_paths()
        rec = next((r for r in _pr.read_proposals(jf) if r["id"] == pid), None)
        if rec is None:
            return {"ok": False, "stale": True, "error": "no such proposal - the list moved on"}
        state = _prop_state(sf)
        status = (state.get(pid) or {}).get("status", "open")
        if status != "open":
            return {"ok": False, "stale": True, "error": f"this proposal is already {status}"}
        pl = _pr.plan_proposal(REPO, build(REPO), rec, _pr.date.today().isoformat())
        if not pl["ok"]:
            return {"ok": False, "stale": True, "error": pl["problem"]}
        if pl["digest"] != digest:
            return {"ok": False, "stale": True,
                    "error": "the plan changed since this preview - review the change again"}
        for rel, text in pl["files"].items():
            (REPO / rel).write_bytes(text.encode("utf-8"))
        state[pid] = {"status": "applied", "at": _now_iso(), "summary": pl["summary"],
                      "ops": pl["ops"]}
        _save_prop_state(sf, state)
    return _regen_after_edit({"ok": True, "summary": pl["summary"], "files": sorted(pl["files"])})


def undo_proposal(pid: str) -> dict:
    with _PROP_LOCK:
        jf, sf, _ = _proposal_paths()
        state = _prop_state(sf)
        st = state.get(pid) or {}
        if st.get("status") != "applied":
            return {"ok": False, "error": "only an applied change can be undone"}
        r = _pr.undo_ops(REPO, st.get("ops") or [])
        if not r["ok"]:
            return {"ok": False, "error": r["problem"]}
        for rel, text in r["files"].items():
            (REPO / rel).write_bytes(text.encode("utf-8"))
        state.pop(pid, None)            # back to open: apply again, or dismiss
        _save_prop_state(sf, state)
    return _regen_after_edit({"ok": True, "summary": st.get("summary", ""),
                              "warnings": r["warnings"]})


def mark_proposals(ids: list, status: str) -> dict:
    """Dismiss, hand to re-plan, or reopen. Never touches the plan; an applied
    change is reversed with Undo, not reopened."""
    if status not in ("dismissed", "replan", "open"):
        return {"ok": False, "error": "unknown status " + repr(status)}
    with _PROP_LOCK:
        jf, sf, _ = _proposal_paths()
        known = {r["id"] for r in _pr.read_proposals(jf)}
        state = _prop_state(sf)
        n = 0
        for pid in [str(x) for x in ids]:
            if pid not in known:
                continue
            cur = (state.get(pid) or {}).get("status", "open")
            if status == "open" and cur in ("dismissed", "replan"):
                state.pop(pid, None)
                n += 1
            elif status != "open" and cur == "open":
                state[pid] = {"status": status, "at": _now_iso()}
                n += 1
        if n:
            _save_prop_state(sf, state)
    return {"ok": True, "changed": n}


PLAN_ID = "_plan"          # ticket target meaning "the active plan", not a phase


def plan_ticket_prompt(model: dict, project: str, out: Path, cap: int = 2000) -> str:
    """The drafting prompt for ONE ticket covering the whole plan: the same
    work-order discipline as a phase ticket, with the phases as its scope."""
    plan = model["project"].get("plan", "the plan")
    rows = []
    for p in model.get("phases", []):
        open_n = sum(1 for i in p.get("items", []) if i["state"] != "done")
        rows.append(f"  - Phase {p['id']} - {p['name']}: {open_n} of {p['total']} items open"
                    + ("; optional, not part of the plan's finish" if p.get("optional") else "")
                    + (f"; exit test: {p['exit_test']}" if p.get("exit_test") else ""))
    return (
        f"Draft ONE JIRA ticket for the whole plan {plan} "
        f"({model['project'].get('name', '')}).\n\n"
        f"Read {plan} for context first - then write a short work order, not a summary of "
        "the plan. The point of reading is that the scope and the acceptance criteria "
        "are TRUE.\n\n"
        "The plan's phases:\n" + ("\n".join(rows) or "  (none)") + "\n"
        + (f"JIRA project key: {project}\n" if project else "")
        + "\nWrite EXACTLY this structure into the description, in this order, using these "
        "headings verbatim:\n\n"
        "Goal\n"
        "- 1 to 2 bullets: what the plan delivers when it is done.\n"
        "Scope\n"
        "- One bullet per phase, in plan order: 'Phase <id> - <name>: <what it delivers>'. "
        "One line each.\n"
        "Out of scope\n"
        "- 0 to 3 bullets. Things a reader would otherwise assume are included.\n"
        "Acceptance criteria\n"
        "- 3 to 7 bullets, each decidable by INSPECTING A NAMED THING or RUNNING A NAMED "
        "COMMAND. One assertion per bullet. No judgement words (appropriate, properly, "
        "correctly, clean, secure).\n"
        "Blockers\n"
        "- 0 to 3 bullets, only things that genuinely stop the work starting.\n"
        "Open questions\n"
        "- 0 to 3 bullets, one line each, only where the answer changes what gets built. "
        "Omit the heading if there are none.\n\n"
        f"HARD LIMITS. summary: one imperative line, at most 80 characters. description: "
        f"at most {cap} characters TOTAL - count it, and cut until it fits. Every bullet "
        "one line. No sub-bullets.\n\n"
        "Do NOT include: a summary of the repository's state, file inventories, rationale, "
        "quotations, or notes about your own process.\n\n"
        f"Write the result as JSON to {out} with exactly two keys, summary and description, "
        "serialised with a JSON library so the newlines in the description are escaped.\n\n"
        "Write ONLY that file. Do not create the ticket, do not call any JIRA API, and do "
        "not modify the plan - a person reviews this draft and submits it."
    )


def _write_keep_eol(path: Path, text: str) -> None:
    """Write a config edit in the file's own line endings."""
    try:
        eol = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
    except OSError:
        eol = "\n"
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8"))


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
        f"order. Sources: {doc}, "
        f"and the code under {', '.join(ph.get('modules') or ['the repo'])}. The point of "
        "reading is that the scope and the acceptance criteria are TRUE, not that the "
        "ticket recounts what you read.\n\n"

        f"Exit test for the phase: {ph.get('exit_test') or 'none written yet'}\n"
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
    """Where the ACTIVE plan's draft for this phase lives - named for the plan,
    because phase ids repeat across plans and a draft is about one of them."""
    return (PROMPT_DIR / f"ticket-{_pr.plan_slug(_pr.active_plan(CFG))}-"
                         f"{_pr.safe_id(phase_id)}.json")


def _legacy_draft_path(phase_id: str) -> Path | None:
    """A draft written before drafts were per plan. Only while the project has
    one plan can it be known to belong to it; with several it stays unread."""
    p = PROMPT_DIR / f"ticket-{_pr.safe_id(phase_id)}.json"
    return p if _single_plan() and p.exists() else None


def draft_ticket(phase_id: str, tool: str, model: dict) -> dict:
    """Ask a CODING SESSION to draft a ticket for this phase.

    Deliberately not an LLM call from here. The session already has the repo,
    the plan, the phase doc and every configured context provider, and it
    already routes through whichever model you set up — so the dashboard stays
    a stdlib renderer with no model client, no second model configuration and
    no additional credential to hold. It hands over a prompt; the session writes
    the JSON; this reads it back.
    """
    is_plan = phase_id == PLAN_ID
    ph = next((p for p in model.get("phases", []) if str(p["id"]) == str(phase_id)), None)
    if ph is None and not is_plan:
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
    jira_cfg = (CFG.get("integrations", {}) or {}).get("jira", {}) or {}
    project = jira_cfg.get("project_key") or jira_cfg.get("project") or ""
    if is_plan:
        cap = int(jira_cfg.get("draft_max_chars_plan", 2000) or 2000)
        prompt = plan_ticket_prompt(model, project, out, cap)
    else:
        open_items = [i["label"] for i in ph.get("items", []) if i["state"] != "done"]
        doc = ph.get("source") or f"the Phase {ph['id']} section of the plan"
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
        p = _legacy_draft_path(phase_id) or p
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


def _jira_auth_header(t: dict) -> tuple[str, str]:
    """(Authorization header, "") or ("", error). The token is read on demand
    and never returned to the page."""
    token = _read_secret(t["auth_env"])
    if not token:
        return "", f"${t['auth_env']} is not set"
    if t["auth_mode"] == "basic":
        user = str(((CFG.get("integrations", {}) or {}).get("jira", {}) or {}).get("auth_user", ""))
        if not user:
            return "", "auth_mode = basic needs [integrations.jira].auth_user"
        import base64
        return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode(), ""
    return "Bearer " + token, ""


_STANDUP_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}|latest)\.html$")


def standup_file(name: str) -> Path | None:
    """docs/standups/<date>.html, or the newest for 'latest'. None if absent
    or the name is not a date - nothing else under docs/ is served this way."""
    m = _STANDUP_NAME.match(name or "")
    if not m:
        return None
    d = REPO / "docs" / "standups"
    if m.group(1) == "latest":
        files = sorted(d.glob("????-??-??.html")) if d.is_dir() else []
        return files[-1] if files else None
    p = d / name
    return p if p.is_file() else None


def attach_standup(name: str) -> dict:
    """Attach a standup HTML to the plan's ticket and leave a one-line comment.

    Outward-facing: the caller confirmed it in the UI, the button names the
    ticket, and the response says exactly what JIRA created. The file is the
    one on disk - what you opened is what gets attached.
    """
    key = _pr.plan_ticket(CFG)
    if not key:
        return {"ok": False, "error": "this plan has no linked ticket - link one in the plan row first"}
    if key.startswith("http"):
        key = key.rstrip("/").rsplit("/", 1)[-1]
    p = standup_file(name)
    if p is None:
        return {"ok": False, "error": f"no standup file {name!r} - run Standup first"}
    t = jira_target()
    if not t["configured"]:
        return {"ok": False, "error": "JIRA API is not configured: " + "; ".join(t["missing"])}
    auth, err = _jira_auth_header(t)
    if err:
        return {"ok": False, "error": err}
    import urllib.error
    import urllib.request
    import uuid
    boundary = "----pcc" + uuid.uuid4().hex
    fname = f"standup-{p.stem}.html"
    data = p.read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n").encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    base, v = t["base"], t["api_version"]
    req = urllib.request.Request(f"{base}/rest/api/{v}/issue/{key}/attachments", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json",
        "X-Atlassian-Token": "no-check", "Authorization": auth, "User-Agent": "progress-control-center"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            created = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace"))
            msg = "; ".join(detail.get("errorMessages", []) or [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()])
        except (ValueError, OSError):
            msg = ""
        hint = (" - attachments may be disabled for this project, or the token lacks Create Attachments"
                if exc.code == 403 else "")
        return {"ok": False, "error": f"JIRA said {exc.code} {exc.reason}" + (f" - {msg}" if msg else "") + hint}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"could not reach {base}: {exc.reason}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    att = created[0] if isinstance(created, list) and created else {}
    # A short comment so the attachment is found from the ticket's timeline.
    try:
        s = _pr.standup_data(build(REPO), 1)
        line = (f"Standup {s['today']} attached ({fname}): overall {s['overall']}%, "
                f"{len(s['completed'])} done, {len(s['started'])} started, "
                f"{s['remaining_days']}d remaining to {s['finish_date']}.")
        cbody = {"body": _adf(line) if v >= 3 else line}
        creq = urllib.request.Request(f"{base}/rest/api/{v}/issue/{key}/comment", data=json.dumps(cbody).encode("utf-8"),
                                      method="POST", headers={"Content-Type": "application/json", "Accept": "application/json",
                                                              "Authorization": auth, "User-Agent": "progress-control-center"})
        with urllib.request.urlopen(creq, timeout=30):
            commented = True
    except Exception as exc:          # noqa: BLE001 - the attachment succeeded; say the comment did not
        commented = False
        comment_error = f"{type(exc).__name__}: {exc}"
    browse = ((CFG.get("integrations", {}) or {}).get("jira", {}) or {}).get("browse_url", "")
    return {"ok": True, "key": key, "filename": att.get("filename", fname), "size": att.get("size", len(data)),
            "url": browse.replace("{key}", key) if browse else f"{base}/browse/{key}",
            "content": att.get("content", ""), "commented": commented,
            **({} if commented else {"comment_error": comment_error})}


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
    if phase_id == PLAN_ID:
        have = _pr.plan_ticket(CFG)
        if have:
            return {"ok": False, "error": f"this plan already has ticket {have} - use "
                    "Unlink first if you meant to raise a different one"}
    else:
        ph_cfg = next((p for p in _pr.scope_phases(CFG).get("phase") or []
                       if str(p.get("id")) == str(phase_id)), None)
        if ph_cfg is None:
            return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r}"}
        if ph_cfg.get("jira"):
            return {"ok": False, "error": f"phase {phase_id} already has ticket "
                    f"{ph_cfg['jira']} — use Unlink on this phase first if you "
                    "meant to raise a different one"}
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
        # The single most common 401/403 here is not permissions at all: a
        # Cloud site with auth_mode = bearer. Cloud API tokens only work as
        # basic auth with the ACCOUNT EMAIL - and this exact misconfiguration
        # sat in a real config and produced a generic "check permissions".
        hint = " (check project_key, issue_type and the token's permissions)"
        if exc.code in (401, 403) and ".atlassian.net" in t["base"]                 and not auth.startswith("Basic "):
            hint = (" — this is a JIRA Cloud site, and Cloud rejects bearer "
                    "tokens. Set your Account email in /setup → This project "
                    "→ Advanced and press Save config: the wizard flips "
                    "auth_mode to basic automatically.")
        elif exc.code == 401 and auth.startswith("Basic "):
            hint = (" — basic auth was refused: the account email and the "
                    "token in $" + t.get("auth_env", "JIRA_PAT") +
                    " must belong to the same Atlassian account.")
        elif exc.code not in (400, 401, 403):
            hint = ""
        return {"ok": False, "error": f"JIRA said {exc.code} {exc.reason}" +
                (f" — {msg}" if msg else "") + hint}
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
        if phase_id == PLAN_ID:
            new = _pr.set_toml_key(text, _pr.plan_header(_pr.active_plan(CFG)), "jira", key)
        else:
            new = _pr.set_phase_key(text, str(phase_id), "jira", key, plan=_pr.active_plan(CFG))
        tomllib.loads(new)                      # never write a file we just broke
        _write_keep_eol(cfgp, new)
    except KeyError:
        return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r} in progress.toml"}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    CFG.clear()
    CFG.update(tomllib.loads(cfgp.read_text(encoding="utf-8")))
    return {"ok": True, "key": key, "path": str(cfgp)}


def unlink_ticket(phase_id: str) -> dict:
    """Remove a phase's ticket link. The ISSUE is untouched - only the pointer.

    Deliberately not two-step, unlike creating: creating a ticket is outward
    facing and cannot be undone, while this edits one line of a local file that
    git already tracks, and the button names the key it will remove before you
    press it.
    """
    if phase_id == PLAN_ID:
        had = _pr.plan_ticket(CFG)
        if not had:
            return {"ok": False, "error": "this plan has no ticket linked"}
    else:
        ph_cfg = next((p for p in _pr.scope_phases(CFG).get("phase") or []
                       if str(p.get("id")) == str(phase_id)), None)
        if ph_cfg is None:
            return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r}"}
        had = str(ph_cfg.get("jira", "") or "")
        if not had:
            return {"ok": False, "error": f"phase {phase_id} has no ticket linked"}
    cfgp = REPO / "docs" / "progress.toml"
    try:
        import datetime
        text = cfgp.read_text(encoding="utf-8")
        note = f"unlinked {datetime.date.today().isoformat()}"
        if phase_id == PLAN_ID:
            new = _pr.del_toml_key(text, _pr.plan_header(_pr.active_plan(CFG)), "jira", note)
        else:
            new = _pr.del_phase_key(text, str(phase_id), "jira", note,
                                    plan=_pr.active_plan(CFG))
        tomllib.loads(new)                      # never write a file we just broke
        _write_keep_eol(cfgp, new)
    except KeyError:
        return {"ok": False, "error": f"no [[phase]] with id = {phase_id!r} in progress.toml"}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    CFG.clear()
    CFG.update(tomllib.loads(cfgp.read_text(encoding="utf-8")))
    return {"ok": True, "was": had, "path": str(cfgp)}


# ---------------------------------------------------------------- action layer

CSS = """
#pcc-bar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;gap:8px;align-items:center;
 flex-wrap:wrap;padding:10px 14px;background:var(--panel);border-top:1px solid var(--line);
 box-shadow:0 -6px 24px -18px rgba(0,0,0,.6)}
.replanbox{margin-top:10px;padding:10px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--panel-2)}
.replanbox textarea{width:100%;box-sizing:border-box;font:inherit;font-size:13px;
  padding:8px 10px;border:1px solid var(--line);border-radius:7px;
  background:var(--panel);color:var(--ink);resize:vertical}
.replanbox .rprovs{display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  margin-top:8px;font-size:12.5px;color:var(--ink-2)}
.replanbox .rprovs label{display:inline-flex;align-items:center;gap:5px;
  cursor:pointer;min-height:24px}
.replanbox .rbar{display:flex;gap:8px;margin-top:10px}
#pcc-replan-host{position:fixed;right:16px;bottom:54px;width:min(460px,90vw);z-index:60}
#pcc-replan-host .replanbox{box-shadow:var(--shadow);background:var(--panel)}
#pcc-bar .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
 color:var(--ink-3);margin-right:2px}
.pcc-btn{appearance:none;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
 font:inherit;font-size:12.5px;padding:6px 12px;border-radius:7px;cursor:pointer}
.pcc-btn:hover{border-color:var(--accent);color:var(--accent)}
.pcc-btn[disabled]{opacity:.5;cursor:progress}
.pcc-btn.run{border-color:var(--accent);background:var(--accent);color:#fff}
.pcc-btn.run:hover{color:#fff;filter:brightness(1.08)}
/* ---- project bar, Today, identity (multi-project) ---- */
#pcc-bar{background:var(--panel);border-bottom:1px solid var(--line)}
.pcc-bar-in{max-width:1180px;margin:0 auto;padding:0 24px;display:flex;gap:2px;align-items:stretch;overflow-x:auto}
.pcc-tab{display:flex;align-items:center;gap:8px;padding:0 14px;min-height:46px;text-decoration:none;
 color:var(--ink);border-bottom:3px solid transparent;white-space:nowrap;font-size:14px}
.pcc-tab:hover{background:var(--panel-2);color:var(--ink)}
.pcc-tab.on{font-weight:600}
.pcc-tab .quiet{font-size:12px}
#pcc-msg{max-width:1180px;margin:0 auto;padding:6px 24px}
#pcc-msg:empty{display:none}
.pmark{display:inline-flex;align-items:center;justify-content:center;flex:none;border-radius:7px;
 color:#fff;font-weight:700;font-family:var(--mono);font-size:10px;line-height:1}
.pcount{font-size:11.5px;padding:1px 7px;border-radius:999px;background:var(--warn-soft);color:var(--warn)}
.pidrow{display:flex;gap:14px;align-items:center}
.pcc-here{margin:0 0 16px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;
 background:var(--warn-soft);display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.pcc-here p{margin:0}
.pcc-here .acts,.today .acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.today{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start}
.today main{flex:999 1 600px;min-width:0}
.today aside{flex:1 1 280px;min-width:0}
.today h1{margin:0}
.tdsub{color:var(--ink-2);margin:2px 0 14px}
.inbox{list-style:none;margin:0 0 16px;padding:0;border:1px solid var(--line);border-radius:10px;
 background:var(--panel);overflow:hidden}
.inbox .grp{padding:12px 16px 9px;background:var(--panel-2);border-top:1px solid var(--line);
 display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.inbox .grp:first-child{border-top:0}
.inbox .grp h2{margin:0;font-weight:400}
.inbox .row{display:flex;flex-wrap:wrap;gap:10px 12px;padding:14px 16px;border-top:1px solid var(--line);align-items:flex-start}
.inbox .row .txt{flex:1 1 200px;min-width:0}
.inbox .row .acts{margin-left:auto}
.inbox .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:baseline;font-size:12.5px;color:var(--ink-2)}
.inbox .meta a{color:inherit}
.inbox .ttl{margin:5px 0 0;font-weight:600}
.inbox .det{margin:0;color:var(--ink-2);font-size:13px}
.kpill{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--todo-soft);color:var(--ink-2)}
.kpill.wait{background:var(--warn-soft);color:var(--warn)}
.kpill.live{background:var(--done-soft);color:var(--done)}
.kpill.crit{background:var(--crit-soft);color:var(--crit)}
.kpill.info{background:var(--accent-soft);color:var(--accent)}
.tdempty{padding:24px;border:1px solid var(--line);border-radius:10px;background:var(--panel);margin-bottom:16px}
.tdempty h2{margin:0 0 6px;font-size:17px}
.projs{border:1px solid var(--line);border-radius:10px;background:var(--panel);padding:14px 16px 4px}
.projs h2{margin:0;font-weight:400}
.projs ul{list-style:none;margin:8px 0 0;padding:0}
.projs li{padding:12px 0;border-top:1px solid var(--line)}
.projs .prow{display:flex;gap:10px;align-items:center}
.projs .pname{flex:1;font-weight:600;color:var(--ink)}
.projs .pinfo{margin:8px 0 0 38px}
.projs .pinfo p{margin:5px 0 0;font-size:12px}
.pbar{height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.pbar span{display:block;height:100%;border-radius:3px}
@media (max-width:640px){
  .pcc-tab .tabname{display:none}
  .pcc-bar-in{padding:0 12px}
  .inbox .row .acts{margin-left:40px}
  .inbox .pcc-btn,.projs .pcc-btn,.pcc-here .pcc-btn{min-height:44px}
}
.planbar{margin:18px 0 6px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;
 background:var(--panel)}
.planbar-head{display:flex;gap:10px;align-items:baseline;margin-bottom:8px;flex-wrap:wrap}
.planbar .dact{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.pchanges{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.pchanges:empty{display:none}
.pchead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:4px}
.pchead .dact{margin-left:auto}
.pchg{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;background:var(--panel-2)}
.pchg.handled{opacity:.8}
.pctop{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12.5px}
.pk{font-family:var(--mono);font-size:11px;padding:1px 7px;border-radius:999px;
 background:var(--accent-soft);color:var(--ink)}
.pk-drop,.pk-redo{background:var(--warn-soft)}
.pk-unreadable{background:var(--crit-soft)}
.pcwhat{margin:5px 0 3px;line-height:1.45}
.pchg .dact{margin-top:6px}
.pcfile{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);margin:8px 0 3px}
.pdiff{margin:0;padding:6px 8px;border-radius:6px;background:var(--panel);border:1px solid var(--line);
 font-family:var(--mono);font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.pdiff .del{color:var(--crit)}
.pdiff .add{color:var(--done)}
.psess{margin:6px 0 4px;font-family:var(--mono);font-size:11px;color:var(--ink-3);
 display:flex;flex-direction:column;gap:4px;line-height:1.5}
.psess .warn{color:var(--warn)}
.psess button{margin:2px 6px 0 0;font-size:11.5px;padding:4px 9px}
.dstatus.warn{color:var(--warn)}
#pcc-out{position:fixed;right:14px;bottom:58px;z-index:51;width:min(680px,calc(100vw - 28px));
 max-height:52vh;display:none;flex-direction:column;background:var(--panel);
 border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);overflow:hidden}
#pcc-out.on{display:flex}
#pcc-out .links{display:flex;gap:7px;padding:8px 11px;border-top:1px solid var(--line);flex-wrap:wrap}
#pcc-out .links a{text-decoration:none}
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
    }).then(function(r){ return r.json(); }).then(function(d){
        // The per-run token died with the old server process. The raw error
        // reads like a bug; what happened is the dashboard restarted.
        if(d && d.error === 'bad or missing token'){
          d.error = 'the dashboard was restarted since this page loaded \u2014 reload the page and try again';
        }
        return d;
      });
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
          if(ttl.textContent === 'Standup' && d.rc === 0) standupLinks();
        }
      }).catch(function(){ clearInterval(poll); enable(true); });
    }, 400);
  }

  // After a standup: the report as a page, as a download, and - when the plan
  // has a ticket and the API is configured - attached to that ticket. The
  // attach is outward-facing, so it arms on the first click and names the
  // ticket before it does anything.
  function standupLinks(){
    var old = out.querySelector('.links'); if(old) old.remove();
    var row = document.createElement('div'); row.className = 'links';
    var openA = document.createElement('a'); openA.className = 'pcc-btn'; openA.textContent = 'Open report \u2197';
    openA.href = '/standup/latest.html'; openA.target = '_blank'; openA.rel = 'noopener';
    openA.title = 'The standup as a page - tiles, what moved, phases, blockers, next, activity';
    var dl = document.createElement('a'); dl.className = 'pcc-btn'; dl.textContent = 'Download HTML';
    dl.href = '/standup/latest.html?download=1'; dl.title = 'One self-contained file - mail it or drop it in a ticket';
    row.appendChild(openA); row.appendChild(dl);
    var P0 = window.__ANU_PLAN__ || {};
    if(P0.jira){
      var at = document.createElement('button'); at.className = 'pcc-btn';
      var armed = false, key = String(P0.jira).replace(/\/+$/, '').split('/').pop();
      if(!P0.jira_api_ready){
        at.textContent = 'Attach to ' + key; at.disabled = true;
        at.title = 'Needs the JIRA API configured in Setup (site, project key, token) - the download works without it';
      } else {
        at.textContent = 'Attach to ' + key + '\u2026';
        at.title = 'Attaches this file to the ticket and leaves a one-line comment. First click arms it.';
        at.addEventListener('click', function(){
          if(!armed){
            armed = true; at.textContent = 'Confirm: attach to ' + key;
            at.style.background = 'var(--crit)'; at.style.borderColor = 'var(--crit)'; at.style.color = '#fff';
            return;
          }
          at.disabled = true; at.textContent = 'Attaching\u2026';
          api('/api/standup/attach', {name: 'latest.html'}).then(function(d){
            if(!d.ok){ at.disabled = false; armed = false; at.textContent = 'Attach to ' + key + '\u2026';
                       at.style.background = ''; at.style.borderColor = ''; at.style.color = '';
                       pre.textContent += '\n\nattach failed: ' + d.error; pre.scrollTop = pre.scrollHeight; return; }
            at.textContent = 'Attached \u2713';
            at.style.background = ''; at.style.borderColor = ''; at.style.color = '';
            pre.textContent += '\n\nattached ' + d.filename + ' (' + d.size + ' bytes) to ' + d.key +
              (d.commented ? ' with a comment' : ' - comment failed: ' + d.comment_error) + '\n' + d.url;
            pre.scrollTop = pre.scrollHeight;
          }).catch(function(){ at.disabled = false; armed = false; at.textContent = 'Attach to ' + key + '\u2026'; });
        });
      }
      row.appendChild(at);
    }
    out.appendChild(row);
  }

  btns.forEach(function(b){
    b.addEventListener('click', function(){
      enable(false);
      var old = out.querySelector('.links'); if(old) old.remove();
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
  // Phase sessions on record server-side: which conversation Send goes to and
  // what it will do. Loaded once, refreshed after every launch; anything that
  // draws from it registers a watcher.
  var SESS = {}, sessWatchers = [];
  function loadSessions(cb){
    fetch('/api/sessions').then(function(r){ return r.json(); }).then(function(d){
      if(d && d.ok){ SESS = d.phases || {}; sessWatchers.forEach(function(f){ try{ f(); }catch(e){} }); }
      if(cb) cb();
    }).catch(function(){ if(cb) cb(); });
  }
  function baseOf(tool){ return (LN[tool] && LN[tool].base) ? LN[tool].base : tool; }
  function sessRec(phaseId, tool){ return (SESS[String(phaseId)] || {})[baseOf(tool)] || null; }
  function warmKeyFor(base){
    for(var i=0;i<lnKeys.length;i++){ if(LN[lnKeys[i]].warm && baseOf(lnKeys[i]) === base) return lnKeys[i]; }
    return null;
  }
  // Preselect for a phase: your usual tool, switched to its "continue" form
  // when this phase already has a session on record - and back to the cold
  // form when it does not, so "continue" is never the silent default for a
  // phase that has nothing to continue.
  function pickLauncher(sel, phaseId){
    var k = preferredTool(); if(!k || !LN[k]) return;
    var base = baseOf(k), rec = (SESS[String(phaseId)] || {})[base], wk = warmKeyFor(base);
    // A record whose transcript is known to be gone is no session to send to.
    if(rec && rec.id && rec.transcript !== false){ if(wk && !LN[k].warm) k = wk; }
    else if(LN[k].warm && LN[base]) k = base;
    sel.value = k;
  }
  function ago(iso){
    if(!iso) return '';
    var ms = Date.now() - new Date(iso).getTime(); if(isNaN(ms)) return iso;
    var m = Math.round(ms/60000); if(m < 1) return 'just now'; if(m < 60) return m + ' min ago';
    var h = Math.round(m/60); if(h < 48) return h + ' h ago'; return Math.round(h/24) + ' d ago';
  }
  loadSessions();

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
  // One re-plan box for every scope. It collects the steering comment and
  // which context providers the session should consult, asks the server for
  // the prompt (the rules live server-side, in one place), and hands it to
  // the same session machinery every other button uses.
  function replanBox(scope, phaseId, itemLabel, say, onSent){
    var box = document.createElement('div'); box.className = 'replanbox';
    var ta = document.createElement('textarea');
    ta.placeholder = scope === 'plan'
      ? 'why re-plan? what changed, what feels wrong, what to optimise for…'
      : 'steering: what changed, what is wrong with this as written…';
    ta.rows = 3;
    box.appendChild(ta);
    var provs = window.__ANU_PROVIDERS__ || [];
    var picks = [];
    if(provs.length){
      var pl = document.createElement('div'); pl.className = 'rprovs';
      var plab = document.createElement('span');
      plab.textContent = 'consult:'; pl.appendChild(plab);
      provs.forEach(function(n){
        var l = document.createElement('label');
        var c = document.createElement('input'); c.type = 'checkbox'; c.checked = true;
        l.appendChild(c); l.appendChild(document.createTextNode(' ' + n));
        pl.appendChild(l); picks.push({name: n, cb: c});
      });
      box.appendChild(pl);
    }
    var bar = document.createElement('div'); bar.className = 'rbar';
    // fail() runs on EVERY path that does not reach fn - a stale token, a
    // restarted server, a rejected fetch. Without it the launch button's
    // disabled=true had no matching false, and one failure bricked the box.
    function withPrompt(fn, fail){
      api('/api/replan-prompt', {
        scope: scope, phase: phaseId, item: itemLabel || '',
        comment: ta.value,
        providers: picks.filter(function(x){ return x.cb.checked; })
                        .map(function(x){ return x.name; })
      }).then(function(d){
        if(!d || !d.ok){
          say((d && d.error) || 'prompt build failed', 'err');
          if(fail) fail(); return;
        }
        fn(d.prompt);
      }).catch(function(){
        say('server unreachable - is the dashboard still running?', 'err');
        if(fail) fail();
      });
    }
    // No launcher on this machine means this button could only ever fail;
    // every other session control in this file hides itself in that case.
    var haveLaunch = Object.keys(window.__ANU_LAUNCHERS__ || {}).length > 0;
    var ob = document.createElement('button');
    ob.className = 'pcc-btn run'; ob.textContent = 'Open re-plan session';
    ob.title = 'Opens your tool on this repo with the re-plan prompt';
    ob.addEventListener('click', function(){
      ob.disabled = true;
      function again(){ ob.disabled = false; }
      withPrompt(function(pr){
        api('/api/session', {phase: 'replan-' + scope + '-' + (phaseId || 'all'),
                             prompt: pr, tool: window.__ANU_PROFILE_TOOL__ || 'claude'})
          .then(function(d){ again();
            if(d && d.ok && onSent) onSent();
            say(d && d.ok ? 'session opened — this page follows the plan as it changes'
                          : ((d && d.error) || 'launch failed'), d && d.ok ? 'ok' : 'err'); })
          .catch(function(){ again(); say('launch failed — server unreachable', 'err'); });
      }, again);
    });
    var cp = document.createElement('button');
    cp.className = 'pcc-btn'; cp.textContent = 'Copy prompt';
    cp.title = 'For a session you already have open, or another machine';
    cp.addEventListener('click', function(){
      withPrompt(function(pr){
        copyLocal(pr).then(function(){ if(onSent) onSent(); say('re-plan prompt copied', 'ok'); },
                           function(){ say('clipboard refused', 'err'); });
      });
    });
    if(haveLaunch) bar.appendChild(ob);
    bar.appendChild(cp);
    box.appendChild(bar);
    return box;
  }

  // toggle helper: one button shows/hides one lazily built box
  function replanToggle(scope, phaseId, itemLabel, say, host){
    var b = document.createElement('button');
    b.className = 'pcc-btn'; b.textContent = 'Re-plan…';
    b.title = scope === 'item'
      ? 'Reassess this item in a code session, with your steering — it may add items, or a phase, if the rethink needs them'
      : 'Reassess this phase in a code session — it proposes changes first, then edits the plan; it may add items or a phase';
    var box = null;
    b.addEventListener('click', function(){
      if(box){ box.remove(); box = null; b.textContent = 'Re-plan…'; return; }
      box = replanBox(scope, phaseId, itemLabel, say);
      host.appendChild(box); b.textContent = 'Close re-plan';
      box.querySelector('textarea').focus();
    });
    return b;
  }

  function copyLocal(text){
    try {
      if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    } catch(e){}
    return Promise.reject();
  }
  // One launch routine, used by the start cards and by the phase drawer.
  function launch(btn, phaseId, prompt, tool, say, extra){
    btn.disabled = true;
    var was = btn.textContent;
    btn.textContent = 'Opening…';
    try { localStorage.pccLauncher = tool; } catch(e){}
    var body = {phase: phaseId, prompt: prompt, tool: tool};
    if(extra && extra.item) body.item = extra.item;
    if(extra && extra.prompt_warm) body.prompt_warm = extra.prompt_warm;
    // Copy what the server will actually send: the warm shape on a continue launcher.
    var toCopy = (LN[tool] && LN[tool].warm && extra && extra.prompt_warm) ? extra.prompt_warm : prompt;
    var copied = false;
    copyLocal(toCopy).then(function(){ copied = true; }, function(){}).then(function(){
      return api('/api/session', body);
    }).then(function(d){
      btn.disabled = false; btn.textContent = was;
      if(!d.ok){ if(say) say(d.error, 'err'); else alert(d.error); return; }
      // Say exactly what happened and what is left for you to do. "paste it in"
      // was too vague to act on, and claiming a copy we had not verified was
      // worse than saying nothing.
      var name = LN[d.tool] ? LN[d.tool].label : (d.tool || d.via);
      var ok = copied || d.copied, msg, cls = 'ok';
      if(d.mode === 'paste'){
        // Composed here from the COMBINED copy result: the page's own copy may
        // have succeeded where the server's failed, or the other way round.
        var where = d.where || 'the terminal running the session';
        msg = ok ? ('prompt on your clipboard — paste it into ' + where +
                    (d.focused ? '' : ' (the terminal is in your taskbar)'))
                 : ('the session is open in ' + where + ', but no clipboard copy succeeded — ' +
                    'open “view prompt” below and copy it by hand');
        if(!ok) cls = 'err';
      } else if(d.mode === 'clipboard'){
        msg = ok ? (name + ' opened — the prompt is on your clipboard, press Ctrl+V in it')
                 : (name + ' opened, but the clipboard copy FAILED — open “view prompt” ' +
                    'below and copy it by hand');
        if(!ok) cls = 'err';
      } else {
        msg = (d.shape === 'warm' ? 'Follow-up sent to ' : 'Session started in ') + name +
              (d.agent ? ' as ' + d.agent : '') +
              (d.session ? ' · session ' + String(d.session).slice(0, 8) + '…' : '') +
              (d.pinned ? ' · brief pinned' : '') +
              (d.note ? ' — ' + d.note : '') + (ok ? ' · also on your clipboard' : '');
        if(d.note) cls = 'warn';
      }
      if(say) say(msg, cls); else { btn.textContent = ok ? 'Opened ✓' : 'Opened (no copy)';
        btn.title = msg; setTimeout(function(){ btn.textContent = was; }, 4000); }
      loadSessions();
    });
  }
  window.__pccLaunch__ = launch;

  // Whole-plan re-plan lives on the bottom bar; its box floats above it so it
  // works from any tab.
  (function(){
    var b = document.getElementById('pcc-replan-all'); if(!b) return;
    var host = document.createElement('div'); host.id = 'pcc-replan-host';
    document.body.appendChild(host);
    function sayTop(m, c){
      var n = host.querySelector('.dstatus');
      if(!n){ n = document.createElement('div'); n.className = 'dstatus';
              host.appendChild(n); }
      n.textContent = m || ''; n.className = 'dstatus ' + (c || '');
    }
    var box = null;
    b.addEventListener('click', function(){
      if(box){
        host.textContent = '';   // the box AND its floating status line
        box = null; sent = null; b.textContent = 'Re-plan…'; return;
      }
      box = replanBox('plan', '', '', sayTop, function(){ if(sent) sent(); });
      host.appendChild(box); b.textContent = 'Close re-plan';
      box.querySelector('textarea').focus();
    });
    // "Re-plan with this/these" on proposed plan changes: this box, opened with
    // the proposals as the steering. They count as handed over only once the
    // session is actually opened or the prompt copied - not on a mere open.
    var sent = null;
    window.__pccReplanWith = function(text, onSent){
      if(!box) b.click();
      sent = onSent || null;
      var ta = box.querySelector('textarea');
      ta.value = text; ta.rows = Math.min(12, text.split('\n').length + 1); ta.focus();
    };
  })();

  // ------------------------------------------------------------- freshness --
  // A git pull rewrites the plan under this page. Every render already comes
  // from disk, so the missing piece is the page NOTICING: poll a stamp of the
  // watched files' mtimes and reload when it moves - open phases restored, so
  // the reload costs one blink.
  (function(){
    // The baseline is the stamp of the files THIS page was rendered from,
    // baked in server-side. Adopting the first poll response instead would
    // silently absorb a change landing between render and first poll - the
    // exact git-pull-during-load window the feature exists for.
    var v0 = window.__ANU_FRESH0__ || null;
    function tick(){
      // A write this page started (plan switch, phase sync) reloads the page
      // itself when it lands; the poll must not race it and drop its note.
      if(window.__pccSelfWrite) return;
      fetch('/api/fresh').then(function(r){ return r.json(); }).then(function(d){
        if(!d || !d.v) return;
        if(window.__pccProposalsPoke) window.__pccProposalsPoke(d.pv);
        if(v0 === null){ v0 = d.v; return; }
        if(d.v === v0) return;
        // open-phase state survives via the shared render's own
        // pccOpenPhases beforeunload stash - reload() fires it like any
        // other navigation; a second mechanism here would just race it.
        location.reload();
      }).catch(function(){});
    }
        setInterval(tick, 4000); tick();
  })();

  // More than one plan: the bar's select makes another one active.
  (function(){
    var s = document.getElementById('pcc-plan'); if(!s) return;
    var was = s.value;
    s.addEventListener('change', function(){
      s.disabled = true;
      window.__pccSelfWrite = true;
      api('/api/plan/switch', {plan: s.value}).then(function(d){
        if(d && d.ok){
          try { sessionStorage.setItem('pccPhaseSyncNote', 'active plan is now ' + s.value +
                ((d.notes && d.notes.length) ? ' \u2014 ' + d.notes.join('; ') : '')); } catch(e){}
          location.reload();
        } else {
          s.disabled = false; s.value = was; window.__pccSelfWrite = false;
          alert((d && d.error) || 'could not switch plans');
        }
      });
    });
  })();

  // Phase headings in the plan with no [[phase]] block: ask the server to add
  // them (the additive sync Save runs), then reload so they render. Once per
  // gap per tab, so a failing write cannot loop; what was written is said.
  (function(){
    try {
      var note = sessionStorage.getItem('pccPhaseSyncNote');
      if(note){
        sessionStorage.removeItem('pccPhaseSyncNote');
        var n = document.createElement('p'); n.className = 'pnote';
        n.textContent = 'docs/progress.toml updated: ' + note;
        var host = document.querySelector('main') || document.body;
        host.insertBefore(n, host.firstChild);
      }
    } catch(e){}
    var g = window.__ANU_PHASE_GAP__ || {};
    if(!((g.missing && g.missing.length) || g.mode)) return;
    var sig = 'pccPhaseSync:' + (g.repo || '') + ':' + (g.missing || []).join(',') + (g.mode ? '+mode' : '');
    try { if(sessionStorage.getItem(sig)) return; sessionStorage.setItem(sig, '1'); } catch(e){ return; }
    window.__pccSelfWrite = true;
    api('/api/phases/sync', {}).then(function(d){
      if(d && d.ok && d.written){
        try { sessionStorage.setItem('pccPhaseSyncNote', (d.notes || []).join('; ')); } catch(e){}
        location.reload();
      } else { window.__pccSelfWrite = false; if(d && !d.ok) console.warn('phase sync: ' + d.error); }
    });
  })();
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
  function itemPromptWarm(p, label){
    if(!p.item_tmpl_warm) return '';
    return p.item_tmpl_warm.split(p.slot).join(label);
  }
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
    var prompt = itemPrompt(p, label), warmPrompt = itemPromptWarm(p, label);
    var msg = document.createElement('div'); msg.className = 'dstatus';
    function say(t, c){ msg.textContent = t || ''; msg.className = 'dstatus ' + (c||''); }

    var row = document.createElement('div'); row.className = 'dact';

    if(lnKeys.length){
      var sel = toolSelect();
      if(!isDone) pickLauncher(sel, p.id);
      var go = document.createElement('button');
      go.className = 'pcc-btn run';
      // The button says what the launch will DO, which depends on the launcher
      // and on whether this phase has a session on record - not a fixed verb.
      function relabel(){
        if(isDone){
          go.textContent = 'Open blank session here';
          go.title = 'This item is done — opens the tool in the repo with no prompt at all';
          return;
        }
        var L = LN[sel.value] || {}, rec = sessRec(p.id, sel.value);
        if(L.warm && rec && rec.id && rec.transcript !== false){
          go.textContent = 'Send to phase session';
          // The server already worked out what Send will do; the button says the same.
          go.title = 'Sends only this item to the recorded ' + baseOf(sel.value) + ' session ' +
                     String(rec.id).slice(0, 8) + '… — Send will ' +
                     (rec.send_will || 'paste into its tab if that is open, resume it in a new tab if not');
        } else if(L.warm){
          go.textContent = 'Send to last session';
          go.title = (rec && rec.id)
            ? 'No transcript on disk for the recorded session: this continues the most recent conversation in the repo, which may not be it'
            : 'No session is recorded for this phase: this continues the most recent conversation in the repo, which may not be it';
        } else if(L.mode === 'clipboard'){
          // No prompt argument: the brief goes to the clipboard and the app is
          // opened. No session id, no pinned brief, nothing to send to later.
          go.textContent = 'Copy item brief for ' + L.label.replace(/\s*\(.*$/, '');
          go.title = 'This tool takes no prompt: the full brief for this item goes to your clipboard and the app is opened - paste it in. Sessions are not tracked for it.';
        } else {
          go.textContent = 'Open session on this item';
          go.title = 'Opens a new session with the full brief for this one item; it becomes the phase session';
        }
        // The one thing the page CAN verify about the session: whether the
        // item it was last sent is still open in the plan. Sending the next
        // one onto an unconfirmed brief is the mistake this line prevents.
        // A send that went via --continue reached an unknown conversation, so it
        // says nothing about THIS session's unconfirmed brief.
        var prev = rec && rec.last_sent && rec.last_sent.via !== 'continue' && rec.last_sent.item;
        var warnOn = false;
        if(L.warm && rec && rec.id && prev && prev !== label && li.parentNode){
          var lis = li.parentNode.querySelectorAll('li.item');
          for(var i=0;i<lis.length;i++){
            if(lis[i].dataset.item === prev && lis[i].dataset.s !== 'done'){ warnOn = true; break; }
          }
        }
        // 'stale' marks the one message relabel owns; a launch result (which
        // may also be amber) is never cleared by the refresh that follows it.
        if(warnOn) say('the item last sent to this session — “' + prev + '” — is still open in the plan', 'warn stale');
        else if(msg.className.indexOf('stale') >= 0) say('');
      }
      sel.addEventListener('change', function(){ sel.dataset.touched = '1'; relabel(); });
      // Records load asynchronously: re-pick when they arrive, unless the
      // developer has already chosen a launcher by hand.
      sessWatchers.push(function(){ if(!isDone && !sel.dataset.touched) pickLauncher(sel, p.id); relabel(); });
      relabel();
      go.addEventListener('click', function(ev){
        ev.stopPropagation();
        if(isDone) launchBlank(go, p.id, sel.value, say);
        else launch(go, p.id + '-item', prompt, sel.value, say, {item: label, prompt_warm: warmPrompt});
      });
      row.appendChild(go); row.appendChild(sel);
    }

    var cp = document.createElement('button');
    cp.className = 'pcc-btn';
    cp.textContent = isDone ? 'Copy re-check prompt' : 'Copy prompt (new chat)';
    cp.title = isDone ? 'A prompt that VERIFIES this item, rather than rebuilding it'
                      : 'The full brief for this item — for a chat that has nothing yet';
    cp.addEventListener('click', function(ev){
      ev.stopPropagation();
      var txt = isDone ? recheckPrompt(p, label) : prompt;
      copyLocal(txt).then(function(){ say('prompt copied', 'ok'); },
                          function(){ say('clipboard refused — open the prompt below', 'err'); });
    });
    row.appendChild(cp);
    if(!isDone && warmPrompt){
      var cw = document.createElement('button');
      cw.className = 'pcc-btn'; cw.textContent = 'Copy prompt (same chat)';
      cw.title = 'The short follow-up — for a chat that already holds this phase’s brief';
      cw.addEventListener('click', function(ev){
        ev.stopPropagation();
        copyLocal(warmPrompt).then(function(){ say('follow-up copied', 'ok'); },
                                   function(){ say('clipboard refused', 'err'); });
      });
      row.appendChild(cw);
    }

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
        var txt = (/\(continue\)$/.test(t) && warmPrompt) ? warmPrompt : prompt;
        var cmd = tmpl.split('{repo}').join(window.__PCC_REPO__ || '.').split('{p}').join(txt);
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

    row.appendChild(replanToggle('item', String(p.id), label, say, bar));

    // The prompt fold is part of the shared template now (filled from the
    // phase's template at load); the controls go above it.
    bar.insertBefore(row, bar.firstChild);
    bar.insertBefore(msg, row.nextSibling);
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
      pickLauncher(sel, p.id);
      var open = document.createElement('button');
      open.className = 'pcc-btn run';
      function relabel(){
        var L = LN[sel.value] || {}, rec = sessRec(p.id, sel.value);
        if(L.warm && rec && rec.id){
          open.textContent = 'Re-sync phase session';
          open.title = 'Asks the recorded session to re-read the checklist and wait for the next item';
        } else if(L.warm){
          open.textContent = 'Continue last session';
          open.title = 'No session recorded for this phase — continues the most recent conversation in the repo, which may not be it';
        } else if(L.mode === 'clipboard'){
          open.textContent = 'Copy phase brief for ' + L.label.replace(/\s*\(.*$/, '');
          open.title = 'This tool takes no prompt: the phase brief goes to your clipboard and the app is opened - paste it in. Sessions are not tracked for it; the strip above describes terminal sessions only.';
        } else {
          open.textContent = 'Start phase session';
          open.title = p.startable ? 'Opens a session with the phase brief; it reads in, then waits for the items you send'
                                   : 'This phase is blocked — the brief says so';
        }
      }
      sel.addEventListener('change', function(){ sel.dataset.touched = '1'; relabel(); });
      sessWatchers.push(function(){ if(!sel.dataset.touched) pickLauncher(sel, p.id); relabel(); });
      relabel();
      open.addEventListener('click', function(){
        launch(open, p.id, p.prompt, sel.value, say, {prompt_warm: p.prompt_warm});
      });
      act.appendChild(open); act.appendChild(sel);
    }

    if(p.test){
      var t = document.createElement('button');
      t.className = 'pcc-btn'; t.textContent = 'Test';
      t.title = 'Run the `' + p.test + '` action — this phase’s exit test';
      t.addEventListener('click', function(){ runInto(p.test, t, host, say); });
      act.appendChild(t);
    }

    act.appendChild(replanToggle('phase', String(p.id), '', say, host));

    var rg = document.createElement('button');
    rg.className = 'pcc-btn'; rg.textContent = 'Regenerate';
    rg.title = 'Re-read the plan files and refresh — Re-plan… is the one that CHANGES the plan';
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
    var what = p.id === '_plan' ? 'plan' : 'phase';
    // One slot, two states. A phase that has a ticket offers to remove it, with
    // the key in the button so it says what it will do; a phase without one
    // offers the key input. Either action swaps the slot in place - no reload
    // to get back the control you now need.
    var slot = document.createElement('span');
    function currentKey(){
      var rec = (window.__PCC_PHASES__ || {})[p.id];
      return String((rec && rec.jira) || p.jira || '');
    }
    function renderTicketSlot(){
      slot.textContent = '';
      var cur = currentKey();
      if(cur){
        var un = document.createElement('button');
        un.className = 'pcc-btn'; un.textContent = 'Unlink ' + cur;
        un.title = 'Remove this ticket from the ' + what + ' in docs/progress.toml. The '
                 + 'issue itself is not touched - only the link.';
        un.addEventListener('click', function(){
          un.disabled = true;
          api('/api/phase/unlink-jira', {phase: p.id}).then(function(d){
            un.disabled = false;
            if(!d || !d.ok){ say((d && d.error) || 'unlink failed', 'err'); return; }
            if(window.__PCC_PHASES__ && window.__PCC_PHASES__[p.id])
              window.__PCC_PHASES__[p.id].jira = '';
            p.jira = '';
            say('unlinked ' + d.was + ' \u2014 the issue still exists in JIRA; '
                + 'reload to clear its pill', 'ok');
            renderTicketSlot();
          }).catch(function(){ un.disabled = false; say('server unreachable', 'err'); });
        });
        slot.appendChild(un);
        return;
      }
      var inp = document.createElement('input');
      inp.type = 'text'; inp.placeholder = 'PROJ-123'; inp.className = 'pcc-btn';
      inp.style.width = '110px'; inp.style.cursor = 'text';
      var lk = document.createElement('button');
      lk.className = 'pcc-btn'; lk.textContent = 'Link ticket';
      lk.title = 'Record an EXISTING ticket key on this ' + what + ' (docs/progress.toml)';
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
          if(window.__PCC_PHASES__ && window.__PCC_PHASES__[p.id])
            window.__PCC_PHASES__[p.id].jira = d.key;
          p.jira = d.key;
          say('linked ' + d.key + ' \u2014 reload to see the pill everywhere', 'ok');
          renderTicketSlot();
        }).catch(function(){ lk.disabled = false; say('server unreachable', 'err'); });
      }
      lk.addEventListener('click', doLink);
      inp.addEventListener('keydown', function(ev){ if(ev.key === 'Enter') doLink(); });
      slot.appendChild(inp); slot.appendChild(lk);
    }
    renderTicketSlot();
    // A linked phase still needs the control that UNLINKS it; only the
    // drafting controls are pointless once a ticket exists.
    if(p.jira){ act.appendChild(slot); return; }

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
      draft.title = 'Ask the selected terminal tool to write a ticket from this ' + what + ', ' +
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
    load.title = 'Read the draft a session wrote for this ' + what + ' (.pcc/ticket-<plan>-' + p.id + '.json)';
    load.addEventListener('click', function(){ loadDraft(p, act, say, host, true); });
    act.appendChild(load);

    act.appendChild(slot);

    loadDraft(p, act, say, host, false, function(found){
      if(!found) resumeWatch(p, act, say, host);   // a reload mid-drafting resumes
    });
  }

  // One ticket for the PLAN, not one per phase: a row above the tabs with the
  // same draft -> review -> create/link flow, aimed at the active plan.
  (function(){
    var P0 = window.__ANU_PLAN__; if(!P0) return;
    var anchor = document.querySelector('nav.tabs'); if(!anchor) return;
    var panel = document.createElement('section'); panel.className = 'planbar';
    panel.setAttribute('aria-label', 'Plan ticket and proposed plan changes');
    var head = document.createElement('div'); head.className = 'planbar-head';
    var lbl = document.createElement('span'); lbl.className = 'eyebrow'; lbl.textContent = 'Plan ticket';
    var nm = document.createElement('span'); nm.className = 'quiet'; nm.textContent = P0.name;
    head.appendChild(lbl); head.appendChild(nm);
    if(P0.agent){
      var ag = document.createElement('span'); ag.className = 'quiet';
      var miss = (P0.agent.missing || []).length;
      ag.textContent = '\u00b7 agent ' + P0.agent.name + ' \u00b7 ' + P0.agent.sources + ' source' +
        (P0.agent.sources === 1 ? '' : 's') + (miss ? ' (' + miss + ' missing)' : '') +
        ' \u00b7 files for ' + (P0.agent.files.length ? P0.agent.files.join(', ') : 'no tool yet');
      ag.title = miss ? 'Missing: ' + P0.agent.missing.join(', ') + ' \u2014 declared in docs/progress.toml but not found'
                      : 'Cold launches of ' + (P0.agent.files.join(' and ') || 'nothing') + ' start as this agent';
      if(miss) ag.className = 'warn';
      head.appendChild(ag);
    }
    var act = document.createElement('div'); act.className = 'dact';
    var msg = document.createElement('div'); msg.className = 'dstatus';
    function say(t, c){ msg.textContent = t || ''; msg.className = 'dstatus ' + (c||''); }
    var terms = terminalTools();
    if(terms.length && !P0.jira){
      var sel = toolSelect();
      // only tools that can RUN and write the draft file; a clipboard app cannot
      [].slice.call(sel.options).forEach(function(o){ if(terms.indexOf(o.value) < 0) sel.removeChild(o); });
      if(terms.indexOf(sel.value) < 0) sel.value = terms[0];
      sel.setAttribute('aria-label', 'Coding tool that drafts the plan ticket');
      act.appendChild(sel);
    }
    panel.appendChild(head); panel.appendChild(act); panel.appendChild(msg);
    anchor.parentNode.insertBefore(panel, anchor);
    ticketControls(P0, act, say, panel);
  })();

  // Plan changes proposed by working sessions (protocol rule 5). A session
  // never edits the plan for a later item: it appends a proposal, the server
  // previews it as the exact lines it would change, and nothing lands until
  // Apply change is pressed and then confirmed. Applied changes are logged in
  // the plan under "Plan changes along the way" and can be undone here.
  (function(){
    var panel = document.querySelector('section.planbar'); if(!panel) return;
    var box = document.createElement('div'); box.className = 'pchanges';
    panel.appendChild(box);
    var seen = null, pending = false, showHandled = false, last = null, shown = null;
    try { var sv = sessionStorage.getItem('pccChangesShown'); if(sv !== null) shown = sv === '1'; } catch(e){}
    var KINDS = {reword: 1, add: 1, drop: 1, redo: 1, note: 1, exit: 1, unreadable: 1};
    var STATUS = {applied: 'applied', dismissed: 'dismissed', replan: 'sent to re-plan'};
    try {
      var note = sessionStorage.getItem('pccPlanChangeNote');
      if(note){
        sessionStorage.removeItem('pccPlanChangeNote');
        var pn = document.createElement('p'); pn.className = 'pnote'; pn.textContent = note;
        var mh = document.querySelector('main') || document.body;
        mh.insertBefore(pn, mh.firstChild);
      }
    } catch(e){}
    function el(tag, cls, text){
      var n = document.createElement(tag);
      if(cls) n.className = cls;
      if(text !== undefined && text !== null) n.textContent = text;
      return n;
    }
    function previewing(){ return !!box.querySelector('.pcprev'); }
    function load(){
      api('/api/proposals', {}).then(function(d){ last = d; render(); }).catch(function(){});
    }
    // The freshness poll hands over the proposals stamp: a session appending a
    // line refreshes this list, never the page. An open preview is not yanked
    // away mid-confirm; the refresh waits until it closes.
    window.__pccProposalsPoke = function(pv){
      if(pv === undefined || pv === seen) return;
      var first = seen === null; seen = pv;
      if(first) return;
      if(previewing()) pending = true; else load();
    };
    load();

    function describe(v){
      if(v.summary) return v.summary;
      if(v.kind === 'note' || v.kind === 'unreadable') return v.text;
      return (v.target ? '"' + v.target + '"' : '') + (v.text ? ' \u2192 "' + v.text + '"' : '');
    }
    function steering(list, file){
      return 'Plan changes proposed by working sessions (recorded in ' + file + '). ' +
        'Apply what still holds, reject what does not, and say which in the brief:\n' +
        list.map(function(v, n){
          return (n + 1) + '. [Phase ' + (v.phase || '?') + ' \u00b7 ' + v.kind + '] ' + describe(v) +
            (v.why ? ' \u2014 ' + v.why : '') + (v.from ? ' (from ' + v.from + ')' : '');
        }).join('\n');
    }
    function replanWith(list){
      var ids = list.map(function(v){ return v.id; });
      window.__pccReplanWith(steering(list, last.file), function(){
        api('/api/proposals/mark', {ids: ids, status: 'replan'}).then(load);
      });
    }
    function render(){
      var d = last;
      box.textContent = '';
      if(!d || !d.ok || !d.items || !d.items.length) return;
      var open = d.items.filter(function(v){ return v.status === 'open'; });
      var done = d.items.filter(function(v){ return v.status !== 'open'; });
      var head = el('div', 'pchead');
      head.appendChild(el('span', 'eyebrow', 'Plan changes'));
      head.appendChild(el('span', open.length ? '' : 'quiet', open.length
        ? open.length + ' proposed by working sessions \u2014 the plan changes only when you apply'
        : 'none waiting'));
      var acts = el('div', 'dact');
      // A long list would push the phases off the first screen: up to three
      // show at once, more collapse behind Review, and the choice is kept
      // across the reload every Apply causes.
      var expanded = shown === null ? open.length <= 3 : shown;
      if(open.length){
        var tg = el('button', 'pcc-btn', expanded ? 'Hide' : 'Review ' + open.length);
        tg.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        tg.addEventListener('click', function(){
          shown = !expanded;
          try { sessionStorage.setItem('pccChangesShown', shown ? '1' : '0'); } catch(e){}
          render();
        });
        acts.appendChild(tg);
      }
      var steerable = open.filter(function(v){ return v.kind !== 'unreadable'; });
      if(steerable.length > 1 && window.__pccReplanWith){
        var rp = el('button', 'pcc-btn', 'Re-plan with these ' + steerable.length);
        rp.title = 'Opens Re-plan with every waiting proposal as the steering \u2014 for changes that need judgment';
        rp.addEventListener('click', function(){ replanWith(steerable); });
        acts.appendChild(rp);
      }
      if(done.length){
        var hb = el('button', 'pcc-btn', (showHandled ? 'Hide' : 'Show') + ' handled (' + done.length + ')');
        hb.setAttribute('aria-expanded', showHandled ? 'true' : 'false');
        hb.addEventListener('click', function(){ showHandled = !showHandled; render(); });
        acts.appendChild(hb);
      }
      head.appendChild(acts);
      box.appendChild(head);
      if(expanded) open.forEach(function(v){ box.appendChild(card(v)); });
      if(showHandled) done.forEach(function(v){ box.appendChild(card(v)); });
    }
    function card(v){
      var c = el('div', 'pchg' + (v.status !== 'open' ? ' handled' : ''));
      var top = el('div', 'pctop');
      top.appendChild(el('span', 'pk pk-' + (KINDS[v.kind] ? v.kind : 'note'), v.kind));
      top.appendChild(el('span', '', v.phase ? 'Phase ' + v.phase + (v.phase_name ? ' \u00b7 ' + v.phase_name : '')
                                             : 'no phase named'));
      if(v.from) top.appendChild(el('span', 'quiet', 'from ' + v.from));
      if(v.status !== 'open'){
        top.appendChild(el('span', 'quiet', (STATUS[v.status] || v.status) +
          (v.handled_at ? ' ' + String(v.handled_at).slice(0, 16).replace('T', ' ') : '')));
      }
      c.appendChild(top);
      c.appendChild(el('div', 'pcwhat', describe(v)));
      if(v.why) c.appendChild(el('div', 'quiet', 'why: ' + v.why));
      if(v.status === 'open' && v.problem) c.appendChild(el('div', 'dstatus warn', 'Cannot apply as written: ' + v.problem));
      var row = el('div', 'dact');
      var msg = el('div', 'dstatus');
      function say(t, k){ msg.textContent = t || ''; msg.className = 'dstatus ' + (k || ''); }
      if(v.status === 'open'){
        if(v.ops){
          var pv = null;
          var ap = el('button', 'pcc-btn run', 'Apply change\u2026');
          ap.title = 'Shows the exact lines this changes; nothing is written until you confirm';
          ap.setAttribute('aria-expanded', 'false');
          var close = function(){
            if(pv){ pv.remove(); pv = null; }
            ap.textContent = 'Apply change\u2026'; ap.setAttribute('aria-expanded', 'false');
            if(pending){ pending = false; load(); }
          };
          ap.addEventListener('click', function(){
            if(pv){ close(); say(''); return; }
            pv = preview(v, say, close);
            c.insertBefore(pv, msg);
            ap.textContent = 'Close preview'; ap.setAttribute('aria-expanded', 'true');
          });
          row.appendChild(ap);
        }
        if(window.__pccReplanWith && v.kind !== 'unreadable'){
          var rw = el('button', 'pcc-btn', 'Re-plan with this');
          rw.title = 'Opens Re-plan with this proposal as the steering';
          rw.addEventListener('click', function(){ replanWith([v]); });
          row.appendChild(rw);
        }
        var ds = el('button', 'pcc-btn', 'Dismiss');
        ds.title = 'Drops the proposal; the plan is not touched. Reopen it from Show handled.';
        ds.addEventListener('click', function(){
          ds.disabled = true;
          api('/api/proposals/mark', {ids: [v.id], status: 'dismissed'}).then(function(r){
            if(r && r.ok) load();
            else { ds.disabled = false; say((r && r.error) || 'could not dismiss', 'err'); }
          });
        });
        row.appendChild(ds);
      } else if(v.status === 'applied'){
        var un = el('button', 'pcc-btn', 'Undo');
        un.title = 'Restores the lines this change edited and removes its log line \u2014 refused if those lines changed since';
        un.addEventListener('click', function(){
          un.disabled = true;
          window.__pccSelfWrite = true;
          api('/api/proposals/undo', {id: v.id}).then(function(r){
            if(r && r.ok){
              try { sessionStorage.setItem('pccPlanChangeNote', 'Plan change undone: ' + (r.summary || '') +
                ((r.warnings && r.warnings.length) ? ' \u2014 ' + r.warnings.join('; ') : '')); } catch(e){}
              location.reload();
              return;
            }
            window.__pccSelfWrite = false; un.disabled = false;
            say((r && r.error) || 'could not undo', 'err');
          });
        });
        row.appendChild(un);
      } else {
        var ro = el('button', 'pcc-btn', 'Reopen');
        ro.addEventListener('click', function(){
          api('/api/proposals/mark', {ids: [v.id], status: 'open'}).then(function(r){
            if(r && r.ok) load(); else say((r && r.error) || 'could not reopen', 'err');
          });
        });
        row.appendChild(ro);
      }
      c.appendChild(row);
      c.appendChild(msg);
      return c;
    }
    // The second step: the exact lines, then a confirm that names the files.
    // The digest pins the confirm to THIS preview - if the plan moved on in
    // between, the server refuses and the list refreshes with the new edit.
    function preview(v, say, close){
      var w = el('div', 'pcprev');
      var files = [];
      v.ops.forEach(function(op){
        if(files.indexOf(op.file) < 0) files.push(op.file);
        var pre = el('pre', 'pdiff');
        if(op.log !== undefined){
          w.appendChild(el('div', 'pcfile', op.file + ' \u00b7 logged under "Plan changes along the way"' +
            (op.heading ? ' (a new section at the end of the plan)' : '')));
          pre.appendChild(el('span', 'add', '+ ' + op.log));
          w.appendChild(pre);
          return;
        }
        w.appendChild(el('div', 'pcfile', op.file));
        (op.before || []).forEach(function(l){
          pre.appendChild(el('span', 'del', '\u2212 ' + l)); pre.appendChild(document.createTextNode('\n'));
        });
        (op.after || []).forEach(function(l){
          pre.appendChild(el('span', 'add', '+ ' + l)); pre.appendChild(document.createTextNode('\n'));
        });
        w.appendChild(pre);
      });
      var bar = el('div', 'dact');
      var label = 'Confirm: write to ' + files.join(' and ');
      var ok = el('button', 'pcc-btn run', label);
      ok.addEventListener('click', function(){
        ok.disabled = true; ok.textContent = 'Applying\u2026';
        window.__pccSelfWrite = true;
        api('/api/proposals/apply', {id: v.id, digest: v.digest}).then(function(r){
          if(r && r.ok){
            try { sessionStorage.setItem('pccPlanChangeNote', 'Plan change applied: ' + r.summary +
              ' \u2014 logged in the plan under "Plan changes along the way". Undo it from Plan changes \u2192 Show handled.' +
              (r.warning ? ' ' + r.warning : '')); } catch(e){}
            location.reload();
            return;
          }
          window.__pccSelfWrite = false;
          ok.disabled = false; ok.textContent = label;
          say((r && r.error) || 'apply failed', 'err');
          if(r && r.stale){ pending = true; close(); }
        }).catch(function(){
          window.__pccSelfWrite = false; ok.disabled = false; ok.textContent = label;
          say('server unreachable \u2014 is the dashboard still running?', 'err');
        });
      });
      var cn = el('button', 'pcc-btn', 'Cancel');
      cn.addEventListener('click', function(){ close(); say(''); });
      bar.appendChild(ok); bar.appendChild(cn);
      w.appendChild(bar);
      setTimeout(function(){ ok.focus(); }, 0);
      return w;
    }
  })();

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
        if(loud) say(d.error || ('no draft yet for this phase of the active plan' + (d.path ? ' (' + d.path + ')' : '')), 'err');
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
          done.innerHTML = 'Created <b>' + d.key + '</b> and recorded it on this ' +
            (p.id === '_plan' ? 'plan' : 'phase') +
            (d.linked ? '' : ' (writing it to progress.toml failed: ' + d.link_error + ')') +
            ' — <a href="' + d.url + '" target="_blank" rel="noopener">open it ↗</a>';
          box.appendChild(done);
          if(window.__PCC_PHASES__ && window.__PCC_PHASES__[p.id]) window.__PCC_PHASES__[p.id].jira = d.key;
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

  // What the page may honestly say about a phase's session: the record, a
  // process check, a transcript check, and what Send will do next. "last
  // sent" is a launch fact - it does not mean confirmed, let alone done.
  function sessionStrip(p, det, say){
    var host = det.querySelector('.psess');
    if(!host){
      var act = det.querySelector('.dact'); if(!act) return;
      host = document.createElement('div'); host.className = 'psess';
      act.parentNode.insertBefore(host, act.nextSibling);
    }
    function attachBtn(base){
      var a = document.createElement('button'); a.className = 'pcc-btn'; a.textContent = 'Attach session id';
      a.title = 'Point Send at a conversation you started yourself (claude: the uuid from /status; opencode: ses_…)';
      a.addEventListener('click', function(){
        var id = window.prompt('Session id to send Phase ' + p.id + ' items to (' + base + '):', '');
        if(!id) return;
        api('/api/session/attach', {phase: p.id, base: base, id: id.trim()}).then(function(d){
          if(!d.ok){ say(d.error || 'could not attach', 'err'); return; }
          var id8 = id.trim().slice(0, 8) + '…';
          if(d.transcript === false) say('attached — no transcript found here for ' + id8 + '; Send will fall back to the most recent conversation', 'warn');
          else say('attached — Send now goes to ' + id8, 'ok');
          loadSessions();
        });
      });
      return a;
    }
    function draw(){
      host.innerHTML = '';
      var ph = SESS[String(p.id)] || {}, bases = Object.keys(ph).filter(function(b){ return ph[b] && (ph[b].id || ph[b].last_sent); });
      if(!bases.length){
        var none = el('div', 'no phase session on record — Open/Start records one');
        var pref = preferredTool();
        if(pref) none.appendChild(attachBtn(baseOf(pref)));
        host.appendChild(none);
        return;
      }
      bases.forEach(function(base){
        var r = ph[base];
        var idtxt = r.id ? String(r.id).slice(0, 8) + '…' : 'no id recorded';
        var ls = r.last_sent || null;
        var lead = !ls ? '' : (ls.via === 'paste' ? ' · last copied for paste: “'
                       : (ls.via === 'continue' ? ' · last sent via --continue (destination unknown): “'
                       : ' · last sent: “'));
        var last = (ls && ls.item) ? lead + ls.item + '” ' + ago(ls.at)
                                   : (ls ? ' · last sent ' + ago(ls.at) : '');
        if(r.last_sync) last += ' · re-synced ' + ago(r.last_sync.at);
        var n = r.launches || 0;
        host.appendChild(el('div', 'session · ' + base + ' ' + idtxt +
          (r.started ? ' · started ' + ago(r.started) : '') + ' · ' + n + ' launch' + (n === 1 ? '' : 'es') + last +
          (r.previous && r.previous.length ? ' · ' + r.previous.length + ' earlier' : '')));
        var live = r.alive === true ? 'live' : (r.alive === false ? 'not running' : 'unknown');
        var tr = r.transcript === true ? 'found' : (r.transcript === false ? 'not found' : '—');
        host.appendChild(el('div', 'terminal: ' + live + ' · transcript: ' + tr + ' · Send will: ' + (r.send_will || '')));
        if(r.last_sent && r.last_sent.at && Date.now() - new Date(r.last_sent.at).getTime() > 86400000){
          var w = el('div', 'long gap since the last send — the session may hold stale beliefs; the follow-up already tells it to re-read the checklist');
          w.className = 'warn'; host.appendChild(w);
        }
        var btns = document.createElement('div');
        var f = document.createElement('button'); f.className = 'pcc-btn'; f.textContent = 'Forget session';
        f.title = 'Stop sending to this conversation. It is kept in the record’s history and its transcript is untouched; the next Open starts a new one.';
        f.addEventListener('click', function(){
          api('/api/session/forget', {phase: p.id, base: base}).then(function(d){
            if(!d.ok){ say(d.error || 'could not forget', 'err'); return; }
            say('session forgotten — the next Open starts a new one', 'ok'); loadSessions();
          });
        });
        btns.appendChild(f); btns.appendChild(attachBtn(base));
        host.appendChild(btns);
      });
    }
    sessWatchers.push(draw);
    draw();
  }

  window.__pccPhaseOpened__ = function(p, det){
    var act = det.querySelector('.dact');
    var msg = det.querySelector('.pbody > .dstatus');
    if(!act || !msg) return;
    function say(t, c){ msg.textContent = t || ''; msg.className = 'dstatus ' + (c||''); }
    actionRow(p, act, say, det);
    sessionStrip(p, det, say);
    wireTicks(det);

    // Branch and activity: what git says happened under this phase's code paths.
    var box = det.querySelector('.pactivity');
    if(box){
      box.innerHTML = '<span class="quiet">loading activity…</span>';
      api('/api/phase/activity', {phase: p.id}).then(function(d){
        if(!d.ok){ box.innerHTML = '<span class="dstatus err">'+esc(d.error)+'</span>'; return; }
        // the branch as it is now - the render may predate a checkout
        var bEl = det.querySelector('.pbranch');
        if(bEl && d.branch) bEl.textContent = d.branch;
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


def phase_gap(model: dict) -> dict:
    """Phase headings in the plan with no [[phase]] block, and whether the
    detected items mode is still unrecorded. The page asks the server to close
    the gap; it is the same additive sync Save runs."""
    proj = CFG.get("project", {}) or {}
    plan_rel = proj.get("plan", _pr.DEFAULT_PLAN)
    heads: list[str] = []
    if isinstance(plan_rel, str) and (REPO / plan_rel).is_file():
        try:
            heads = list(_pr.plan_phase_sections(
                (REPO / plan_rel).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            heads = []
    declared = {str(p["id"]) for p in model.get("phases", [])}
    return {"missing": [h for h in heads if h not in declared],
            "mode": model.get("items_mode") == "lists" and not proj.get("items"),
            "repo": str(REPO)}


def sync_phases_now() -> dict:
    """Add a [[phase]] block per undeclared heading and record a detected items
    mode. Additive: same plan, so nothing is retired."""
    global CFG
    r = _pr.apply_project_edits(REPO, {}, dry_run=False)
    if r.get("ok") and r.get("written"):
        try:
            CFG = tomllib.loads((REPO / "docs" / "progress.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return {"ok": False, "error": f"written, but could not be re-read: {exc}"}
        for n in r.get("notes", []):
            print("  phase sync: " + n)
    return r


def plan_switcher(model: dict) -> str:
    """A select in the bar when the project has more than one plan."""
    plans = model.get("plans") or []
    if len(plans) < 2:
        return ""
    cur = _pr.plan_key((model.get("project") or {}).get("plan", ""))
    opts = "".join(f'<option value="{_pr.e(p)}"{" selected" if _pr.plan_key(p) == cur else ""}>'
                   f'{_pr.e(p)}</option>' for p in plans)
    return ('<select id="pcc-plan" class="pcc-btn" aria-label="Active plan" title="This project '
            'has more than one plan. Switching keeps each plan\'s phases - days, dependencies, '
            'tickets - and its progress stays in its own file.">' + opts + '</select>')


TODAY_JS = r"""
// ------------------------------------------------------------ the hub --
// The project bar (every dashboard), the Today page, and a project's own
// "waiting on you here" strip - all drawn from /api/today, which reads every
// project on this machine's list. Nothing here is on a published page.
(function(){
  var HERE = window.__PCC_HUB_HERE__ || '';
  var T = window.__ANU_TOKEN__ || '';
  var KIND = {brief: ['Brief to confirm', 'wait'], turn: ['Your turn', 'wait'], change: ['Plan change', 'info'],
              task: ['Your task', ''], working: ['Session working', 'live'], blocker: ['Blocker', 'crit'],
              next: ['Next phase', '']};
  var ORDER = {brief: 0, change: 1, turn: 2, task: 3};

  function post(path, body){
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-PCC-Token': T},
      body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
  }
  function el(tag, cls, text){
    var n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function markEl(p, size){
    var s = el('span', 'pmark', p.mark);
    s.style.background = p.color;
    s.style.width = s.style.height = size + 'px';
    if(size >= 36) s.style.fontSize = '14px';
    s.setAttribute('aria-hidden', 'true');
    return s;
  }
  function fmtDate(iso){
    if(!iso) return '';
    var d = new Date(String(iso).slice(0, 10) + 'T00:00:00');
    if(isNaN(d.getTime())) return String(iso);
    var o = {month: 'short', day: 'numeric'};
    if(d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
    return d.toLocaleDateString(undefined, o);
  }
  function ago(iso){
    if(!iso) return '';
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if(isNaN(s)) return '';
    if(s < 60) return 'just now';
    if(s < 3600) return Math.round(s / 60) + ' min ago';
    if(s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' days ago';
  }
  // Where a project's page is: here, another port, or nowhere yet.
  function href(p, hash){
    hash = hash || '';
    if(p.current) return HERE === 'today' ? '/' + hash : (hash || '#');
    return p.url ? p.url + hash : '';
  }
  function say(msg, cls){
    var n = document.getElementById('pcc-msg');
    if(!n){
      n = el('div', 'dstatus'); n.id = 'pcc-msg'; n.setAttribute('role', 'status');
      var bar = document.getElementById('pcc-bar');
      if(bar) bar.appendChild(n); else document.body.insertBefore(n, document.body.firstChild);
    }
    n.textContent = msg || ''; n.className = 'dstatus ' + (cls || '');
  }
  function start(p, hash){
    say('starting the ' + p.name + ' dashboard in a new window\u2026');
    post('/api/projects/start', {path: p.path}).then(function(r){
      if(r && r.ok && r.url){ location.href = r.url + (hash || ''); return; }
      say((r && r.error) || 'could not start it', 'err');
    }).catch(function(){ say('this dashboard is not answering', 'err'); });
  }
  function go(p, hash){
    var h = href(p, hash);
    if(!h){ start(p, hash); return; }
    if(h.charAt(0) === '#'){ if(location.hash === h) hashGo(); else location.hash = h; }
    else location.href = h;
  }
  function actBtn(p, label, hash, primary){
    var live = p.current || p.running;
    var b = el('button', 'pcc-btn' + (primary ? ' run' : ''), live ? label : 'Start dashboard');
    b.type = 'button';
    if(!live) b.title = p.name + "'s dashboard is not running \u2014 this starts it, then opens it";
    b.addEventListener('click', function(){ go(p, hash); });
    return b;
  }
  function copy(text){
    if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function(res, rej){
      var t = el('textarea'); t.value = text; document.body.appendChild(t); t.select();
      try { document.execCommand('copy') ? res() : rej(); } catch(e){ rej(e); }
      t.remove();
    });
  }

  // ------------------------------------------------------- project bar
  function bar(d){
    var nav = document.getElementById('pcc-bar');
    if(!nav){
      nav = el('nav'); nav.id = 'pcc-bar'; nav.setAttribute('aria-label', 'Projects');
      document.body.insertBefore(nav, document.body.firstChild);
    }
    var msg = document.getElementById('pcc-msg');
    nav.textContent = '';
    var inner = el('div', 'pcc-bar-in'); nav.appendChild(inner);
    if(msg) nav.appendChild(msg);
    var tw = d.totals.waiting;
    var t = el('a', 'pcc-tab' + (HERE === 'today' ? ' on' : ''));
    t.href = '/today';
    t.setAttribute('aria-label', 'Today, ' + tw + ' waiting on you');
    if(HERE === 'today'){ t.setAttribute('aria-current', 'page'); t.style.borderBottomColor = 'var(--ink)'; }
    t.appendChild(el('span', '', 'Today'));
    if(tw){ var c = el('span', 'pcount', String(tw)); c.setAttribute('aria-hidden', 'true'); t.appendChild(c); }
    inner.appendChild(t);
    d.projects.forEach(function(p){
      if(!p.configured) return;
      var on = p.current && HERE !== 'today';
      var live = p.current || p.running;
      var a = el('a', 'pcc-tab' + (on ? ' on' : ''));
      a.href = href(p, '') || '#';
      a.setAttribute('aria-label', p.name + (p.waiting.length ? ', ' + p.waiting.length + ' waiting' : '') +
                                  (live ? '' : ', dashboard not running'));
      if(on){
        a.setAttribute('aria-current', 'page');
        a.style.borderBottomColor = p.color;
        a.style.background = 'color-mix(in srgb, ' + p.color + ' 12%, transparent)';
      }
      a.appendChild(markEl(p, 22));
      a.appendChild(el('span', 'tabname', p.name));
      if(p.waiting.length){
        var b = el('span', 'pcount', String(p.waiting.length)); b.setAttribute('aria-hidden', 'true'); a.appendChild(b);
      }
      if(!live){
        var o = el('span', 'quiet tabname', 'off'); o.setAttribute('aria-hidden', 'true'); a.appendChild(o);
        a.addEventListener('click', function(ev){ ev.preventDefault(); start(p, ''); });
      }
      inner.appendChild(a);
    });
  }

  // ------------------------------------------------------------ Today
  function rowEl(p, r){
    var li = el('li', 'row');
    li.appendChild(markEl(p, 28));
    var txt = el('div', 'txt');
    var meta = el('div', 'meta');
    var k = KIND[r.kind] || [r.kind, ''];
    meta.appendChild(el('span', 'kpill ' + k[1], k[0]));
    var who = el('span');
    var pl = el('a', '', p.name);
    var ph = href(p, '');
    pl.href = ph || '#';
    if(!ph) pl.addEventListener('click', function(ev){ ev.preventDefault(); start(p, ''); });
    who.appendChild(pl);
    var when = r.at ? ago(r.at) : '';
    if(when) who.appendChild(document.createTextNode(' \u00b7 ' + when));
    meta.appendChild(who);
    txt.appendChild(meta);
    txt.appendChild(el('p', 'ttl', r.title));
    if(r.detail) txt.appendChild(el('p', 'det', r.detail));
    li.appendChild(txt);
    var acts = el('div', 'acts');
    var hash = r.phase ? '#phase-' + r.phase : '';
    if(r.kind === 'task'){
      var mk = el('button', 'pcc-btn', 'Mark done'); mk.type = 'button';
      mk.title = 'Ticks this item in ' + p.name + "'s plan";
      mk.addEventListener('click', function(){
        mk.disabled = true;
        post('/api/today/tick', {path: p.path, file: r.file, raw: r.raw, state: 'done'}).then(function(x){
          if(x && x.ok){ say('ticked in ' + p.name, 'ok'); refresh(); }
          else { mk.disabled = false; say((x && x.error) || 'could not tick it', 'err'); }
        }).catch(function(){ mk.disabled = false; say('this dashboard is not answering', 'err'); });
      });
      acts.appendChild(mk);
    } else if(r.kind === 'change'){
      acts.appendChild(actBtn(p, 'Review change', '#plan-changes', false));
    } else if(r.kind === 'blocker'){
      acts.appendChild(actBtn(p, 'Open risks', '#risks', false));
    } else if(r.kind === 'next'){
      acts.appendChild(actBtn(p, 'Read in', hash, false));
    } else {
      acts.appendChild(actBtn(p, r.phase ? 'Open phase' : 'Open project', hash, r.kind === 'brief'));
    }
    if(r.resume){
      var cp = el('button', 'pcc-btn', 'Copy resume command'); cp.type = 'button';
      cp.title = 'For when its window is gone: ' + r.resume;
      cp.addEventListener('click', function(){
        copy(r.resume).then(function(){ say('resume command copied \u2014 run it in a terminal', 'ok'); },
                            function(){ say('the clipboard refused it', 'err'); });
      });
      acts.appendChild(cp);
    }
    li.appendChild(acts);
    return li;
  }
  function today(d){
    var root = document.getElementById('pcc-today'); if(!root) return;
    root.textContent = '';
    var wrap = el('div', 'today');
    var main = el('main'), side = el('aside');
    side.setAttribute('aria-label', 'Project status');
    main.appendChild(el('h1', '', 'Today'));
    var waiting = [], working = [], coming = [];
    d.projects.forEach(function(p){
      p.waiting.forEach(function(r){ waiting.push([p, r]); });
      p.working.forEach(function(r){ working.push([p, r]); });
      p.coming.forEach(function(r){ coming.push([p, r]); });
    });
    waiting.sort(function(a, b){
      var o = (ORDER[a[1].kind] === undefined ? 9 : ORDER[a[1].kind]) - (ORDER[b[1].kind] === undefined ? 9 : ORDER[b[1].kind]);
      return o || String(a[1].at || '').localeCompare(String(b[1].at || ''));
    });
    var np = d.totals.projects_waiting;
    var sub = waiting.length ? waiting.length + (waiting.length === 1 ? ' thing waits' : ' things wait') +
              ' on you in ' + np + (np === 1 ? ' project' : ' projects') : 'All clear';
    if(working.length) sub += ' \u00b7 ' + working.length + (working.length === 1 ? ' session' : ' sessions') + ' working';
    main.appendChild(el('p', 'tdsub', sub));
    if(!waiting.length){
      var e = el('div', 'tdempty');
      e.appendChild(el('h2', '', 'Nothing waits on you'));
      e.appendChild(el('p', 'quiet', 'No brief to confirm, no plan change to review, no task marked for you.'));
      var ea = el('div', 'acts');
      d.projects.forEach(function(p){ if(p.configured && (p.current || p.running)) ea.appendChild(actBtn(p, 'Open ' + p.name, '', false)); });
      if(ea.childNodes.length) e.appendChild(ea);
      main.appendChild(e);
    }
    if(waiting.length || working.length || coming.length){
      var ul = el('ul', 'inbox');
      var group = function(title, hint, list){
        if(!list.length) return;
        var g = el('li', 'grp');
        g.appendChild(el('h2', 'eyebrow', title + ' \u00b7 ' + list.length));
        g.appendChild(el('span', 'quiet', hint));
        ul.appendChild(g);
        list.forEach(function(x){ ul.appendChild(rowEl(x[0], x[1])); });
      };
      group('Waiting on you', 'most pressing first', waiting);
      group('Working', 'no action needed', working);
      group('Coming up', 'blockers and the next phase', coming);
      main.appendChild(ul);
    }
    var box = el('div', 'projs');
    box.appendChild(el('h2', 'eyebrow', 'Projects'));
    var lst = el('ul');
    d.projects.forEach(function(p){
      var li = el('li'), top = el('div', 'prow');
      top.appendChild(markEl(p, 28));
      var nm = el('a', 'pname', p.name), h = href(p, '');
      nm.href = h || '#';
      if(!h) nm.addEventListener('click', function(ev){ ev.preventDefault(); start(p, ''); });
      top.appendChild(nm);
      if(p.current) top.appendChild(el('span', 'kpill live', 'this dashboard'));
      else if(p.running) top.appendChild(el('span', 'kpill live', 'running :' + ((/:(\d+)\//.exec(p.url) || [])[1] || '')));
      else {
        var sb = el('button', 'pcc-btn', 'Start dashboard'); sb.type = 'button';
        sb.addEventListener('click', function(){ start(p, ''); });
        top.appendChild(sb);
      }
      li.appendChild(top);
      var info = el('div', 'pinfo');
      if(p.configured && !p.error){
        var pb = el('div', 'pbar'), fill = el('span');
        fill.style.width = (p.pct || 0) + '%'; fill.style.background = p.color;
        pb.appendChild(fill); info.appendChild(pb);
        info.appendChild(el('p', 'quiet', (p.pct || 0) + '% \u00b7 ' + (p.phase || 'no current phase') +
                                          (p.finish ? ' \u00b7 finish ' + fmtDate(p.finish) : '')));
      } else {
        info.appendChild(el('p', 'quiet', p.error || 'not set up yet \u2014 open it to run Setup'));
      }
      li.appendChild(info);
      lst.appendChild(li);
    });
    box.appendChild(lst);
    side.appendChild(box);
    wrap.appendChild(main); wrap.appendChild(side);
    root.appendChild(wrap);
  }

  // ------------------------------------------- this project's own strip
  function summarize(list){
    var n = {brief: 0, turn: 0, change: 0, task: 0}, bp = '';
    list.forEach(function(r){ n[r.kind] = (n[r.kind] || 0) + 1; if(r.kind === 'brief' && !bp) bp = r.phase; });
    var parts = [];
    if(n.brief) parts.push(n.brief === 1 ? (bp ? 'the Phase ' + bp + ' brief to confirm' : 'a brief to confirm') : n.brief + ' briefs to confirm');
    if(n.change) parts.push(n.change + (n.change === 1 ? ' plan change' : ' plan changes') + ' to review');
    if(n.turn) parts.push(n.turn + (n.turn === 1 ? ' session waiting' : ' sessions waiting') + ' for your reply');
    if(n.task) parts.push(n.task + (n.task === 1 ? ' task' : ' tasks') + ' marked for you');
    if(parts.length < 2) return parts.join('');
    return parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1];
  }
  function here(d){
    var me = d.projects.filter(function(p){ return p.current; })[0];
    if(!me) return;
    var hd = document.querySelector('.wrap > header');
    if(hd && !hd.querySelector('.pidrow') && hd.firstElementChild){
      var row = el('div', 'pidrow'), left = hd.firstElementChild;
      hd.insertBefore(row, left);
      row.appendChild(markEl(me, 44));
      row.appendChild(left);
    }
    var old = document.getElementById('pcc-here');
    if(old) old.remove();
    if(!me.waiting.length) return;
    var tiles = document.querySelector('.wrap > .tiles');
    if(!tiles) return;
    var s = el('section', 'pcc-here'); s.id = 'pcc-here';
    s.setAttribute('aria-label', 'Waiting on you in this project');
    var p = el('p');
    p.appendChild(el('b', '', me.waiting.length + (me.waiting.length === 1 ? ' thing waits' : ' things wait') + ' on you here: '));
    p.appendChild(document.createTextNode(summarize(me.waiting) + '.'));
    s.appendChild(p);
    var acts = el('div', 'acts');
    var fb = me.waiting.filter(function(r){ return (r.kind === 'brief' || r.kind === 'turn') && r.phase; })[0];
    if(fb) acts.appendChild(actBtn(me, 'Open Phase ' + fb.phase, '#phase-' + fb.phase, true));
    if(me.waiting.some(function(r){ return r.kind === 'change'; })) acts.appendChild(actBtn(me, 'Review changes', '#plan-changes', false));
    var tl = el('a', 'pcc-btn', 'Today'); tl.href = '/today'; tl.style.textDecoration = 'none';
    acts.appendChild(tl);
    s.appendChild(acts);
    tiles.parentNode.insertBefore(s, tiles);
  }

  // Links from Today land on a phase, the plan changes or the risks.
  function hashGo(){
    var h = location.hash || '';
    var m = /^#phase-([\w.-]+)$/.exec(h);
    if(m){
      var a = document.querySelector('[data-phase-actions="' + m[1] + '"]');
      var det = a && a.closest('details');
      if(det){ det.open = true; setTimeout(function(){ det.scrollIntoView({block: 'start'}); }, 60); }
      return;
    }
    if(h === '#plan-changes'){
      var tries = 0;
      (function find(){
        var box = document.querySelector('.pchanges');
        var btn = box && [].slice.call(box.querySelectorAll('button')).filter(function(b){ return /^Review /.test(b.textContent); })[0];
        if(btn) btn.click();
        if(box && box.childNodes.length){ box.scrollIntoView({block: 'start'}); return; }
        if(++tries < 20) setTimeout(find, 300);
      })();
      return;
    }
    if(h === '#risks'){
      var t = document.getElementById('tab-risk');
      if(t){ t.click(); t.scrollIntoView({block: 'start'}); }
    }
  }

  function refresh(){
    fetch('/api/today').then(function(r){ return r.json(); }).then(function(d){
      if(!d || !d.ok) return;
      bar(d);
      if(HERE === 'today') today(d); else here(d);
    }).catch(function(){});
  }
  if(HERE !== 'today'){ hashGo(); window.addEventListener('hashchange', hashGo); }
  refresh();
  setInterval(function(){ if(document.visibilityState === 'visible') refresh(); }, HERE === 'today' ? 20000 : 60000);
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
        '<button class="pcc-btn" id="pcc-replan-all" '
        'title="Reassess the whole plan in a code session">Re-plan…</button>'
        + plan_switcher(model) +
        '<a class="pcc-btn" href="/setup" style="text-decoration:none;margin-left:auto"'
        ' title="configure this machine and this project">Setup</a>'
        '<span class="pcc-local">actions live</span></div>'
        "<script>window.__ANU_TOKEN__=" + _pr.js(token) + ";"
        "window.__ANU_FRESH0__=" + _pr.js(fresh_stamp()) + ";"
        "window.__ANU_ITEMS__=" + _pr.js(idx) + ";"
        "window.__ANU_PHASE_GAP__=" + _pr.js(phase_gap(model)) + ";"
        "window.__ANU_PLAN__=" + _pr.js({
            "id": PLAN_ID, "name": (model.get("project") or {}).get("plan", "the plan"),
            "jira": model.get("plan_ticket", ""),
            "jira_api_ready": bool(jira_target().get("configured")),
            "agent": ({"name": model["agent"]["name"],
                       "sources": len(model["agent"]["resolved"]),
                       "missing": model["agent"]["missing"],
                       "files": [t for t in ("claude", "opencode") if
                                 (REPO / (".claude" if t == "claude" else ".opencode") / "agents"
                                  / (model["agent"]["name"] + ".md")).is_file()]}
                      if model.get("agent") else None),
            "jira_create_tmpl": ("" if model.get("plan_ticket") else
                                 ((CFG.get("integrations", {}) or {}).get("jira", {}) or {})
                                 .get("create_url", ""))}) + ";"
        "window.__ANU_PROFILE_TOOL__=" + _pr.js(
            (_pr.load_user_profile() or {}).get("tool", "")) + ";"
        "window.__ANU_PROVIDERS__=" + _pr.js(
              [c.get("name", "") for c in (CFG.get("context") or []) if c.get("name")]) + ";"
          "window.__ANU_LAUNCHERS__=" + _pr.js(
            {k: {"label": v["label"], "mode": v.get("mode", ""),
                 "base": v.get("base", k), "warm": bool(v.get("warm"))}
             for k, v in LAUNCHERS.items()}) + ";</script>"
        "<script>" + JS + "</script>"
        "<script>" + TODAY_JS + "</script>")


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
        var abs = /^([A-Za-z]:[\\/]|\/)/.test(f);
        var inside = abs && (E.repo||'') && f.replace(/\\/g,'/').toLowerCase().indexOf(String(E.repo).replace(/\\/g,'/').toLowerCase().replace(/\/$/,'') + '/') === 0;
        if(abs && !inside){
          boxNote.className='why planboxes nobox';
          boxNote.innerHTML='<b>Outside this project.</b> This page serves <code>'+esc(E.repo)+'</code>, and '+
            'phases, git activity, agent files and prompts are all derived from that folder - a plan in '+
            'another checkout would be read from there while everything else points here. Save will refuse it. '+
            'To work on that checkout, open it from the <b>Projects</b> tab.';
          return;
        }
        boxNote.innerHTML = (abs ? 'inside this project \u2014 saved as a path relative to it \u00b7 ' : '') +
          'not in the last scan \u2014 press <b>Rescan</b> if you just added it';
        return;
      }
      // The one failure this field exists to prevent, said BEFORE it happens
      // rather than after it has read 0% for a week.
      var hit = RAW.filter(function(c){ return c.file === f })[0] || {};
      var ph = hit.phases || 0, declared = P.phase_count || 0;
      var head = n
        ? '<b>'+n+'</b> checkbox'+(n===1?'':'es')+
          (ph ? ' · <b>'+ph+'</b> phase heading'+(ph===1?'':'s') : '')+' in this file'
        : ((hit.items && ph)
          ? '<b>No checkboxes</b> · <b>'+ph+'</b> phase heading'+(ph===1?'':'s')+
            ' with <b>'+hit.items+'</b> list item'+(hit.items===1?'':'s')+' — tracked as '+
            'written: each top-level list entry under a phase heading is an item, and '+
            'ticking one writes <code>[x]</code> into its line. Saving records '+
            '<code>items = "lists"</code>.'
          : '<b>No checkboxes in this file</b>, and no list items under '+
            '<code>### Phase &lt;id&gt;</code> headings, so this plan would read '+
            '<b>0% forever</b>. Choose another file, or give it phase headings.');
      var trackable = n || (hit.items && ph);
      // Checkboxes alone render an EMPTY dashboard: phases are declared in
      // docs/progress.toml ([[phase]] id/days/depends_on - what markdown cannot
      // say), and each pulls its checklist from its "### Phase <id>" section.
      // Without saying so here, "31 checkboxes but no phases" is a mystery.
      var tail = '';
      if(trackable && !declared){
        // The config's [[phase]] blocks are generated from the headings - on
        // Save, and by the dashboard itself when it finds headings undeclared.
        tail = ph
          ? '<div class="why" style="margin-top:4px">No phases declared yet: Save (or opening '+
            'the dashboard) adds a <code>[[phase]]</code> block for each of the <b>'+ph+'</b> '+
            'phase heading'+(ph===1?'':'s')+', with days and dependencies as TODO placeholders.</div>'
          : '<div class="ready no">The dashboard will show <b>no phases</b>: this file has no '+
            '<code>### Phase &lt;id&gt;: name</code> headings to derive them from.</div>';
      } else if(trackable && declared && ph){
        tail = '<div class="why" style="margin-top:4px">'+declared+' phase'+
          (declared===1?'':'s')+' declared in the config</div>';
      }
      boxNote.className = 'why planboxes' + (trackable ? '' : ' nobox');
      boxNote.innerHTML = head + tail;
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
    var pcm = P.pace || null;
    var paceIn = inp('p-pace', P.active_days_per_week, pcm && pcm.pace_measured ? 'measured: ' + pcm.pace_measured : 'e.g. 3', 'number');
    paceIn.min = '0.5'; paceIn.max = '7'; paceIn.step = '0.5';
    box.appendChild(row('active_days_per_week','Active days per week', paceIn,
      'How many days a week you actually work on this plan. It sets the calendar: items left \u00f7 items per active day '+
      '\u00f7 this \u00d7 7. Leave it empty to use the <b>measured</b> value from the snapshot history'+
      (pcm ? ' \u2014 currently <b>'+pcm.pace+'</b> ('+pcm.pace_src+'), '+pcm.active_days+' active day(s) in '+pcm.span_days : '')+'.',
      !!P.active_days_per_week));
    var ipdIn = inp('p-ipd', P.items_per_active_day, pcm && pcm.rate_all ? 'measured: ' + pcm.rate_all : 'e.g. 2', 'number');
    ipdIn.min = '0.1'; ipdIn.step = '0.1';
    box.appendChild(row('items_per_active_day','Items per active day', ipdIn,
      'One checklist item is one brief-and-confirm cycle. Leave it empty to use the <b>measured</b> rate'+
      (pcm ? ' \u2014 currently <b>'+pcm.rate+'</b> ('+pcm.rate_src+')'+(pcm.rate_recent ? ', last two weeks '+pcm.rate_recent : '') : '')+
      '. Set it only to plan against a pace you intend rather than the one observed.', !!P.items_per_active_day));
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

    // ---- The plan's agent: defined here, generated from here. ------------
    // One object per plan in docs/progress.toml; this block edits it. Sources
    // are POINTERS (paths, globs, URLs, MCP names, prior plans) - the agent
    // reads them on demand, nothing is copied anywhere.
    var A = P.agent || null;
    var agentState = {remove: false};
    if(A){
      var abody = el('div',{class:'abody'});
      var ahead = el('h3',{text:'Agent for this plan'});
      ahead.style.cssText = 'margin:18px 0 4px;font-size:13px';
      box.appendChild(ahead);
      box.appendChild(el('div',{class:'why',html:
        'A named agent whose knowledge base is this plan\u2019s. Saved as '+
        '<code>[plans."'+esc(A.plan)+'".agent]</code>; Save then writes '+
        '<code>.claude/agents/&lt;name&gt;.md</code> and <code>.opencode/agents/&lt;name&gt;.md</code>, '+
        'and cold launches of those tools start as the agent. '+
        (A.declared ? 'Currently <b>'+esc(A.name)+'</b>'+
           (A.file_claude||A.file_opencode ? ' \u00b7 files for '+[A.file_claude?'claude':'',A.file_opencode?'opencode':''].filter(Boolean).join(', ') : ' \u00b7 no files yet')+'.'
         : 'This plan has <b>no agent yet</b>.')}));
      var enable = el('input',{type:'checkbox',id:'p-ag-on'}); enable.checked = A.declared;
      var enableRow = el('div',{class:'row'},[enable, el('label',{class:'k',text:'Define an agent',for:'p-ag-on'}),
        el('div',{class:'v'},[el('div',{class:'why',text:'ticked: the fields below are saved with the config; unticked: nothing about the agent is sent'})])]);
      box.appendChild(enableRow);
      box.appendChild(abody);
      function arow(label, ctl, why){
        var v = el('div',{class:'v'}); v.appendChild(ctl);
        if(why) v.appendChild(el('div',{class:'why',html:why}));
        return el('div',{class:'row'},[el('span'), el('label',{class:'k',text:label}), v]);
      }
      abody.appendChild(arow('Name', inp('p-ag-name', A.name, 'plan-my-topic'),
        'lowercase letters, digits and dashes \u2014 it becomes the file name and the <code>--agent</code> argument'));
      var desc = el('textarea',{id:'p-ag-desc',rows:'2',placeholder:'Owns the X migration. Use for anything touching A, B or C.'});
      desc.value = A.description || '';
      abody.appendChild(arow('Description', desc,
        'doubles as the <b>trigger phrase</b>: name the topics it owns, so a tool picks it for matching work'));
      abody.appendChild(arow('Model', inp('p-ag-model', A.model, 'optional \u2014 e.g. opus'),
        'optional; Claude Code takes a model alias, opencode a provider/model id'));

      // one-per-line lists with Browse, and checkable suggestions beneath
      var S = A.sources || {}, C = A.candidates || {};
      function listArea(id, kind, lines, ph, want){
        var ta = el('textarea',{id:id,rows:'3',placeholder:ph,spellcheck:'false'});
        ta.value = (lines||[]).join('\n');
        var wrap = el('div',{},[ta]);
        if(want){
          var bar = el('div',{class:'bar'});
          var probe = el('input',{type:'hidden'});
          bar.appendChild(pathPicker(probe, want, E.repo));
          function take(){
            var v = (probe.value||'').trim(); if(!v) return;
            v = v.replace(/\\/g,'/');
            if(want === 'dir' && !/\/$/.test(v)) v += '/';
            var cur = ta.value.split('\n').map(function(s){return s.trim()}).filter(Boolean);
            if(cur.indexOf(v) < 0){ cur.push(v); ta.value = cur.join('\n'); ta.dispatchEvent(new Event('input',{bubbles:true})); }
            probe.value = '';
          }
          probe.addEventListener('input', take); probe.addEventListener('change', take);
          wrap.appendChild(bar);
        }
        var sug = el('div',{class:'why'}); wrap.appendChild(sug);
        wrap._ta = ta; wrap._sug = sug; wrap._kind = kind;
        return wrap;
      }
      function chips(wrap, items, label){
        wrap._sug.textContent = '';
        if(!items || !items.length){ return; }
        var cur = wrap._ta.value.split('\n').map(function(s){return s.trim()});
        var fresh = items.filter(function(x){ return cur.indexOf(x) < 0; });
        if(!fresh.length){ return; }
        wrap._sug.appendChild(el('span',{text: label + ' '}));
        fresh.forEach(function(x){
          var c = el('label',{class:'chip',style:'cursor:pointer;margin:2px 4px 2px 0;display:inline-block'});
          var cb = el('input',{type:'checkbox'}); cb.style.marginRight='4px';
          c.appendChild(cb); c.appendChild(document.createTextNode(x));
          cb.addEventListener('change', function(){
            var lines = wrap._ta.value.split('\n').map(function(s){return s.trim()}).filter(Boolean);
            var i = lines.indexOf(x);
            if(cb.checked && i < 0) lines.push(x);
            if(!cb.checked && i >= 0) lines.splice(i,1);
            wrap._ta.value = lines.join('\n');
            wrap._ta.dispatchEvent(new Event('input',{bubbles:true}));
          });
          wrap._sug.appendChild(c);
        });
      }
      var wFiles = listArea('p-ag-files','files', S.files, 'docs/decisions/01[4-9]-*.md\none path or glob per line', 'md');
      var wDirs  = listArea('p-ag-dirs','dirs', S.dirs, 'modules/connect/\none folder per line', 'dir');
      var wUrls  = listArea('p-ag-urls','urls', S.urls, 'https://docs.example.com/\none URL per line \u2014 listed for the agent, never fetched here', null);
      var wPlans = listArea('p-ag-plans','plans', S.plans, 'OLD-PLAN.md\nprior plans, read-only context', 'md');
      abody.appendChild(arow('Files', wFiles, 'paths or globs, relative to the repo'));
      abody.appendChild(arow('Folders', wDirs, ''));
      abody.appendChild(arow('URLs', wUrls, ''));
      // MCP: a checklist of what the project already declares, plus free names
      var mcpWrap = el('div',{});
      var mcpKnown = (C.mcp||[]).slice();
      (S.mcp||[]).forEach(function(n){ if(mcpKnown.indexOf(n)<0) mcpKnown.push(n); });
      mcpKnown.forEach(function(n){
        var c = el('label',{class:'chip',style:'cursor:pointer;margin:2px 6px 2px 0;display:inline-block'});
        var cb = el('input',{type:'checkbox'}); cb.value = n; cb.className='p-ag-mcp'; cb.checked = (S.mcp||[]).indexOf(n)>=0;
        cb.style.marginRight='4px'; c.appendChild(cb); c.appendChild(document.createTextNode(n));
        mcpWrap.appendChild(c);
      });
      if(!mcpKnown.length) mcpWrap.appendChild(el('span',{class:'why',text:'no MCP servers declared in [[context]], .mcp.json or opencode.json'}));
      abody.appendChild(arow('MCP servers', mcpWrap, 'from <code>[[context]]</code>, <code>.mcp.json</code> and <code>opencode.json</code>; opencode is allowed only these'));
      abody.appendChild(arow('Prior plans', wPlans, 'other plans of this project, as read-only context'));

      // suggest: from the plan text, the phases' modules, decision records
      var sbar = el('div',{class:'bar'});
      var suggest = el('button',{class:'act',text:'Suggest sources'});
      suggest.title = 'Offer paths the plan mentions, the phases\u2019 modules, decision records that name the plan, every declared MCP server and the other plans \u2014 as ticks, nothing is written';
      suggest.addEventListener('click', function(){
        chips(wFiles, C.files, 'mentioned in the plan or naming it:');
        chips(wDirs,  C.dirs,  'folders the plan or its phases name:');
        chips(wPlans, C.plans, 'other plans:');
        document.querySelectorAll('.p-ag-mcp').forEach(function(cb){ if(!cb.checked) cb.parentNode.style.outline='1px dashed var(--accent)'; });
        var n = (C.files||[]).length + (C.dirs||[]).length + (C.plans||[]).length + (C.mcp||[]).length;
        say('#sw-pmsg', n ? n + ' suggestion(s) shown as ticks \u2014 tick what applies, then Save' : 'nothing to suggest: the plan names no existing paths and no decision record names it', n ? '' : 'err');
      });
      sbar.appendChild(suggest);
      if(A.declared){
        var rm = el('button',{class:'act',text:'Remove agent'});
        rm.title = 'Comments the agent tables out of docs/progress.toml on Save (dated, not deleted); the generated files are left for you to delete';
        rm.addEventListener('click', function(){
          agentState.remove = !agentState.remove;
          rm.textContent = agentState.remove ? 'Removal pending \u2014 click to keep' : 'Remove agent';
          abody.style.opacity = agentState.remove ? '.45' : '';
          say('#sw-pmsg', agentState.remove ? 'the agent will be removed on Save' : 'removal cancelled', '');
          $('#sw-writecard').classList.add('dirty');
        });
        sbar.appendChild(rm);
      }
      abody.appendChild(arow('', sbar, ''));

      // how the declared sources resolve right now
      if(A.declared && (A.resolved||[]).length){
        var res = el('div',{class:'why'});
        res.innerHTML = '<b>Now:</b> ' + A.resolved.map(function(s){
          var st = s.ok === true ? 'ok' : (s.ok === false ? 'MISSING' : 'listed');
          return '<code>'+esc(s.spec)+'</code> '+(s.kind==='file' && s.ok ? s.paths.length+' file'+(s.paths.length===1?'':'s') : st);
        }).join(' \u00b7 ');
        abody.appendChild(arow('', res, ''));
      }
      function syncEnable(){ abody.style.display = enable.checked ? '' : 'none'; }
      enable.addEventListener('change', function(){ syncEnable(); $('#sw-writecard').classList.add('dirty'); say('#sw-pmsg','Unsaved changes.',''); });
      syncEnable();
    }
    window.__agentPayload__ = function(){
      if(!A) return null;
      if(agentState.remove) return {remove: true};
      if(!$('#p-ag-on') || !$('#p-ag-on').checked) return null;
      function lines(id){ var n=$('#'+id); return n ? n.value.split('\n').map(function(s){return s.trim()}).filter(Boolean) : []; }
      return {name: val('p-ag-name'), description: ($('#p-ag-desc')||{}).value||'', model: val('p-ag-model'),
              sources: {files: lines('p-ag-files'), dirs: lines('p-ag-dirs'), urls: lines('p-ag-urls'),
                        mcp: [].slice.call(document.querySelectorAll('.p-ag-mcp')).filter(function(c){return c.checked}).map(function(c){return c.value}),
                        plans: lines('p-ag-plans')}};
    };

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

    // Rows adopted from one host survive a scan of another: harvest ticked
  // rows into KEPT before each scan, and render leftovers as sticky rows.
  // One resource can live on one IP and the rest on another - the scan host
  // is just a probe convenience, never the shape of the config.
  var KEPT = {};
  function harvestScan(){
    document.querySelectorAll('#sw-svc tbody tr').forEach(function(tr){
      if(!tr._name || tr._configured) return;
      if(tr._cb.checked){
        KEPT[tr._name] = {label: tr._label, what: tr._what, host: tr._host,
                          url: tr._url.value, kind: tr._kind.value,
                          auth_env: tr._auth.value};
      } else {
        delete KEPT[tr._name];
      }
    });
  }
  function scanRow(t, name, label, what, chip, chipCls, vals, host, configured, ticked){
    var cb=el('input',{type:'checkbox'});
    cb.checked = ticked; cb.disabled = configured;
    var st = el('span',{class:'chip '+chipCls, text:chip});
    var url=inp('', vals.url); var kind=sel('', vals.kind,
      ['mcp-stateless-http','mcp-stateful-http','prompt-only']);
    var auth=inp('', vals.auth_env,'env var name');
    url.disabled=kind.disabled=auth.disabled=configured;
    var tr=el('tr',{class:cb.checked?'':'off'},[
      el('td',{},[cb]),
      el('td',{},[el('div',{text:label}),el('div',{class:'why',text:what})]),
      el('td',{},[st]), el('td',{},[url]), el('td',{},[kind]), el('td',{},[auth])]);
    cb.addEventListener('change',function(){tr.classList.toggle('off',!cb.checked)});
    tr._name=name; tr._label=label; tr._what=what; tr._host=host;
    tr._configured=configured; tr._cb=cb; tr._url=url; tr._kind=kind; tr._auth=auth;
    t.appendChild(tr);
  }
  function renderScan(rows, host){
    var t=$('#sw-svc tbody'); t.textContent='';
    var leftover = {}; Object.keys(KEPT).forEach(function(k){ leftover[k]=KEPT[k] });
    rows.forEach(function(s){
      // "configured" alone hid reachability - you could not see your own
      // tunnel working - and never said WHAT covers the row, so an already
      // adopted service read as one you were being refused. Both, always:
      var live = s.up ? 'up '+s.ms+'ms' : 'no answer';
      var chip = s.configured ? 'configured · '+live : live;
      var cls  = s.configured ? 'have' : (s.up ? 'up' : 'down');
      var what = s.configured
        ? s.what+' · already adopted as “'+(s.configured_as||s.name)+
          '” — nothing to add here; sessions use it whenever the probe is green'
        : s.what;
      var kept = KEPT[s.name];
      if(kept && kept.host === host){
        // same service, same host: show it with the edits it was kept with
        delete leftover[s.name];
        scanRow(t, s.name, s.label, what, chip, cls,
                kept, host, s.configured, !s.configured);
        return;
      }
      scanRow(t, s.name, s.label, what, chip, cls,
              {url:s.url, kind:s.kind, auth_env:s.auth_env},
              host, s.configured, s.up && !s.configured);
    });
    // rows adopted from OTHER hosts stay on screen and stay picked
    Object.keys(leftover).forEach(function(name){
      var k = leftover[name];
      scanRow(t, name, k.label, k.what, 'kept \u00b7 '+k.host, 'up',
              k, k.host, false, true);
    });
    $('#sw-svc').style.display =
      (rows.length || Object.keys(leftover).length) ? 'table' : 'none';
  }
  function pickedContexts(){
    var out=[], seen={};
    document.querySelectorAll('#sw-svc tbody tr').forEach(function(tr){
      if(!tr._name || tr._configured || !tr._cb.checked) return;
      var name = tr._name;
      if(seen[name]){
        // the same service adopted from two hosts: both are real, so the
        // second keeps its identity by carrying the host in its name
        name = name + '-' + String(tr._host||'').replace(/[^A-Za-z0-9-]+/g,'-');
      }
      seen[name]=1;
      out.push({name:name, label:tr._label, kind:tr._kind.value,
                url:tr._url.value, auth_env:tr._auth.value, probe:true});
    });
    return out;
  }
  function projFields(){
    var f={};
    if(on('name','#sw-proj')) f.name=val('p-name');
    if(on('plan')) f.plan=val('p-plan');
    if(on('owner')) f.owner=val('p-owner');
    if(on('start_date')) f.start_date=val('p-start');
    if(on('active_days_per_week') && val('p-pace')) f.active_days_per_week=val('p-pace');
    if(on('items_per_active_day') && val('p-ipd')) f.items_per_active_day=val('p-ipd');
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
      var btn=this; harvestScan(); btn.disabled=true; say('#sw-smsg','scanning\u2026');
      api('/api/setup/scan',{host:$('#sw-host').value.trim()||'127.0.0.1'}).then(function(d){
        btn.disabled=false;
        if(!d.ok){say('#sw-smsg',d.error,'err');return}
        var up=d.services.filter(function(s){return s.up}).length;
        say('#sw-smsg',up+' of '+d.services.length+' answered \u00b7 a port that answers proves '+
            'something is listening, not that it is what we named it','');
        renderScan(d.services, d.host);
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
      api('/api/setup/project',{fields:projFields(),contexts:pickedContexts(),agent:(window.__agentPayload__?window.__agentPayload__():null),apply:false})
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
      api('/api/setup/project',{fields:projFields(),contexts:pickedContexts(),agent:(window.__agentPayload__?window.__agentPayload__():null),apply:true})
        .then(function(d){
          b.disabled = false;
          if(!d.ok){ say('#sw-pmsg',d.error,'err'); return }
          if(d.agent_files){
            var af = d.agent_files, ar = d.agent_resolved;
            var bits = [];
            if(af.written && af.written.length) bits.push('agent files written: ' + af.written.join(', '));
            if(af.skipped && af.skipped.length) bits.push(af.skipped.join('; '));
            if(ar) bits.push(ar.name + ': ' + ar.sources + ' source' + (ar.sources===1?'':'s') + (ar.missing.length ? ', MISSING: ' + ar.missing.join(', ') : ', all found'));
            if(af.error) bits.push('agent files: ' + af.error);
            if(bits.length) setTimeout(function(){ say('#sw-pmsg', 'Saved. ' + bits.join(' \u00b7 '), (ar && ar.missing.length) || af.error ? 'err' : 'ok'); }, 0);
            // the view (resolved sources, file state) is stale after a write
            setTimeout(function(){ load(); }, 1500);
          }
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


def switch_plan(plan: str) -> dict:
    """Make another of this project's plans the active one. Only a plan the
    config already knows, and only if its file still exists."""
    try:
        cfg_all = tomllib.loads((REPO / "docs" / "progress.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"ok": False, "error": f"config unreadable: {exc}"}
    known = {_pr.plan_key(p): p for p in _pr.known_plans(cfg_all)}
    plan = known.get(_pr.plan_key(plan))
    if not plan:
        return {"ok": False, "error": "not one of this project's plans - add a new one in Setup"}
    if not (REPO / plan).is_file():
        return {"ok": False, "error": f"{plan} no longer exists in this checkout"}
    return setup_project({"fields": {"plan": plan}, "apply": True})


def setup_project(body: dict) -> dict:
    """Preview or write the shared config. Writing reloads context providers but
    NOT the run-command allowlist: that one was approved at startup and a new
    command must go through the trust prompt on a restart, not a form post."""
    r = _pr.apply_project_edits(REPO, body.get("fields") or {},
                                body.get("contexts") or [],
                                dry_run=not body.get("apply"),
                                agent=body.get("agent") if isinstance(body.get("agent"), dict) else None)
    if r.get("ok") and r.get("written"):
        # The agent files follow the config: regenerate them now and say what
        # resolved, so a typo in a source is seen here and not at launch.
        try:
            refresh_cfg()
            m = build(REPO)
            ag = _pr.write_agent_files(m, REPO)
            r["agent_files"] = ag
            if m.get("agent"):
                r["agent_resolved"] = {"name": m["agent"]["name"],
                                       "sources": len(m["agent"]["resolved"]),
                                       "missing": m["agent"]["missing"]}
        except Exception as exc:          # noqa: BLE001 - the save itself succeeded
            r["agent_files"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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


# What a client that went away looks like from this side, on every platform.
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class QuietServer(ThreadingHTTPServer):
    """socketserver prints a full traceback for any exception in a handler.

    For a client that hung up mid-response that is routine - this page reloads
    itself by design - and the traceback reads like a crash. Those become one
    line; everything else still prints in full, because a real handler bug
    must not be silenced by the same net.
    """
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            print(f"  client {client_address[0]}:{client_address[1]} went away "
                  f"mid-response ({type(exc).__name__}) - fine", file=sys.stderr)
            return
        super().handle_error(request, client_address)


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
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE:
            # The browser dropped the socket - a reload, a closed tab, a
            # navigation - while this response was in flight. Nothing to
            # deliver and nobody to deliver it to; not an error of ours.
            pass

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._loopback_host():
            self._json({"error": "loopback only"}, 421)
            return
        refresh_cfg()           # an edit from a session or a git pull, not ours
        path = urlparse(self.path).path

        if path == "/api/fresh":
            self._json({"v": fresh_stamp(), "pv": proposals_stamp()})
            return

        if path.startswith("/standup/"):
            # docs/standups/<date>.html as a page, or as a download with ?download=1.
            q = urlparse(self.path).query
            f = standup_file(path.rsplit("/", 1)[-1])
            if f is None:
                self._json({"error": "no such standup - run Standup first"}, 404)
                return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if "download=1" in q:
                self.send_header("Content-Disposition", f'attachment; filename="standup-{f.stem}.html"')
            self.end_headers()
            try:
                self.wfile.write(data)
            except CLIENT_GONE:
                pass
            return

        if path == "/":
            if not (REPO / "docs" / "progress.toml").exists():
                self._redirect("/setup")     # nothing to render yet; go configure
                return
            try:
                model = build(REPO)
                _pr.write_briefs(model, REPO)   # so /next-item and a pinned launch never read a stale brief
                try:
                    r_ag = _pr.write_agent_files(model, REPO)
                    for s in r_ag.get("skipped", []):
                        print("  agent files: " + s, file=sys.stderr)
                except Exception as exc:          # noqa: BLE001 - never block a render
                    print(f"  agent files: not written ({type(exc).__name__}: {exc})", file=sys.stderr)
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

        if path == "/api/whoami":
            # Other dashboards on this machine ask which project this port serves.
            self._json({"repo": str(REPO), "pid": os.getpid(), "port": SERVE_PORT,
                        "name": (CFG.get("project") or {}).get("name", "")})
            return

        if path == "/api/today":
            try:
                self._json(today_view())
            except Exception as exc:  # noqa: BLE001 - the bar must degrade, not 500 the page
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if path == "/today":
            body = today_page(Handler.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except CLIENT_GONE:
                pass
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

        if path == "/api/sessions":
            self._json(sessions_view())
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
        refresh_cfg()
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
            # Match by name OR URL: the committed entry may carry its own
            # name (project-context) while the scan invents one from the
            # label - name-only matching showed an already-adopted service
            # as adoptable, which is an invitation to configure it twice.
            have = {c.get("name") for c in CFG.get("context", [])}
            have_urls = {str(c.get("url", "")).rstrip("/")
                         for c in CFG.get("context", []) if c.get("url")}
            by_url = {str(c.get("url", "")).rstrip("/"): c.get("name")
                      for c in CFG.get("context", []) if c.get("url")}
            rows = _pr.scan_services(host)
            for r in rows:
                r["configured"] = (r["name"] in have
                                   or r["url"].rstrip("/") in have_urls)
                if r["configured"]:
                    r["configured_as"] = (r["name"] if r["name"] in have
                                          else by_url.get(r["url"].rstrip("/"), r["name"]))
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

        if path == "/api/phase/unlink-jira":
            self._json(unlink_ticket(str(body.get("phase", ""))))
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

        if path == "/api/replan-prompt":
            self._json(replan_prompt(str(body.get("scope", "phase")),
                                     str(body.get("phase", "")),
                                     str(body.get("item", "")),
                                     str(body.get("comment", "")),
                                     list(body.get("providers") or [])))
            return

        if path == "/api/plan/switch":
            self._json(switch_plan(str(body.get("plan", ""))))
            return

        if path == "/api/standup/attach":
            self._json(attach_standup(str(body.get("name", "latest.html"))))
            return

        if path == "/api/phases/sync":
            self._json(sync_phases_now())
            return

        if path == "/api/session":
            self._json(open_session(str(body.get("phase", "x")), str(body.get("prompt", "")),
                                    str(body.get("tool", "claude")),
                                    blank=bool(body.get("blank")),
                                    item=str(body.get("item", "")),
                                    prompt_warm=str(body.get("prompt_warm", ""))))
            return

        if path == "/api/proposals":
            self._json(proposals_view())
            return

        if path == "/api/proposals/apply":
            self._json(apply_proposal(str(body.get("id", "")), str(body.get("digest", ""))))
            return

        if path == "/api/proposals/undo":
            self._json(undo_proposal(str(body.get("id", ""))))
            return

        if path == "/api/proposals/mark":
            self._json(mark_proposals(list(body.get("ids") or []), str(body.get("status", ""))))
            return

        if path == "/api/today/tick":
            self._json(today_tick(body))
            return

        if path == "/api/projects/start":
            self._json(start_dashboard(str(body.get("path", ""))))
            return

        if path == "/api/session/forget":
            self._json(forget_session(str(body.get("phase", "")), str(body.get("base", ""))))
            return

        if path == "/api/session/attach":
            self._json(attach_session(str(body.get("phase", "")), str(body.get("base", "")),
                                      str(body.get("id", ""))))
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
    QuietServer.allow_reuse_address = False
    try:
        srv = QuietServer((a.host, a.port), Handler)
    except OSError as exc:
        print(f"cannot bind {a.host}:{a.port} — {exc}", file=sys.stderr)
        print("  another dashboard is probably already running there. Stop it, or "
              "pass --port.", file=sys.stderr)
        return 1
    global SERVE_PORT
    SERVE_PORT = a.port
    # Recorded so the other dashboards' project bars can link here; cleared on
    # the way out (and a stale record is caught by asking the port).
    _pr.mark_served(REPO, a.port, os.getpid())
    import atexit
    atexit.register(lambda: _pr.clear_served(REPO, os.getpid()))
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

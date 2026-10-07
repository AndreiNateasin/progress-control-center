"""Optional phases: their own progress, out of everything that answers
"when are we done".

Run from the repo root:  python -m unittest discover -s tests -v
Stdlib only, like the scripts it tests. Every fixture is a temp repo; no test
reads or writes this repo's own plan.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

_GEN = Path(__file__).resolve().parents[1] / "progress-report.py"
_spec = importlib.util.spec_from_file_location("progress_report_under_test", _GEN)
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def make_repo(plan: str, phases: str, extra: str = "") -> Path:
    """A minimal project: docs/progress.toml + docs/PLAN.md, no git."""
    root = Path(tempfile.mkdtemp(prefix="pcc-optional-"))
    (root / "docs").mkdir()
    (root / "docs" / "progress.toml").write_text(
        '[project]\nname = "t"\nplan = "docs/PLAN.md"\nstart_date = "2026-10-01"\n'
        + extra + "\n" + phases, encoding="utf-8")
    (root / "docs" / "PLAN.md").write_text(plan, encoding="utf-8")
    return root


def phase_block(pid: str, name: str, days: int, deps: list[str], extra: str = "") -> str:
    return (f'[[phase]]\nid = "{pid}"\nname = "{name}"\ndays = {days}\n'
            f'depends_on = {json.dumps(deps)}\n{extra}\n')


# Base: Phase 1 (2 days, 2/2 done -> 100%), Phase 2 (2 days, 1/2 -> 50%).
# Optional: Phase 3 (4 days, 1/4 -> 25%), Phase 4 (1 day, 0/1 -> 0%).
# Base overall = (100*2 + 50*2) / 4 = 75. Optional = (25*4 + 0*1) / 5 = 20.
PLAN = """# Plan

### Phase 1: One
- [x] a
- [x] b

### Phase 2: Two
- [x] c
- [ ] d

### Phase 3{q3}: Three
- [x] e
- [ ] f
- [ ] g
- [ ] h

### Phase 4{q4}: Four
- [ ] i
"""


def plan(q3: str = "", q4: str = "") -> str:
    return PLAN.format(q3=q3, q4=q4)


def phases(opt3: str = "", opt4: str = "", deps4: list[str] | None = None) -> str:
    return (phase_block("1", "One", 2, [])
            + phase_block("2", "Two", 2, ["1"])
            + phase_block("3", "Three", 4, ["2"], opt3)
            + phase_block("4", "Four", 1, deps4 if deps4 is not None else ["3"], opt4))


class OptionalPhases(unittest.TestCase):
    def tearDown(self):
        for r in getattr(self, "_repos", []):
            shutil.rmtree(r, ignore_errors=True)

    def build(self, plan_text: str, phase_text: str, extra: str = "") -> dict:
        r = make_repo(plan_text, phase_text, extra)
        self._repos = getattr(self, "_repos", []) + [r]
        self.repo = r
        return pr.build(r)

    # -- no key: the compatibility contract --------------------------------
    def test_no_key_renders_as_before(self):
        d = self.build(plan(), phases())
        self.assertEqual(d["optional_phases"], [])
        self.assertIsNone(d["optional_overall"])
        self.assertEqual(d["base_phases"], ["1", "2", "3", "4"])
        # every phase counted: (100*2 + 50*2 + 25*4 + 0*1) / 9 = 44
        self.assertEqual(d["overall"], 44)
        self.assertEqual(d["critical_path"], ["1", "2", "3", "4"])
        html = pr.render(d)
        for marker in ("Optional / future", "optgroup", "pill opt", " opt\"", "gopt",
                       "optional_overall", ".optbar"):
            self.assertNotIn(marker, html, marker)
        self.assertNotIn("optional", "".join(p["brief"] for p in d["phases"]))
        # the model gains exactly three keys, and no phase gains a flag
        self.assertTrue(all("optional" not in p for p in d["phases"]))
        self.assertNotIn("optional_ready", d)

    # -- the toml key ------------------------------------------------------
    def test_toml_key_marks_a_phase_optional(self):
        d = self.build(plan(), phases(opt3="optional = true", opt4="optional = true"))
        self.assertEqual(d["optional_phases"], ["3", "4"])
        self.assertEqual(d["base_phases"], ["1", "2"])
        self.assertTrue(all(p["optional"] for p in d["phases"] if p["id"] in ("3", "4")))

    # -- the heading qualifier ---------------------------------------------
    def test_heading_qualifier_optional_and_future(self):
        d = self.build(plan(q3=" (optional)", q4=" (future)"), phases())
        self.assertEqual(d["optional_phases"], ["3", "4"])

    def test_other_qualifiers_are_not_optional(self):
        d = self.build(plan(q3=" (follow-up)"), phases())
        self.assertEqual(d["optional_phases"], [])

    # -- the toml wins over the qualifier, both ways -------------------------
    def test_toml_overrides_the_qualifier(self):
        d = self.build(plan(q3=" (optional)"), phases(opt3="optional = false", opt4="optional = true"))
        self.assertEqual(d["optional_phases"], ["4"])

    # -- both percentages ----------------------------------------------------
    def test_both_percentages(self):
        d = self.build(plan(), phases(opt3="optional = true", opt4="optional = true"))
        self.assertEqual(d["overall"], 75)
        self.assertEqual(d["optional_overall"], 20)
        self.assertEqual((d["done_phases"], d["total_phases"]), (1, 2))

    # -- the critical path and the finish ------------------------------------
    def test_optional_phase_excluded_from_critical_path(self):
        # Phase 3 is the longest and the last to end - the terminal of the
        # critical path - until it is optional.
        base = self.build(plan(), phases(deps4=["2"]))
        self.assertIn("3", base["critical_path"])
        d = self.build(plan(), phases(opt3="optional = true", deps4=["2"]))
        self.assertNotIn("3", d["critical_path"])
        self.assertEqual(d["critical_path"], ["1", "2", "4"])
        self.assertLess(d["finish_date"], base["finish_date"])
        self.assertLess(d["remaining_days"], base["remaining_days"])
        self.assertFalse(next(p for p in d["phases"] if p["id"] == "3")["critical"])

    def test_optional_out_of_ready_but_startable(self):
        # Phase 3 (optional) depends only on Phase 1, which is done.
        text = (phase_block("1", "One", 2, []) + phase_block("2", "Two", 2, ["1"])
                + phase_block("3", "Three", 4, ["1"], "optional = true")
                + phase_block("4", "Four", 1, ["3"], "optional = true"))
        d = self.build(plan(), text)
        self.assertEqual([r["phase"]["id"] for r in d["ready"]], ["2"])
        self.assertTrue(next(p for p in d["phases"] if p["id"] == "3")["startable"])
        self.assertEqual(d["current"]["id"], "2")
        html = pr.render(d)
        self.assertIn('data-filt="ready">Ready<span class="cnt">2</span>', html)

    def test_risks_and_pace(self):
        text = (phase_block("1", "One", 2, []) + phase_block("2", "Two", 2, ["1"])
                + phase_block("3", "Three", 4, ["1"], 'optional = true\nexternal_blockers = ["hw"]')
                + phase_block("4", "Four", 1, ["3"], "optional = true"))
        extra = '[[blocker]]\nid = "hw"\nname = "Hardware"\nlead_days = 400\nstatus = "open"\n'
        d = self.build(plan(), text, extra=extra)
        r = next(x for x in d["risks"] if "Hardware" in x["risk"])
        self.assertEqual(r["severity"], "info")
        self.assertIn("optional", r["risk"])
        # only the base plan's open items are left: Phase 2's one
        self.assertEqual(d["pace"]["items_left"], 1)

    def test_base_done_with_optional_open(self):
        # the base plan finished, optional work open: no "every item is ticked"
        done_plan = plan().replace("- [ ] d", "- [x] d")
        d = self.build(done_plan, phases(opt3="optional = true", opt4="optional = true"))
        self.assertEqual(d["overall"], 100)
        html = pr.render(d)
        self.assertNotIn("every item is ticked", html)
        self.assertIn("base plan complete · 4 optional items open", html)

    # -- rendering ------------------------------------------------------------
    def test_render_groups_base_then_optional(self):
        d = self.build(plan(), phases(opt3="optional = true", opt4="optional = true"))
        html = pr.render(d)
        self.assertIn("Optional / future", html)
        self.assertLess(html.index('id="phase-2"'), html.index('class="optgroup"'))
        self.assertLess(html.index('class="optgroup"'), html.index('id="phase-3"'))
        self.assertIn('<span class="optpct num">20%</span>', html)
        self.assertIn("optional 20%", html)              # the Overall tile's note
        gantt = html[html.index('class="gantt"'):]
        self.assertLess(gantt.index('href="#phase-2"'), gantt.index('href="#phase-3"'))
        self.assertIn('class="grow gopt" href="#phase-3"', gantt)

    def test_agent_projection_lists_optional_phases(self):
        extra = ('[plans."docs/PLAN.md".agent]\nname = "planner"\ndescription = "d"\n'
                 '[plans."docs/PLAN.md".agent.sources]\nfiles = []\n')
        d = self.build(plan(), phases(opt3="optional = true"), extra=extra)
        body = pr.agent_body(d)
        self.assertIn("## Optional phases", body)
        self.assertIn("  - Phase 3 - Three: 3 of 4 open", body.split("## Optional phases")[1])
        self.assertNotIn("Phase 3 - Three", body.split("## Optional phases")[0])

    def test_snapshot_records_both_figures(self):
        d = self.build(plan(), phases(opt3="optional = true"))
        pr.set_repo(self.repo)
        snap = json.loads(pr.snapshot(d).read_text(encoding="utf-8"))
        self.assertEqual((snap["overall"], snap["optional_overall"]), (d["overall"], d["optional_overall"]))
        d0 = self.build(plan(), phases())
        pr.set_repo(self.repo)
        self.assertNotIn("optional_overall", json.loads(pr.snapshot(d0).read_text(encoding="utf-8")))

    # -- --check ------------------------------------------------------------
    def check(self, plan_text: str, phase_text: str) -> tuple[int, str]:
        r = make_repo(plan_text, phase_text)
        self._repos = getattr(self, "_repos", []) + [r]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = pr.check_config(r)
        return rc, out.getvalue()

    def test_check_rejects_a_non_boolean(self):
        rc, out = self.check(plan(), phases(opt3='optional = "yes"'))
        self.assertEqual(rc, 1)
        self.assertIn("optional must be true or false", out)

    def test_check_warns_when_base_depends_on_optional(self):
        rc, out = self.check(plan(q3=" (optional)"), phases(deps4=["3"]))
        self.assertEqual(rc, 0)
        self.assertIn("depends_on optional phase '3'", out)

    # -- review: what the first suite let through ---------------------------
    def test_no_key_says_nothing_optional_anywhere(self):
        extra = ('[plans."docs/PLAN.md".agent]\nname = "planner"\ndescription = "d"\n'
                 '[plans."docs/PLAN.md".agent.sources]\nfiles = []\n')
        d = self.build(plan(), phases(), extra=extra)
        pr.set_repo(self.repo)
        for name, text in (("page", pr.render(d)), ("standup", pr.standup(d, 1)),
                           ("standup page", pr.standup_html(d, 1)), ("agent", pr.agent_body(d)),
                           ("prompts", "".join(p["prompt"] + p["item_prompt_tmpl"] for p in d["phases"]))):
            self.assertNotIn("optional", text.lower(), name)

    def test_current_is_a_base_phase(self):
        text = (phase_block("1", "One", 2, [], "optional = true") + phase_block("2", "Two", 2, []))
        p = "### Phase 1: One\n- [x] a\n- [ ] b\n\n### Phase 2: Two\n- [x] c\n- [ ] d\n"
        d = self.build(p, text)
        self.assertEqual(d["current"]["id"], "2")
        d = self.build("### Phase 1: One\n- [x] a\n- [ ] b\n\n### Phase 2: Two\n- [ ] c\n", text)
        self.assertIsNone(d["current"])            # only the optional phase is active

    def test_done_count_ignores_a_finished_optional_phase(self):
        d = self.build(plan().replace("- [ ] i", "- [x] i"), phases(opt4="optional = true"))
        self.assertEqual((d["done_phases"], d["total_phases"]), (1, 3))

    def test_base_after_optional_keeps_its_ancestors_on_the_path(self):
        # 1 -> 2 -> optional 3 -> 4: the walk back steps through 3 to 2 and 1
        d = self.build(plan(), phases(opt3="optional = true", deps4=["3"]))
        self.assertEqual(d["critical_path"], ["1", "2", "4"])
        self.assertGreater(d["remaining_days"], 0)

    def test_blocker_wait_and_stall_leave_optional_out(self):
        text = (phase_block("1", "One", 2, []) + phase_block("2", "Two", 2, ["1"])
                + phase_block("3", "Three", 4, ["1"], '{opt}external_blockers = ["hw"]')
                + phase_block("4", "Four", 1, ["3"], "{opt}"))
        extra = '[[blocker]]\nid = "hw"\nname = "Hardware"\nlead_days = 400\nstatus = "open"\n'
        stall = plan().replace("- [ ] f\n- [ ] g\n- [ ] h", "- [x] f\n- [x] g\n- [~] h")
        base = self.build(stall, text.replace("{opt}", ""), extra=extra)
        opt = self.build(stall, text.replace("{opt}", "optional = true\n"), extra=extra)
        self.assertEqual(base["pace"]["waits"], 400)
        self.assertEqual(opt["pace"]["waits"], 0)
        self.assertTrue(any("done but not closed" in r["risk"] for r in base["risks"]))
        self.assertFalse(any("done but not closed" in r["risk"] for r in opt["risks"]))

    def test_parallel_saving_and_waves_are_base_only(self):
        # optional 3 starts with base 2 (both after 1): not in 2's wave or group
        text = (phase_block("1", "One", 2, []) + phase_block("2", "Two", 2, ["1"])
                + phase_block("3", "Three", 4, ["1"], "optional = true")
                + phase_block("4", "Four", 1, ["2"]))
        d = self.build(plan(), text)
        ph = {p["id"]: p for p in d["phases"]}
        self.assertNotEqual(ph["3"]["level"], ph["2"]["level"])
        self.assertIsNone(ph["2"]["group"])
        self.assertEqual(d["sequential_days"], 5)       # 2 + 2 + 1, Phase 3 left out
        self.assertEqual(d["saved_days"], 0)

    def test_briefs_and_bare_next_mark_optional_phases(self):
        import subprocess
        import sys
        d = self.build(plan(), phases(opt3="optional = true"))
        ph = {p["id"]: p for p in d["phases"]}
        self.assertIn("Optional phase:", ph["3"]["brief"])
        self.assertIn("Optional phase:", ph["3"]["prompt"])
        self.assertNotIn("Optional phase:", ph["2"]["brief"])
        r = subprocess.run([sys.executable, str(_GEN), "--repo", str(self.repo), "--next"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertIn("1, 2, 3 (optional), 4.", r.stdout)

    def test_continuous_optional_phase_has_no_figure(self):
        d = self.build(plan(), phases(opt3="optional = true\ncontinuous = true"))
        self.assertEqual(d["optional_phases"], ["3"])
        self.assertIsNone(d["optional_overall"])
        html = pr.render(d)
        self.assertNotIn("None%", html)
        self.assertIn('<span class="optpct num">\u2014</span>', html)
        pr.set_repo(self.repo)
        self.assertNotIn("optional_overall", json.loads(pr.snapshot(d).read_text(encoding="utf-8")))

    def test_check_warns_on_an_optional_placeholder(self):
        empty = plan(q3=" (future)").replace("- [x] e\n- [ ] f\n- [ ] g\n- [ ] h\n", "Ideas for later.\n")
        rc, out = self.check(empty, phases())
        self.assertEqual(rc, 0)
        self.assertIn("an optional placeholder", out)
        rc, _ = self.check(empty.replace(" (future)", ""), phases())
        self.assertEqual(rc, 1)

    def test_check_survives_a_scalar_depends_on(self):
        text = phases() + '[[phase]]\nname = "Orphan"\ndays = 1\ndepends_on = 1\n'
        rc, out = self.check(plan(q3=" (optional)"), text)
        self.assertEqual(rc, 1)
        self.assertIn("has no id", out)

    def test_sync_honours_a_declared_optional_key(self):
        cfg = ('[project]\nname = "t"\nplan = "docs/PLAN.md"\nstart_date = "2026-10-01"\n\n'
               + phase_block("1", "One", 1, []) + phase_block("2", "Two", 1, ["1"], "{k}"))
        three = "### Phase 1: One\n- [ ] a\n\n### Phase 2{q}: Two\n- [ ] b\n\n### Phase 3: Three\n- [ ] c\n"

        def stub_dep(cfg_text, plan_text):
            new, _ = pr.sync_phases_with_plan(cfg_text, plan_text, "docs/PLAN.md", "docs/PLAN.md", "2026-10-07")
            m = re.search(r'id\s*=\s*"3"[\s\S]*?depends_on\s*=\s*\[([^\]]*)\]', new)
            return m.group(1).strip()
        self.assertEqual(stub_dep(cfg.format(k="optional = true"), three.format(q="")), '"1"')
        self.assertEqual(stub_dep(cfg.format(k="optional = false"), three.format(q=" (optional)")), '"2"')
        self.assertEqual(stub_dep(cfg.format(k=""), three.format(q=" (optional)")), '"1"')


if __name__ == "__main__":
    unittest.main()

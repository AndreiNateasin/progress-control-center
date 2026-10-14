"""The projects list: taking a project off it sticks.

Run from the repo root:  python -m unittest discover -s scripts/tests -v
Stdlib only. APPDATA (and the home folder, for the server) point at a temp
dir, so the real projects list is never read or written.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("progress_report_registry_test", _SCRIPTS / "progress-report.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcc-registry-"))
        self.env = mock.patch.dict(os.environ, {"APPDATA": str(self.tmp / "appdata"),
                                                "USERPROFILE": str(self.tmp / "home"),
                                                "HOME": str(self.tmp / "home")})
        self.env.start()
        (self.tmp / "home").mkdir()
        self.old = self.tmp / "old_checkout"
        self.new = self.tmp / "new_checkout"
        for d in (self.old, self.new):
            d.mkdir()
        pr.remember_project(self.old, "old one")
        pr.remember_project(self.new, "new one")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def listed(self):
        return [Path(e["path"]).name for e in pr.load_projects()]

    def test_registry_is_the_temp_one(self):
        self.assertTrue(str(pr.projects_path()).startswith(str(self.tmp)))
        self.assertEqual(sorted(self.listed()), ["new_checkout", "old_checkout"])

    def test_forget_retires(self):
        pr.forget_project(str(self.old), instead=str(self.new))
        self.assertEqual(self.listed(), ["new_checkout"])
        gone = pr.retired_entry(self.old)
        self.assertIsNotNone(gone)
        self.assertEqual(Path(gone["instead"]), self.new.resolve())
        self.assertTrue(gone["since"])
        self.assertIsNone(pr.retired_entry(self.new))

    def test_opening_it_again_does_not_bring_it_back(self):
        pr.forget_project(str(self.old))
        pr.remember_project(self.old, "old one")
        self.assertEqual(self.listed(), ["new_checkout"])

    def test_an_older_writer_cannot_bring_it_back(self):
        # a dashboard started before retired.toml existed rewrites the whole
        # list from what it read at its start - the old entry included
        before = pr.load_projects()
        pr.forget_project(str(self.old))
        pr.save_projects(before)
        self.assertEqual(self.listed(), ["new_checkout"])

    def test_restore(self):
        pr.forget_project(str(self.old))
        self.assertTrue(pr.restore_project(self.old))
        self.assertFalse(pr.restore_project(self.old))
        pr.remember_project(self.old, "old one")
        self.assertIn("old_checkout", self.listed())

    def test_forget_twice_keeps_one_record(self):
        pr.forget_project(str(self.old))
        pr.forget_project(str(self.old), instead=str(self.new))
        recs = [e for e in pr.load_retired() if Path(e["path"]) == self.old.resolve()]
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["instead"])

    # -- the server ----------------------------------------------------------
    def serve(self, *extra, wait=True):
        port = free_port()
        p = subprocess.Popen([sys.executable, str(_SCRIPTS / "progress-serve.py"), "--repo", str(self.old),
                              "--port", str(port), "--no-open", "--trust-yes", *extra],
                             env=dict(os.environ), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return p, port

    def test_server_refuses_a_retired_folder(self):
        pr.forget_project(str(self.old), instead=str(self.new))
        p, _ = self.serve()
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 2, err)
        self.assertIn("taken off your project list", err)
        self.assertIn(f"--repo {self.new.resolve()}", err)
        self.assertIn("--restore", err)
        self.assertEqual(self.listed(), ["new_checkout"])

    def test_server_restore_puts_it_back(self):
        pr.forget_project(str(self.old))
        p, port = self.serve("--restore")
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            who = {}
            for _ in range(80):
                try:
                    with op.open(f"http://127.0.0.1:{port}/api/whoami", timeout=2) as r:
                        who = json.loads(r.read().decode("utf-8"))
                        break
                except Exception:
                    if p.poll() is not None:
                        break
                    time.sleep(0.25)
            self.assertTrue(who.get("repo"), p.stderr.read() if p.poll() is not None else "no answer")
            self.assertIsNone(pr.retired_entry(self.old))
            self.assertIn("old_checkout", self.listed())
        finally:
            p.terminate()
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    unittest.main()

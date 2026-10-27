#!/usr/bin/env python3
"""
selftest_packaging.py -- Verify the distribution layout actually imports.

The modules use flat top-level imports (`from btengine import ...`) so they
run as plain scripts from a checkout. But when the project is INSTALLED, the
same modules live inside the `btlab` package, where a flat import does not
resolve. Each module therefore needs a dual import: flat first, relative as
the fallback.

This test builds a throwaway copy of scripts/ shaped as a `btlab/` package
and imports every module from it. If a module loses its relative-import
fallback, this fails -- which is exactly what a user extending the library
would hit on `import btlab.btvalidate`.

Offline, stdlib only, no installation required.

Run:  python scripts/selftest_packaging.py
Exit: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

MODULES = ["run_backtest", "btengine", "btvalidate", "intraday", "intake",
           "disclosure"]


class TestPackagedLayout(unittest.TestCase):
    """Every module must import from the installed `btlab` package layout."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="btpkg_")
        pkg = os.path.join(cls.tmp, "btlab")
        os.makedirs(pkg, exist_ok=True)
        for name in os.listdir(HERE):
            if name.endswith(".py"):
                shutil.copy(os.path.join(HERE, name), os.path.join(pkg, name))
        cls.tmp = cls.tmp

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _import_in_package(self, module):
        code = (
            "import sys; sys.path.insert(0, %r); "
            "import %s; print('OK')" % (self.tmp, module)
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def test_1_every_module_imports_as_package(self):
        """`import btlab.<module>` must work for every module."""
        for m in MODULES:
            ok, out = self._import_in_package(f"btlab.{m}")
            self.assertTrue(ok, f"btlab.{m} failed to import as a package:\n{out}")

    def test_2_entry_point_resolves(self):
        """The console-script target must be importable and callable."""
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from btlab.run_backtest import main; "
            "assert callable(main); print('OK')" % self.tmp
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         f"entry point btlab.run_backtest:main not callable:\n"
                         f"{r.stdout}{r.stderr}")

    def test_3_modules_still_run_as_flat_scripts(self):
        """The flat import path must keep working for a plain checkout."""
        for m in ("btengine", "btvalidate", "intraday", "intake", "disclosure"):
            code = (
                "import sys; sys.path.insert(0, %r); "
                "import %s; print('OK')" % (HERE, m)
            )
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0,
                             f"flat import of {m} broke:\n{r.stdout}{r.stderr}")

    def test_4_every_module_declares_dual_imports(self):
        """Each module that imports a sibling must have the fallback."""
        siblings = {
            "btvalidate.py": "btengine",
            "intraday.py": "btengine",
        }
        for fname, sibling in siblings.items():
            path = os.path.join(HERE, fname)
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            self.assertIn(f"from {sibling} import", src,
                          f"{fname} must import {sibling}")
            self.assertIn("except ImportError", src,
                          f"{fname} must have a relative-import fallback")
            self.assertIn(f"from .{sibling} import", src,
                          f"{fname} must fall back to a relative import of "
                          f"{sibling} for the installed package layout")


if __name__ == "__main__":
    unittest.main(verbosity=2)

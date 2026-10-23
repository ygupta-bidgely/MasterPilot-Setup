"""No environment/pilot literal in any executable string of the provisioner (docstrings may keep history)."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RX = re.compile(r"productqa|masterpilot|8800[0-9]|10130|fortplains|htmltoimage|cloudfront\.net")


class NoTargetLiterals(unittest.TestCase):
    def test_executable_strings(self):
        hits = []
        for f in sorted(ROOT.glob("*.py")):
            tree = ast.parse(f.read_text())
            docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef,
                    ast.AsyncFunctionDef)) and ast.get_docstring(n, clean=False)}
            hits += [f"{f.name}:{n.lineno}" for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and RX.search(n.value)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()

"""Audit: no environment / deployment / pilot / endpoint / AWS / bucket / queue / schema / rate-plan literal in the
EXECUTABLE code of the modules the debug kit calls (docstrings are history and are excluded; comments are not code)."""
import ast
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
# the engines + every script the debug kit's catalog launches (catalog provisioner_phases); kept explicit here and
# cross-checked against the installed kit catalog when it is available
SCRIPTS = ["create_mds_single_fuel_user.py", "create_mds_dual_fuel_user.py", "create_mds_tou_coaching_user.py",
           "create_mds_tou_onboarding_user.py", "create_mds_budget_alert_user.py", "create_mds_budget_billing_user.py",
           "create_mds_single_fuel_her_user.py", "create_mds_dual_fuel_her_user.py"]
CALLED = ["mds_common.py", "mds_target.py", "mds_tou.py", "mds_ba.py", "mds_bb.py", "mds_her.py", "mds_paper_her.py"] + SCRIPTS
BAD_STR = re.compile(r"productqa|masterpilot|nonprodqa|8800\d|10130|fortplains|us-(?:east|west)-\d|bidgely-(?!email-htmltoimage|debug)[a-z]+-[a-z]"
                     r"|resi-az|hybrid-models-|MDS_SOLAR_TOU|MDS_TOU_EVRR|America/|bidgely_masterpilot|bidgelydbqa|api-server-"
                     r"|\.bidgely\.com|bidgelyqa\+|jumphost-|login-bops|list-hosts", re.I)
BAD_INT = {88001, 88002, 88009, 10130, 905, 906, 907}


def _docstring_nodes(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def audit(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    docs, hits = _docstring_nodes(tree), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docs:
            v = node.value
            if isinstance(v, str) and BAD_STR.search(v):
                hits.append(f"{path.name}:{node.lineno}: {v[:90]!r}")
            elif isinstance(v, int) and not isinstance(v, bool) and v in BAD_INT:
                hits.append(f"{path.name}:{node.lineno}: {v}")
    return hits


class NoTargetLiterals(unittest.TestCase):
    def test_script_list_matches_the_kit_catalog(self):
        import json
        cat = Path.home() / ".claude/skills/config-fail-testcase-debugger/catalog/catalog.json"
        if not cat.is_file():
            self.skipTest("debug kit not installed")
        declared = {k for k in json.loads(cat.read_text()).get("provisioner_phases", {}) if not k.startswith("_")}
        self.assertEqual(declared, set(SCRIPTS))

    def test_called_modules_carry_no_target(self):
        hits = [h for f in CALLED for h in audit(HERE / f)]
        self.assertEqual(hits, [], "\n".join(hits))


if __name__ == "__main__":
    unittest.main()

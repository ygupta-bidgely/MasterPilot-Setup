"""Moved: the evidence classifier lives in the debug kit (one maintained implementation):
    <config-fail-testcase-debugger skill>/scripts/emailkit/chain.py
This module re-exports it; the kit is found exactly like mds_target finds it (BIDGELY_KIT_SCRIPTS or the installed skill)."""
from __future__ import annotations

import sys
from pathlib import Path

import mds_target

_P = mds_target.kit_scripts()
if not (Path(_P) / "emailkit" / "chain.py").is_file():
    raise ImportError(f"tou_evidence moved to the debug kit (emailkit/chain.py): no kit at {_P} (install debug-kit >= 1.2.0)")
if _P not in sys.path:
    sys.path.insert(0, _P)

from emailkit.chain import *  # noqa: E402,F401,F403
from emailkit.chain import (SERVICES, Attempt, Chain, classify, fuel_status, fuels_in, in_window, report,  # noqa: E402,F401
                            thread_of, ts_of)

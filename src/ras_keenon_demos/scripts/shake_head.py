#!/usr/bin/env python3
"""Standalone shake_head demonstration client."""
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
if (root/'ras_keenon_demos').is_dir():sys.path.insert(0,str(root))
from ras_keenon_demos.cli import run_fixed
if __name__=='__main__':raise SystemExit(run_fixed('shake_head'))

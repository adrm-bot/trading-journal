#!/usr/bin/env python3
"""run_all.py — 적재 + 다이제스트를 한 번에. 작업 스케줄러가 이걸(run.bat 경유) 호출."""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

print("=== 1) 거래 적재 ===")
subprocess.run([PY, os.path.join(HERE, "pull_trades.py")])
print("\n=== 2) 다이제스트 ===")
subprocess.run([PY, os.path.join(HERE, "notify.py")])

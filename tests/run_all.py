"""Run every test here. `python3 tests/run_all.py` — no network, no AI calls, no real sends."""
import pathlib
import subprocess
import sys

here = pathlib.Path(__file__).parent
files = sorted(p for p in here.glob("test_*.py"))
results = []
for f in files:
    print(f"\n{'=' * 62}\n  {f.name}\n{'=' * 62}")
    r = subprocess.run([sys.executable, str(f)], cwd=here)
    results.append((f.name, r.returncode == 0))
print(f"\n{'=' * 62}")
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
bad = [n for n, ok in results if not ok]
print(f"{'=' * 62}\n{len(results) - len(bad)}/{len(results)} suites passed")
sys.exit(1 if bad else 0)

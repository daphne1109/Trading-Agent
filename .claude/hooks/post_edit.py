"""Claude Code PostToolUse hook: lint the edited Python file and run the fast unit tests.

Exit code 2 sends the failure output back to Claude so it fixes the problem before moving on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")


def tool(name: str) -> str:
    exe = BIN / (f"{name}.exe" if sys.platform == "win32" else name)
    return str(exe) if exe.exists() else name


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = (event.get("tool_input") or {}).get("file_path", "")
    if not path.endswith(".py"):
        return 0

    problems: list[str] = []
    lint = subprocess.run([tool("ruff"), "check", path], capture_output=True, text=True)
    if lint.returncode != 0:
        problems.append(f"ruff check {path}:\n{lint.stdout}{lint.stderr}")

    tests = subprocess.run(
        [tool("python"), "-m", "pytest", "-q", "-x", "tests/unit"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if tests.returncode not in (0, 5):  # 5 = no tests collected
        problems.append(f"unit tests failed:\n{tests.stdout[-3000:]}")

    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# End-to-end run with real Claude Code + Strands Decider 2B (local) in a throwaway project.
#   scripts/e2e_demo.sh "<task prompt>" [workdir]
# No API key: the model runs on this machine (weights cached after the first download).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TASK="$1"
WORK="${2:-$(mktemp -d)}"
mkdir -p "$WORK/demo" && cd "$WORK/demo"
git init -q
printf 'def add(a, b):\n    return a + b\n' > calc.py
printf 'import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n\nif __name__ == "__main__":\n    unittest.main()\n' > test_calc.py
git add . && git -c user.name=demo -c user.email=demo@example.com commit -qm init
PYTHONPATH="$REPO" "$REPO/.venv/bin/python" -m completion_checker install "$WORK/demo" --mode enforce >/dev/null
export DECIDER_CHECKER_STATE_DIR="$WORK/state"
export HF_HUB_OFFLINE=1
claude -p "$TASK" --model claude-sonnet-5 --max-turns 30 \
  --allowedTools "Read" "Edit" "Write" "Bash(python3 -m unittest*)" "Bash(python3 *)" "Bash(mkdir *)" "Bash(cat *)" "Bash(ls *)" \
  --output-format text
echo "=== checker report ==="
cat "$WORK/demo/.completion"/*/report.md
echo "=== workdir: $WORK"

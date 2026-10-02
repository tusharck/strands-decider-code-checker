"""Entry point Claude Code runs for every configured hook event."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from completion_checker.cli import main  # noqa: E402

main(["hook"])

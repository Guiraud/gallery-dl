#!/usr/bin/env python3
"""Wrapper CLI to build static HTML timelines for gallery-dl Twitter exports."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macos_app.twitter_index import main as _main


def main() -> int:
    # Delegate parsing/logic to the shared module so GUI and CLI stay in sync.
    return _main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())

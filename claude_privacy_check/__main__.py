"""Allows `python3 -m claude_privacy_check`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

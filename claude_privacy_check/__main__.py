"""Allows `python3 -m claude_privacy_check`."""

import sys

from .install import prepare_launch
from .cli import main

if __name__ == "__main__":
    prepare_launch()
    sys.exit(main())

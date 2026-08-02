#!/usr/bin/env python3
"""Claude Privacy Check — entry point.

Detects whether an organisation is capturing Claude Code prompts on this
machine, and lets the local history be deleted selectively.

    python3 run.py                 graphical interface
    python3 run.py --cli           check against the baseline (terminal)
    python3 run.py --help          all options

What this cannot do: a server-side organisation data export by the Primary
Owner runs entirely at Anthropic and leaves no trace on this machine.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from claude_privacy_check.install import prepare_launch  # noqa: E402
from claude_privacy_check.cli import main  # noqa: E402

if __name__ == "__main__":
    # Ensure menu entry / icons / symlink when missing. The GUI shows each
    # install step live; CLI launches apply quietly.
    prepare_launch()
    sys.exit(main())

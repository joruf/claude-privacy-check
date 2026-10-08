"""Claude Privacy Check — detect client-side monitoring of Claude Code."""

__version__ = "1.1.0"
# Counter over every change, never reset. It answers "is this yesterday's
# build?", which the semantic number alone cannot.
__build__ = 3
VERSION_LABEL = f"{__version__} ({__build__})"

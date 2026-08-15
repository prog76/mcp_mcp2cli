#!/usr/bin/env python3
"""Allow ``python -m mcp2cli`` as an alternative to the console script."""

import sys

from mcp2cli.cli import main

if __name__ == "__main__":
    sys.exit(main())

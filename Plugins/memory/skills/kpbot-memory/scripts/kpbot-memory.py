#!/usr/bin/env python3
"""Entry point for the KPBot memory skill.

Usage::

    python3 Plugins/memory/skills/kpbot-memory/scripts/kpbot-memory.py backend \
        initialize --config /path/to/backend.json

    python3 Plugins/memory/skills/kpbot-memory/scripts/kpbot-memory.py backend \
        recall --input payload.json --config /path/to/backend.json

The script only wires the sibling ``kpbot_memory`` package onto ``sys.path``
and delegates to :func:`kpbot_memory.cli.main`; all behaviour lives in the
package.  The backend CLI never prints configuration or credential values.
"""

from __future__ import annotations

import os
import sys


def _bootstrap():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def main(argv=None):
    _bootstrap()
    from kpbot_memory.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())

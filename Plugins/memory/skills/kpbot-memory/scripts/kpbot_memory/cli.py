"""KPBot memory command line.

The plugin's memory entry is the one-shot OpenViking backend client;
``kpbot memory backend ...`` dispatches to it.
"""
from __future__ import annotations

import sys

_USAGE = (
    "usage: kpbot memory backend {initialize,status,remember,recall,close}"
    " --config FILE [--input FILE|-] [--timeout SECONDS]\n"
    "\n"
    "Run `kpbot memory backend --help` for the full option list.\n"
)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "backend":
        from .backend_cli import main as backend_main

        return backend_main(arguments[1:])
    if not arguments or arguments[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    sys.stderr.write(_USAGE)
    return 2

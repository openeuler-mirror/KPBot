"""KPBot memory command line.

Dispatch explicit configuration to ``configure`` and one-shot operations to
``backend`` (OpenViking, mem0 or TencentDB).
"""
from __future__ import annotations

import sys

_USAGE = (
    "usage: kpbot memory configure --backend {openviking,mem0,tencentdb} --config FILE\n"
    "       kpbot memory backend {initialize,status,remember,recall,close}"
    " --config FILE [--input FILE|-] [--timeout SECONDS] [--progress | --no-progress]\n"
    "\n"
    "Progress uses stderr (TTY default); backend results remain JSON on stdout.\n"
    "Run `kpbot memory configure --help` or `kpbot memory backend --help` for options.\n"
)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "configure":
        from .configure import main as configure_main

        return configure_main(arguments[1:])
    if arguments and arguments[0] == "backend":
        from .backend import main as backend_main

        return backend_main(arguments[1:])
    if not arguments or arguments[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    sys.stderr.write(_USAGE)
    return 2

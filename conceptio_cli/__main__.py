"""``python -m conceptio_cli`` — the same entry point as the ``conceptio`` script.

Useful when the console script is not on PATH (a virtualenv that was never
activated, a host process that resolves the interpreter rather than the shim),
and for editing a checkout without installing it.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

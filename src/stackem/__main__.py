"""``python -m stackem`` -- the same entry point as the ``stackem`` script.

SPEC.md sec 11 distributes stackem for ``uvx``, where the console script in
``[project.scripts]`` is what runs.  This exists so the module form works too,
and so neither path can drift from the other: both call :func:`stackem.cli.main`.
"""

from __future__ import annotations

from stackem.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

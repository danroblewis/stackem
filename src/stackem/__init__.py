"""stackem -- keep a chain of stacked branches and their pull requests in sync.

Layout
------
``stackem.gitx``          the only module that runs ``git``
``stackem.model``         plain dataclasses: Branch, PullRequest, Stack, RepoSettings
``stackem.forge``         the forge-neutral Provider protocol
``stackem.forge.mock_server``  an in-process mock GitHub, used by the test suite

stackem stores no state and changes no configuration (CLAUDE.md, "Two constraints").
"""

__version__ = "1.0.0"

__all__ = ["__version__"]

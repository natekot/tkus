"""tkus -- attribute AI agent token usage and cost to individual git commits."""

# The single source of truth for the version. pyproject.toml reads it from here
# via setuptools' dynamic version, and __main__ imports it -- so `tkus --version`
# and the installed package metadata can never disagree.
#
# Bump this on every deployed change. pip compares *packaged* versions, so a
# stale number here makes `pip install --upgrade` a silent no-op on machines
# that already have tkus.
__version__ = "0.6.0"

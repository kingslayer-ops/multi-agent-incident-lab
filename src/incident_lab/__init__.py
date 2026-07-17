"""Multi-Agent Incident Lab application package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("multi-agent-incident-lab")
except PackageNotFoundError:  # Source tree without an installed distribution.
    __version__ = "0+unknown"

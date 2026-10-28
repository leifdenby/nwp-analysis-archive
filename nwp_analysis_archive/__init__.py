"""Archive the analysis time step of NWP forecast zarrs."""

from importlib import metadata

try:
    __version__ = metadata.version("nwp-analysis-archive")
except metadata.PackageNotFoundError:  # not installed, e.g. run from a copy
    __version__ = "unknown"

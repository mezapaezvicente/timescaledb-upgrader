"""Tooling for upgrading and rolling back self-hosted TimescaleDB on Kubernetes.

Standard library only, on purpose: this runs in the same shells and CI jobs as
the rest of the toolkit, and an upgrade tool that first needs its own dependency
install is one more thing to go wrong during a maintenance window.
"""

__version__ = "0.2.0"

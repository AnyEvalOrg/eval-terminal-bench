"""Shared verifier setup-failure signature; companion app must match byte for byte."""
import re

_SETUP_FAILURE = re.compile(r"Temporary failure resolving|Could not resolve host|uvx: (?:command )?not found|curl: (?:command )?not found|No module named pytest", re.I)

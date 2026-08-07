# ADK discovers agents by importing this package by name and expects the
# `agent` module (with `root_agent`) to be registered as a side effect.
from . import agent  # noqa: F401

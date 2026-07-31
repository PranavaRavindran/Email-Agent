import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# agent.py's own imports (agents.*, tools.*, auth) resolve against the project root
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Load the project's agent.py by path under a non-colliding module name.
# ADK registers this wrapper package in sys.modules as "agent", so a plain
# `from agent import root_agent` would resolve back to this package itself.
_spec = importlib.util.spec_from_file_location(
    "_project_root_agent", os.path.join(_ROOT, "agent.py")
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

root_agent = _module.root_agent

__all__ = ["root_agent"]
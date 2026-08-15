"""Reconciler ADK agent package.

The supervisor is the root agent (`root_agent`). It is the only agent exposed
to the ADK API server (`adk api_server agents/reconciler`). Specialists are
wired in as `sub_agents` in later phases.
"""

from .agent import root_agent

__all__ = ["root_agent"]
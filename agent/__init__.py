"""Autonomous maker-agent for the Rock Pi 4C+ (RK3399).

A small, dependency-light agent that runs in a loop, decides its own tasks,
and uses tools (shell, files, web, image-gen, sensors, dashboards) to carry
them out. LLM calls are routed across free cloud providers first and fall
back to a local Ollama model when those are rate-limited or unreachable.
"""

__version__ = "1.0.0"

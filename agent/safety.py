"""Deprecated shim. The real, tamper-resistant policy now lives in
``agent/safeguard.py`` (installed root-owned, read-only). This module simply
re-exports it so older imports keep working. Import ``safeguard`` directly in
new code.
"""

from .safeguard import (  # noqa: F401
    BUILTIN_DENY,
    CommandRejected,
    check_command,
    posix_limits,
    safe_join,
)

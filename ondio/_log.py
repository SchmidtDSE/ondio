"""Lazy, optional logging through cocina's Printer.

cocina's `Printer` is a process-wide singleton: whoever constructs it first
fixes its configuration (log file, silence, ...), and later constructor
arguments are silently ignored. This module therefore never constructs it at
import time — the instance is resolved on each attribute access, so an
application that configures `Printer(...)` before ondio's first log line
wins. If cocina is not installed, every call is a silent no-op: logging
narrates, exceptions are the failure signal, so nothing is lost.

Usage:
    from ondio._log import printer

    printer.message("using byte-range strategy", start_byte=0, end_byte=1024)
"""

from __future__ import annotations

from typing import Any

try:
    from cocina.printer import Printer
except ImportError:
    Printer = None


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


class _LazyPrinter:
    """Forwards attribute access to the Printer singleton (or a no-op).

    Attribute access returns the singleton's *bound method*, so the call
    itself puts no ondio._log frame on the stack — cocina's automatic
    caller-name headers (`caller_name` skips only cocina frames) keep
    labeling messages with the real calling module.
    """

    def __getattr__(self, name: str) -> Any:
        if Printer is None:
            return _noop
        return getattr(Printer(), name)


printer = _LazyPrinter()

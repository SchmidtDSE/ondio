"""Lazy, optional logging through cocina's Printer.

cocina's `Printer` is a process-wide singleton: whoever constructs it first
fixes its configuration (log file, silence, ...), and later constructor
arguments are silently ignored. This module therefore never constructs it at
import time — the instance is resolved on each attribute access, so an
application that configures `Printer(...)` before ondio's first log line
wins. If cocina is not installed, every call is a silent no-op: logging
narrates, exceptions are the failure signal, so nothing is lost.

Callers with their own printer-like object (anything with `.message`,
`.error`, ...) can route ondio's narration to it with `set_printer`; see its
docstring for the contract.

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


_override: Any | None = None


def set_printer(printer_like: Any | None) -> None:
    """Route ondio's log lines to a custom printer-like object.

    Anything duck-typed like cocina's Printer works — attributes ondio logs
    through (`.message`, `.error`, ...) are looked up by name, and ones the
    object doesn't implement are silently skipped. The override is
    process-global, mirroring the Printer singleton it replaces; per-call
    routing is not supported (call kwargs are reserved for backend
    configuration).

    Args:
        printer_like: Object to receive ondio's log calls, or None to
            restore the default — the cocina Printer singleton if cocina is
            installed, otherwise a silent no-op.
    """
    global _override
    _override = printer_like


class _LazyPrinter:
    """Forwards attribute access to the Printer singleton (or a no-op).

    A `set_printer` override takes precedence. Otherwise attribute access
    returns the singleton's *bound method*, so the call itself puts no
    ondio._log frame on the stack — cocina's automatic caller-name headers
    (`caller_name` skips only cocina frames) keep labeling messages with the
    real calling module.
    """

    def __getattr__(self, name: str) -> Any:
        if _override is not None:
            return getattr(_override, name, _noop)
        if Printer is None:
            return _noop
        return getattr(Printer(), name)


printer = _LazyPrinter()

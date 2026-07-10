"""The _LazyPrinter contract: cocina is optional, and labels name the caller.

Logging narrates, exceptions are the failure signal — so with cocina absent
every call must be a silent no-op, and with it present the singleton's
auto-labels must name the *calling* module (the bound-method forwarding puts
no ondio._log frame on the stack).
"""

import pytest

import ondio._log as log_mod
from ondio._log import printer


def test_noop_when_cocina_absent(monkeypatch, capsys):
    monkeypatch.setattr(log_mod, "Printer", None)
    assert printer.message("nothing to see") is None
    assert printer.error("nor here") is None
    assert capsys.readouterr().out == ""


def test_messages_carry_callers_label(capsys):
    pytest.importorskip("cocina.printer")
    printer.message("hello from the test module")
    out = capsys.readouterr().out
    assert "hello from the test module" in out
    assert "test_log" in out  # labeled with the calling module …
    assert "ondio._log" not in out  # … not the forwarding shim

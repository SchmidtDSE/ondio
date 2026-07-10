"""The _LazyPrinter contract: cocina is optional, and labels name the caller.

Logging narrates, exceptions are the failure signal — so with cocina absent
every call must be a silent no-op, and with it present the singleton's
auto-labels must name the *calling* module (the bound-method forwarding puts
no ondio._log frame on the stack). A `set_printer` override outranks both
defaults, and partial duck types must never crash a log call.
"""

import pytest

import ondio._log as log_mod
from ondio._log import printer, set_printer


@pytest.fixture(autouse=True)
def _restore_default_printer():
    yield
    set_printer(None)


class _Recorder:
    def __init__(self):
        self.calls = []

    def message(self, *args, **kwargs):
        self.calls.append(("message", args, kwargs))

    def error(self, *args, **kwargs):
        self.calls.append(("error", args, kwargs))


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


def test_set_printer_routes_calls_to_override(capsys):
    recorder = _Recorder()
    set_printer(recorder)
    printer.message("routed", detail=1)
    printer.error("also routed")
    assert recorder.calls == [
        ("message", ("routed",), {"detail": 1}),
        ("error", ("also routed",), {}),
    ]
    assert capsys.readouterr().out == ""  # nothing leaks to the default path


def test_set_printer_tolerates_partial_ducks():
    class OnlyMessage:
        def message(self, *args, **kwargs):
            pass

    set_printer(OnlyMessage())
    assert printer.error("no error method — must not raise") is None


def test_set_printer_none_restores_default(monkeypatch, capsys):
    set_printer(_Recorder())
    set_printer(None)
    monkeypatch.setattr(log_mod, "Printer", None)
    assert printer.message("back to the no-op default") is None
    assert capsys.readouterr().out == ""


def test_set_printer_overrides_cocina_singleton(capsys):
    pytest.importorskip("cocina.printer")
    recorder = _Recorder()
    set_printer(recorder)
    printer.message("override outranks the singleton")
    assert capsys.readouterr().out == ""
    assert recorder.calls == [
        ("message", ("override outranks the singleton",), {}),
    ]

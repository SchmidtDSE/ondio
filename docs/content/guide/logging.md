# Logging

`ondio` writes short log lines about what it is doing, such as which backend a URI
uses and whether a FLAC window is fetched with a byte range. The lines only
describe progress: every failure raises an exception, so nothing is lost if you
never see them.

## Where the lines go

`ondio` uses the `Printer` from [`cocina`](https://github.com/SchmidtDSE/cocina) when
`cocina` is installed. `cocina` is not a dependency of `ondio`; without it, `ondio` logs
nothing.

`cocina`'s `Printer` is one object for the whole process, and the first code to
construct it sets its configuration. `ondio` never constructs it at import, so if
your application constructs `Printer(...)` before `ondio`'s first log line, your
configuration is used.

## Your own logger

`ondio.set_printer` sends the lines to any object with a `message` method instead.
This example passes them to Python's `logging`:

```python
import logging

import ondio

log = logging.getLogger("ondio")


class ToLogging:
    def message(self, text, *args, **kwargs):
        log.info(text)


ondio.set_printer(ToLogging())
```

The setting applies to the whole process. `ondio.set_printer(None)` goes back to
the default.

## Turning it off

An object without a `message` method receives nothing, so this silences `ondio`
even when `cocina` is installed:

```python
ondio.set_printer(object())
```

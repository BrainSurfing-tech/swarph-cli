"""Encoding-safe operator output — the one place the daemon's loop may print from.

WHY THIS IS ITS OWN MODULE rather than a helper inside ``commands/daemon.py``:
the modules that most need the guard are the ones ``daemon`` IMPORTS
(``delivery_queue``, ``stall_alert``, ``session_bridge``), so they cannot import it
back. The guard living in the consumer meant the producers went unguarded — and
that is exactly where the two surviving crashes were found (``delivery_queue.py:43``,
``stall_alert.py:49``), by droplet, in the PUBLISHED 0.40.1 artifact.

>>> THE GUARD'S HOME WAS ITSELF A COVERAGE BOUNDARY. A protection sited inside one
    module protects one module, however carefully that module is then audited. <<<

THE BUG IT EXISTS FOR. On a console whose encoding cannot represent a character
(cp1252 on a French Windows box, any 8-bit codepage), ``print`` raises
``UnicodeEncodeError``. In an ``except`` block that raise propagates past the ``try``
entirely — so an error handler whose whole contract is "never crash the loop" becomes
the crash, and it does so on the text MOST likely to carry an unrepresentable
character: an interpolated exception message.

Measured cp1252 coverage of what this fleet actually emits:

====================================  ==========
em-dash, bullet, accented e, guillemet  renders
arrow, box-drawing, emoji, check-mark   RAISES
====================================  ==========

The em-dash renders, so the original DM-blindness on workstation-lc was an arrow or
an emoji — never punctuation.

A SYMBOL GREP MEASURES PRESENCE, NOT COVERAGE: ``grep -c _print_safe`` returns 1
whether this guards one call site or every one. Coverage is asserted by an AST walk
over every module the daemon's loop can reach (``test_daemon_console_encoding.py``),
and that test is itself proven non-vacuous by reintroducing a bare ``print``.
"""

from __future__ import annotations

import sys

__all__ = ["print_safe"]


def print_safe(line: str, *, stream=None, file=None, flush: bool = True) -> None:
    """Print, degrading to a lossy transliteration rather than raising.

    A console that cannot represent the text must cost a mangled character, never a
    poll iteration. Callers that also persist the line (the daemon's inbox audit)
    write to disk in utf-8 BEFORE calling here, so a cp1252 box loses glyphs from its
    screen and never messages from its inbox.

    ``file=`` is accepted as an alias for ``stream=``, and ``flush`` is accepted and
    ignored (this always flushes). Deliberate rather than sloppy: every call site was
    once a ``print``, and a future author reaching for the print spelling must land in
    the guard rather than a ``TypeError`` — a ``TypeError`` inside the delivery-error
    handler is the same crash this function exists to prevent, wearing another name.

    Passing both ``stream=`` and ``file=`` with different targets silently uses
    ``stream``. Recorded decision, not an accident (droplet, PR #158): raising on the
    nonsensical call would put a new ``raise`` inside the one function whose entire
    contract is that it never raises — the cure would be the disease.
    """
    out = stream if stream is not None else (file if file is not None else sys.stdout)
    try:
        print(line, file=out, flush=True)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        try:
            out.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
            out.write("\n")
            out.flush()
        except (OSError, UnicodeEncodeError, ValueError, AttributeError):
            # The fallback failing must not resurrect the crash it exists to prevent.
            # Losing an operator line is survivable; losing the loop is not.
            pass
    except (OSError, ValueError):
        # A closed/broken stream (detached service, full disk) is no reason to stop
        # draining. ValueError covers write-on-closed-file, which teardown reaches.
        pass

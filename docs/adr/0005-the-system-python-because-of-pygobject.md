# The project runs on the system Python, not on whatever is in `PATH`

Talking to Evolution Data Server requires PyGObject, and on the machine this was
built on, `gi` exists only in `/usr/bin/python3` (3.12), installed by the
`python3-gi` package into `/usr/lib/python3/dist-packages`. The `python3` that
answers in `PATH` was a Homebrew build (3.14) with **no** `gi`.

So the project pins the system interpreter and creates its virtualenv with
`--system-site-packages`, so the venv can see the system bindings.

None of this is visible in any source file, and a `python3 -m venv` typed from
reflex produces an environment where `import gi` fails with no obvious
explanation.

## Considered Options

- **Keep the main language on another stack** (TypeScript or Go) and isolate the
  calendar in a small Python process. Rejected: it puts the project's most
  fragile integration behind a process boundary, which is where a bug becomes a
  mystery.
- **Install PyGObject through pip on the other Python.** Rejected: it requires a
  compilation toolchain and, even when it compiles, still depends on the system
  typelibs.

## Consequences

- The calendar path is tied to the Python version the distribution ships. In
  exchange it gains stability: that version only changes when the system does.
- The `gir1.2-ecal-2.0` and `gir1.2-edataserver-1.2` typelibs are also required,
  and are not installed by default.

## Amendment, 2026-08-11: this constrains the calendar, not the project

The `requires-python` upper bound was `<3.13`, and that was wrong — it described
one machine's system Python rather than a constraint of the code.

The core imports `gi` nowhere, and the whole test suite passes with it blocked;
CI proves this on a bare `ubuntu-latest`. On a machine whose system Python is
3.13, `gi` is there for 3.13.

The rule still holds where it is true: **if you want the calendar integration**,
create the venv with the system Python, because that is where PyGObject lives. If
you do not, any Python 3.12+ works.

'''
test_cli_reference.py - the "spotdlp -v" command list stays complete.

The reference is curated for wording and order, so a new command is easy to add
and easy to forget. This fails the moment one is registered without a line.
'''
from __future__ import annotations

from spotdlplus.cli.main import _COMMAND_BLURB, app


def _registered_names() -> set[str]:
    return {c.name or c.callback.__name__.replace('_', '-') for c in app.registered_commands}


def test_every_command_has_a_reference_line():
    missing = _registered_names() - set(_COMMAND_BLURB)
    assert not missing, f'these commands have no "spotdlp -v" line: {sorted(missing)}'


def test_the_reference_lists_nothing_that_does_not_exist():
    stale = set(_COMMAND_BLURB) - _registered_names()
    assert not stale, f'these "spotdlp -v" lines name commands that are gone: {sorted(stale)}'

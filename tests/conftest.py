'''
Shared test fixtures.
'''
from __future__ import annotations

import pytest
from typer.main import get_command

from spotdlplus.cli.main import app


@pytest.fixture
def command_options():
    '''
    Returns a function that gives every flag a command declares, read straight
    from the parser instead of grepped out of --help. typer compacts its help on
    a narrow or color-forced terminal and shows only the short flag, dropping
    --out and leaving -o, so grepping the rendered text passed on every local
    run and turned CI red on all eight jobs. The parser never lies.
    '''
    def names(command: str) -> set[str]:
        cmd = get_command(app).commands[command]
        return {opt for param in cmd.params for opt in param.opts}
    return names

'''
test_vault_option.py - `-o` means a world, not a folder.

The footgun this flag exists to prevent, learned on the real archive:
redirecting output_dir while sharing the global ownership database makes every
re-download skip as "owned" while the new folder stays empty. `-o` therefore
moves the state WITH the files, because each vault owns its own database.
'''

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from spotdlplus.cli.main import _load, app
from spotdlplus.core.models import ArtistRef, Track
from spotdlplus.core.store import Store

runner = CliRunner()


# ----------------------------------------------------------------------------
# the config pairing
# ----------------------------------------------------------------------------

def test_out_moves_output_and_state_together(tmp_path):
    vault = tmp_path / 'Vault'
    cfg = _load(vault)
    assert cfg.output_dir == vault
    assert cfg.state_dir == vault / '.spotdlplus'
    assert cfg.db_path == vault / '.spotdlplus' / 'jobs.db'


def test_omitting_out_changes_nothing(tmp_path):
    cfg = _load(None)
    assert '.spotdlplus' not in str(cfg.output_dir)


def test_out_still_honors_other_overrides(tmp_path):
    cfg = _load(tmp_path / 'V', profile='studio')
    assert cfg.profile == 'studio'
    assert cfg.output_dir == tmp_path / 'V'


def test_none_overrides_are_dropped_not_passed(tmp_path):
    '''`-p` unset arrives as None. it must not clobber the configured profile.'''
    cfg = _load(tmp_path / 'V', profile=None)
    assert cfg.profile in ('canonical', 'completionist', 'studio')


# ----------------------------------------------------------------------------
# two vaults are two worlds
# ----------------------------------------------------------------------------

def _own_a_track(vault: Path, isrc: str) -> None:
    cfg = _load(vault)
    store = Store(cfg.db_path)
    t = Track(title='Song', artists=(ArtistRef(name='A'),), isrc=isrc, duration_ms=1000)
    run = store.create_run('x', 'canonical')
    store.add_track(run, t)
    store.remember(t.identity, str(vault / 'A' / 'song.opus'), 'opus', 100, 'sha')
    store.close()


def test_ownership_does_not_leak_between_vaults(tmp_path):
    '''The whole point: owning a song in vault A must not skip it in vault B.'''
    a, b = tmp_path / 'A', tmp_path / 'B'
    _own_a_track(a, 'USAAA0000001')

    cfg_b = _load(b)
    store_b = Store(cfg_b.db_path)
    try:
        assert store_b.own('isrc:USAAA0000001') is None, \
            'vault B consulted vault A\'s ownership, the footgun is back'
    finally:
        store_b.close()

    cfg_a = _load(a)
    store_a = Store(cfg_a.db_path)
    try:
        assert store_a.own('isrc:USAAA0000001') is not None, 'vault A still owns it'
    finally:
        store_a.close()


# ----------------------------------------------------------------------------
# the flag exists on every connected command, spelled the same way
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('command', ['get', 'plan', 'resume', 'status',
                                     'relink', 'audit', 'library', 'doctor'])
def test_every_connected_command_advertises_out(command):
    result = runner.invoke(app, [command, '--help'])
    assert result.exit_code == 0
    assert '--out' in result.output and '-o' in result.output


def test_library_reads_the_vault_it_is_pointed_at(tmp_path):
    vault = tmp_path / 'V'
    _own_a_track(vault, 'USAAA0000002')
    result = runner.invoke(app, ['library', '-o', str(vault)])
    assert result.exit_code == 0
    assert '1 track' in result.output   # singular now, '1 tracks' was the bug


def test_status_reads_the_vault_it_is_pointed_at(tmp_path):
    vault = tmp_path / 'Empty'
    result = runner.invoke(app, ['status', '-o', str(vault)])
    assert result.exit_code == 0
    assert 'no runs yet' in result.output

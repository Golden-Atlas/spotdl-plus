'''
test_shared_output_dir.py - pointing spotdl+ at a folder that already has stuff
in it must not cost you the stuff.

The scenario: you already keep music in D:\\Music, you set output_dir to it, and
you expect your library to be ADDED to that folder rather than poured over it.
A rendered path can land exactly on a file that was there first, and placement
is an atomic replace, so without a guard that file is gone with no message.

Ownership is the test. A library row means the file is ours and fresh work wins
over it, which is the 1.1.x fix for corrupt files wedging themselves in place.
No library row means it belongs to whoever put it there, and we stop.
'''

from __future__ import annotations

from pathlib import Path

import pytest

from spotdlplus.core.errors import WouldOverwrite
from spotdlplus.core.models import Album, ArtistRef, Track
from spotdlplus.core.store import Store

ARTIST = ArtistRef(name='Radiohead')


def track(title='Airbag', isrc='GBAYE9701274'):
    return Track(title=title, artists=(ARTIST,),
                 album=Album(title='OK Computer', artists=(ARTIST,)),
                 isrc=isrc, duration_ms=287_900)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


# ----------------------------------------------------------------------------
# owner_of_path: the question placement asks before it replaces anything
# ----------------------------------------------------------------------------

def test_a_file_nobody_claimed_has_no_owner(store, tmp_path):
    stranger = tmp_path / 'lib' / 'Radiohead' / 'OK Computer (1997)' / '01 Airbag.opus'
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b'i was here first')
    assert store.owner_of_path(stranger) is None


def test_our_own_file_has_an_owner(store, tmp_path):
    run = store.create_run('x', 'canonical')
    t = track()
    store.add_track(run, t)
    ours = tmp_path / 'lib' / 'Radiohead' / 'OK Computer (1997)' / '01 Airbag.opus'
    ours.parent.mkdir(parents=True)
    ours.write_bytes(b'ours')
    store.remember(t.identity, str(ours), 'opus', 4, 'sha')

    row = store.owner_of_path(ours)
    assert row is not None
    assert row['identity'] == t.identity


def test_ownership_survives_a_differently_spelled_path(store, tmp_path):
    '''
    The stored path and a freshly rendered one can differ by separator or case
    and still be the same file. If that made us think our own file was a
    stranger's, every re-place would refuse and the library would jam.
    '''
    run = store.create_run('x', 'canonical')
    t = track()
    store.add_track(run, t)
    ours = tmp_path / 'lib' / 'a.opus'
    ours.parent.mkdir(parents=True)
    ours.write_bytes(b'ours')
    store.remember(t.identity, str(ours), 'opus', 4, 'sha')

    respelled = Path(str(tmp_path / 'lib') + '/' + 'a.opus')
    assert store.owner_of_path(respelled) is not None


# ----------------------------------------------------------------------------
# the error a person actually sees
# ----------------------------------------------------------------------------

def test_the_refusal_names_the_file_and_says_what_to_do():
    err = WouldOverwrite('42: 01 Airbag.opus already exists and is not ours',
                         context={'row': '42', 'path': 'D:/Music/x.opus'})
    assert err.code == 'FS_WOULD_OVERWRITE'
    assert err.retry.value == 'never', 'retrying finds the same file sitting there'
    assert 'move or rename it' in err.remedy.lower()
    assert 'output_dir' in err.remedy


# ----------------------------------------------------------------------------
# uninstall --everything: our files go, theirs stay
# ----------------------------------------------------------------------------

def test_uninstall_everything_spares_files_that_were_not_ours(tmp_path):
    '''
    The whole promise of pointing output_dir at a folder you already use. We
    delete what a library row claims and nothing else, and we say out loud how
    many we left, so 'it removed my stuff' can never be a silent surprise.
    '''
    from typer.testing import CliRunner

    from spotdlplus.cli.main import _load, app

    vault = tmp_path / 'Shared'
    (vault / 'Radiohead' / 'OK Computer (1997)').mkdir(parents=True)

    stranger = vault / 'Radiohead' / 'OK Computer (1997)' / '99 Ripped Years Ago.opus'
    stranger.write_bytes(b'mine, from before')
    doc = vault / 'notes.txt'
    doc.write_text('not even audio', encoding='utf-8')

    cfg = _load(vault)
    store = Store(cfg.db_path)
    t = track()
    store.add_track(store.create_run('x', 'canonical'), t)
    ours = vault / 'Radiohead' / 'OK Computer (1997)' / '01 Airbag.opus'
    ours.write_bytes(b'ours')
    store.remember(t.identity, str(ours), 'opus', 4, 'sha')
    store.close()

    res = CliRunner().invoke(app, ['uninstall', '--everything', '-y', '-o', str(vault)])
    assert res.exit_code == 0, res.output

    assert not ours.exists(), 'our download is gone'
    assert stranger.exists(), 'their audio file survived'
    assert stranger.read_bytes() == b'mine, from before', 'and was not rewritten'
    assert doc.exists(), 'their non-audio file survived'
    assert '1 audio file(s)' in res.output and 'not ours' in res.output


def test_plain_uninstall_keeps_the_music_and_the_ownership(tmp_path):
    from typer.testing import CliRunner

    from spotdlplus.cli.main import _load, app

    vault = tmp_path / 'Warm'
    cfg = _load(vault)
    store = Store(cfg.db_path)
    t = track()
    store.add_track(store.create_run('x', 'canonical'), t)
    ours = vault / 'a.opus'
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_bytes(b'ours')
    store.remember(t.identity, str(ours), 'opus', 4, 'sha')
    store.close()

    res = CliRunner().invoke(app, ['uninstall', '-y', '-o', str(vault)])
    assert res.exit_code == 0, res.output
    assert ours.exists(), 'a warm uninstall never touches your music'
    assert cfg.db_path.exists(), 'nor the ownership database'

    store = Store(cfg.db_path)
    try:
        assert store.own(t.identity) is not None, 'reinstall plugs straight back in'
    finally:
        store.close()


def test_uninstall_in_a_vault_never_reaches_the_global_config(tmp_path, monkeypatch):
    '''
    The one that bit me. `-o` redirects output_dir and state_dir but NOT
    cache_dir, so deriving the app folder from the cache reached straight out of
    the vault and deleted the real config. I ran this command against a temp
    vault while testing it and lost my own Spotify credentials.

    A vault owns its own state and its own files. Nothing else.
    '''
    from typer.testing import CliRunner

    from spotdlplus.cli.main import _load, app

    # a stand-in for the shared %LOCALAPPDATA%\spotdlplus that must survive
    shared = tmp_path / 'AppData' / 'spotdlplus'
    for sub in ('cache', 'config', 'tools'):
        (shared / sub).mkdir(parents=True)
    (shared / 'config' / 'config.toml').write_text('secret = "keep me"', encoding='utf-8')
    (shared / 'cache' / 'blob.bin').write_bytes(b'x')
    (shared / 'tools' / 'deno.exe').write_bytes(b'x')
    monkeypatch.setenv('SPOTDLPLUS_CACHE_DIR', str(shared / 'cache'))

    vault = tmp_path / 'Vault'
    cfg = _load(vault)
    assert Path(cfg.cache_dir) == shared / 'cache', 'the vault really does share the cache'

    res = CliRunner().invoke(app, ['uninstall', '--everything', '-y', '-o', str(vault)])
    assert res.exit_code == 0, res.output

    assert (shared / 'config' / 'config.toml').exists(), 'the global config survived'
    assert (shared / 'config' / 'config.toml').read_text(encoding='utf-8') == 'secret = "keep me"'
    assert (shared / 'cache' / 'blob.bin').exists(), 'the shared cache survived'
    assert (shared / 'tools' / 'deno.exe').exists(), 'the shared tools survived'
    assert 'vault only' in res.output

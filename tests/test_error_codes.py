'''
test_error_codes.py - the friend-facing codes fire on the right conditions.

Each of these is a real thing a person on a different machine hits. Proving the
classifier maps the raw failure to the friendly, remedy-carrying code is the
whole point, because a generic error helps nobody at 3am.
'''

from __future__ import annotations

import socket
import sqlite3

import pytest

from spotdlplus.core import errors as E


# ----------------------------------------------------------------------------
# every code is real: registered, explainable, carries a remedy
# ----------------------------------------------------------------------------

NEW_CODES = [
    'NET_DNS', 'NET_CAPTIVE', 'NET_CLOCK_SKEW', 'META_REGION', 'FS_DRIVE_LOST',
    'STORE_BUSY', 'STORE_CORRUPT', 'TOOL_QUARANTINED', 'COOKIES_UNREADABLE',
]


@pytest.mark.parametrize('code', NEW_CODES)
def test_code_is_registered_with_a_real_remedy(code):
    cls = E.lookup(code)
    assert cls is not None, f'{code} not registered'
    assert cls.remedy and cls.remedy != E.SpotdlPlusError.remedy
    assert len(cls.remedy) > 20


# ----------------------------------------------------------------------------
# network classifiers (http.py)
# ----------------------------------------------------------------------------

def test_dns_failure_detected():
    from spotdlplus.net.http import is_dns_failure
    assert is_dns_failure(socket.gaierror(11001, 'getaddrinfo failed'))
    assert is_dns_failure(OSError('Name or service not known'))
    assert not is_dns_failure(OSError('connection reset by peer'))


def test_clock_skew_detected_and_distinct_from_trust():
    from spotdlplus.net.http import is_clock_skew, is_tls_trust_failure
    expired = Exception('[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired')
    assert is_clock_skew(expired)
    # a plain untrusted CA is NOT clock skew (it's antivirus territory)
    untrusted = Exception('[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate')
    assert not is_clock_skew(untrusted)
    assert is_tls_trust_failure(untrusted)


# ----------------------------------------------------------------------------
# cookie extraction (ytenv)
# ----------------------------------------------------------------------------

def test_cookie_extraction_error_detected():
    from spotdlplus.net.ytenv import is_cookie_extraction_error
    assert is_cookie_extraction_error('Could not copy Chrome cookie database')
    assert is_cookie_extraction_error('Permission denied while reading cookies')
    assert is_cookie_extraction_error('could not find firefox cookies database')
    # a 'please enable cookies' bot wall is NOT a read failure
    assert not is_cookie_extraction_error('Sign in to confirm. Please enable cookies.')


# ----------------------------------------------------------------------------
# the store (store.py)
# ----------------------------------------------------------------------------

def test_sqlite_locked_is_store_busy():
    from spotdlplus.core.store import _sqlite_error
    mapped = _sqlite_error(sqlite3.OperationalError('database is locked'))
    assert isinstance(mapped, E.StoreBusy)


def test_sqlite_malformed_is_store_corrupt():
    from spotdlplus.core.store import _sqlite_error
    mapped = _sqlite_error(sqlite3.DatabaseError('database disk image is malformed'))
    assert isinstance(mapped, E.StoreCorrupt)


def test_sqlite_other_errors_pass_through():
    from spotdlplus.core.store import _sqlite_error
    assert _sqlite_error(sqlite3.OperationalError('no such column: xyz')) is None


# ----------------------------------------------------------------------------
# a bundled tool that vanished mid-run (transcode.py)
# ----------------------------------------------------------------------------

def test_absolute_missing_tool_is_quarantined_not_missing():
    import os

    from spotdlplus.media.transcode import _run
    # an absolute path we 'resolved' to a bundled binary that isn't there =
    # antivirus quarantine, not a PATH problem. It has to be absolute on the OS
    # running the test. 'C:/...' is absolute on Windows but a plain relative
    # name on Linux, which sent this down the wrong branch and turned CI red.
    missing = 'C:/gone/ffmpeg.exe' if os.name == 'nt' else '/gone/ffmpeg.exe'
    with pytest.raises(E.ToolQuarantined):
        _run([missing, '-version'], timeout_s=5, what='x')


def test_bare_name_not_on_path_is_tool_missing():
    from spotdlplus.media.transcode import _run
    with pytest.raises(E.ToolMissing):
        _run(['spotdlplus_no_such_binary_xyz', '-version'], timeout_s=5, what='x')


# ----------------------------------------------------------------------------
# the output drive got unplugged mid-place (place.py)
# ----------------------------------------------------------------------------

def test_place_onto_a_vanished_drive_is_fs_drive_lost(tmp_path, monkeypatch):
    from spotdlplus.media import place

    src = tmp_path / 's.opus'
    src.write_bytes(b'x' * 32)
    dest = tmp_path / 'lib' / 'song.opus'

    def boom(*a, **k):
        raise OSError(2, 'the device is not ready')

    # the atomic replace fails AND the drive's root is gone, meaning unplugged
    monkeypatch.setattr(place.os, 'replace', boom)
    monkeypatch.setattr(place.os.path, 'exists', lambda p: False)

    with pytest.raises(E.FsDriveLost):
        place.place_atomic(src, dest)

'''
test_fingerprint.py - the acoustic layer, both halves.

fpcalc tests run against the real binary when provisioned (it is, on the dev
box) and skip cleanly elsewhere. AcoustID tests run against a mock transport,
the three-valued verdict logic is pure and must not need the network to prove.
'''

from __future__ import annotations

import shutil
import subprocess

import httpx
import pytest

from spotdlplus.core.errors import CredentialsMissing, VerifyFailed
from spotdlplus.media.fingerprint import Fingerprint, find_fpcalc, fingerprint_file
from spotdlplus.net.http import HttpClient
from spotdlplus.net.ratelimit import HostLimiter
from spotdlplus.providers.acoustid import MIN_SCORE, AcoustIdClient, Verdict

needs_fpcalc = pytest.mark.skipif(find_fpcalc() is None, reason='fpcalc not provisioned')
needs_ffmpeg = pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='no ffmpeg')


# ----------------------------------------------------------------------------
# fpcalc: decode-deep verification
# ----------------------------------------------------------------------------

@needs_fpcalc
@needs_ffmpeg
def test_a_clean_file_fingerprints(tmp_path):
    tone = tmp_path / 'tone.opus'
    subprocess.run(
        ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
         '-c:a', 'libopus', str(tone)],
        check=True, capture_output=True,
    )
    fp = fingerprint_file(tone)
    assert len(fp.fingerprint) > 10, 'a 3s tone still fingerprints'
    assert fp.duration_s in (2, 3), 'fpcalc rounds. either neighbor is honest'


@needs_fpcalc
def test_a_damaged_file_is_a_finding_not_a_crash(tmp_path):
    '''
    The Sakura lesson: a file can pass ffprobe and mutagen while carrying
    CRC-mismatched pages that only a full decode reveals. fingerprint_file is
    that full decode, and damage must surface as a typed VerifyFailed.
    '''
    fake = tmp_path / 'damaged.opus'
    fake.write_bytes(b'OggS' + b'\x00' * 512)   # a header and lies
    with pytest.raises(VerifyFailed) as exc:
        fingerprint_file(fake)
    assert 'decode' in str(exc.value)


# ----------------------------------------------------------------------------
# acoustid: the three-valued verdict
# ----------------------------------------------------------------------------

FP = Fingerprint(fingerprint='AQAAf' * 20, duration_s=238)


def client(payload) -> AcoustIdClient:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    http = HttpClient(transport=transport, limiter=HostLimiter({}, fallback=(1000.0, 1000)))
    return AcoustIdClient(http, 'test-key')


def result(score, *recording_ids):
    return {'score': score,
            'recordings': [{'id': rid} for rid in recording_ids]}


def test_agreement_confirms():
    c = client({'results': [result(0.97, 'mb-rec-airbag')]})
    m = c.verify(FP, expected_recording_id='mb-rec-airbag')
    assert m.verdict is Verdict.CONFIRMED
    assert m.score == 0.97


def test_a_strong_match_to_a_different_recording_is_the_damning_verdict():
    c = client({'results': [result(0.95, 'mb-rec-some-cover')]})
    m = c.verify(FP, expected_recording_id='mb-rec-airbag')
    assert m.verdict is Verdict.MISMATCH
    assert 'mb-rec-some-cover' in m.detail


def test_no_result_is_unknown_not_mismatch():
    '''Bedroom artists are not in AcoustID. Absence of evidence only flags.'''
    c = client({'results': []})
    m = c.verify(FP, expected_recording_id='mb-rec-x')
    assert m.verdict is Verdict.UNKNOWN


def test_a_weak_match_is_noise_not_evidence():
    c = client({'results': [result(MIN_SCORE - 0.1, 'mb-rec-wrong')]})
    m = c.verify(FP, expected_recording_id='mb-rec-x')
    assert m.verdict is Verdict.UNKNOWN, 'a 0.5 score must not quarantine anything'


def test_no_expected_id_still_confirms_known_audio():
    '''Spotify-only metadata: a strong hit proves "a known recording", no cross-check.'''
    c = client({'results': [result(0.9, 'mb-rec-whatever')]})
    m = c.verify(FP, expected_recording_id=None)
    assert m.verdict is Verdict.CONFIRMED
    assert 'no MB id' in m.detail


def test_results_are_taken_best_first():
    c = client({'results': [result(0.61, 'mb-weak'), result(0.98, 'mb-strong')]})
    m = c.verify(FP, expected_recording_id='mb-strong')
    assert m.verdict is Verdict.CONFIRMED and m.score == 0.98


def test_no_key_refuses_with_the_registration_url():
    http = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                      limiter=HostLimiter({}, fallback=(1000.0, 1000)))
    c = AcoustIdClient(http, None)
    assert not c.available
    with pytest.raises(CredentialsMissing) as exc:
        c.lookup(FP)
    assert 'acoustid.org/new-application' in str(exc.value)

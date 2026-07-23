'''
acoustid.py - acoustic verification

AcoustID maps Chromaprint fingerprints to MusicBrainz recording ids, which is
the last link in the chain. Spotify gave us an ISRC, MusicBrainz gave us a
recording, and this asks whether the bytes on disk actually agree.

Verdicts are three-valued on purpose. A real mismatch is quarantine-grade, but
UNKNOWN only flags, because plenty of real music just isn't in the database and
that isn't the file's fault.
'''

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.errors import CredentialsMissing
from ..core.models import Fingerprint
from ..net.http import HttpClient

API = 'https://api.acoustid.org/v2/lookup'

#: Below this AcoustID score, a "match" is noise, not evidence.
MIN_SCORE = 0.60


class Verdict(StrEnum):
    CONFIRMED = 'confirmed'
    MISMATCH = 'mismatch'
    UNKNOWN = 'unknown'


@dataclass(frozen=True, slots=True)
class AcoustMatch:
    verdict: Verdict
    score: float = 0.0
    recording_ids: tuple[str, ...] = ()
    detail: str = ''


class AcoustIdClient:
    def __init__(self, http: HttpClient, api_key: str | None) -> None:
        self._http = http
        self._key = api_key

    @property
    def available(self) -> bool:
        return bool(self._key)

    def lookup(self, fp: Fingerprint) -> list[dict]:
        '''Raw results, best first. Raises CredentialsMissing without a key.'''
        if not self._key:
            raise CredentialsMissing(
                'no AcoustID API key configured. Register a free application at '
                'acoustid.org/new-application and set acoustid_api_key in '
                'config.toml (or ACOUSTID_API_KEY).',
            )
        body = self._http.request(
            'POST', API,
            data={
                'client': self._key,
                'duration': str(fp.duration_s),
                'fingerprint': fp.fingerprint,
                'meta': 'recordingids',
            },
        ).json()
        results = body.get('results') or []
        return sorted(results, key=lambda r: -(r.get('score') or 0.0))

    def verify(self, fp: Fingerprint, *, expected_recording_id: str | None) -> AcoustMatch:
        '''
        Returns the three-valued verdict for one file. Without an expected recording id
        a strong hit still counts as CONFIRMED, since the audio is a known recording
        rather than a mislabeled cover.
        '''
        results = [r for r in self.lookup(fp) if (r.get('score') or 0.0) >= MIN_SCORE]
        if not results:
            return AcoustMatch(Verdict.UNKNOWN, detail='no fingerprint match in AcoustID')

        best = results[0]
        score = best.get('score') or 0.0
        recordings = tuple(
            rec.get('id') for rec in (best.get('recordings') or []) if rec.get('id')
        )

        if expected_recording_id is None:
            return AcoustMatch(Verdict.CONFIRMED, score, recordings,
                               'matches a known recording. no MB id to cross-check')

        if expected_recording_id in recordings:
            return AcoustMatch(Verdict.CONFIRMED, score, recordings,
                               'fingerprint and metadata agree')

        return AcoustMatch(Verdict.MISMATCH, score, recordings,
                           f'audio matches {recordings[:2]}. Not the expected '
                           f'{expected_recording_id}')

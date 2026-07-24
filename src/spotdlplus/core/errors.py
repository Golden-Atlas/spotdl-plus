'''
errors.py - error types, retry policies, and remedies

Every failure gets a name, a code, a retry policy, and a remedy. When a 4,000
track sync drops at 3am on track 812 the store already knows whether to retry
or wait or stop, and you get a sentence telling you what to do about it.
'''

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


class Retry(StrEnum):
    '''How the pipeline should feel about trying this again.'''

    NEVER = 'never'      # deterministic. Asking again gets the same answer, forever.
    NOW = 'now'          # a blip. Go again right away (we still count the attempts).
    BACKOFF = 'backoff'  # the far end is unhappy. Exponential, jittered, patient.
    AFTER = 'after'      # they told us exactly when to come back. Obey it to the second.
    PARK = 'park'        # not our fault, not our fix. Freeze the run and keep everything.


# ----------------------------------------------------------------------------
# registry. So `spotdlp explain RATE_LIMITED` can answer without importing half
# the program.
# ----------------------------------------------------------------------------

_REGISTRY: dict[str, type['SpotdlPlusError']] = {}


def register(cls: type['SpotdlPlusError']) -> type['SpotdlPlusError']:
    '''Decorator. Puts an error class in the lookup table by its code.'''
    if cls.code in _REGISTRY:
        raise RuntimeError(f'duplicate error code: {cls.code}')
    _REGISTRY[cls.code] = cls
    return cls


def lookup(code: str) -> type['SpotdlPlusError'] | None:
    '''Find an error class by code. Powers the "explain" command.'''
    return _REGISTRY.get(code.upper())


def all_codes() -> dict[str, type['SpotdlPlusError']]:
    '''Every code we know how to fail with.'''
    return dict(_REGISTRY)


# ----------------------------------------------------------------------------
# base
# ----------------------------------------------------------------------------

@dataclass(slots=True)
class ErrorRecord:
    '''The flat, serializable shadow of an error. The shape the store keeps.'''

    code: str
    message: str
    retry: Retry
    remedy: str
    retry_after: float | None = None
    context: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            'code': self.code,
            'message': self.message,
            'retry': str(self.retry),
            'remedy': self.remedy,
            'retry_after': self.retry_after,
            'context': self.context,
            'at': self.at,
        }


class SpotdlPlusError(Exception):
    '''Root of the taxonomy. Never raised directly.'''

    code: ClassVar[str] = 'ERR_UNKNOWN'
    retry: ClassVar[Retry] = Retry.NEVER
    remedy: ClassVar[str] = 'No remedy recorded, which shouldn\'t happen. This is a bug. Please file it.'

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        retry_after: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}
        self.retry_after = retry_after
        if cause is not None:
            self.__cause__ = cause

    @property
    def retryable(self) -> bool:
        return self.retry is not Retry.NEVER

    def record(self) -> ErrorRecord:
        '''Freeze into the shape the job store persists.'''
        return ErrorRecord(
            code=self.code,
            message=self.message,
            retry=self.retry,
            remedy=self.remedy,
            retry_after=self.retry_after,
            context=dict(self.context),
        )

    def __str__(self) -> str:
        return f'[{self.code}] {self.message}'


# ----------------------------------------------------------------------------
# CFG: you and the config file disagree
# ----------------------------------------------------------------------------

@register
class ConfigInvalid(SpotdlPlusError):
    code = 'CFG_INVALID'
    retry = Retry.NEVER
    remedy = 'Something in config.toml isn\'t what we expected. Fix the offending key. "spotdlp doctor" shows the resolved config and where each value came from, which makes the culprit pretty easy to spot.'


@register
class OutputTemplateInvalid(SpotdlPlusError):
    code = 'CFG_BAD_TEMPLATE'
    retry = Retry.NEVER
    remedy = 'Your output template names a field that doesn\'t exist. Run "spotdlp doctor --templates" for the full list of valid placeholders.'


# ----------------------------------------------------------------------------
# AUTH: the part old spotdl would spin on forever
# ----------------------------------------------------------------------------

@register
class CredentialsMissing(SpotdlPlusError):
    code = 'AUTH_NO_CREDENTIALS'
    retry = Retry.NEVER
    remedy = 'No Spotify client ID/secret found. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET, or put them in config.toml. Creating an app at developer.spotify.com/dashboard is free and takes about 2 minutes.'


@register
class CredentialsRejected(SpotdlPlusError):
    code = 'AUTH_REJECTED'
    retry = Retry.NEVER
    remedy = 'Spotify refused these credentials outright. They\'re wrong, revoked, or the app was deleted. Re-copy them from the dashboard. A trailing newline is the usual culprit (it always is).'


@register
class TokenExpired(SpotdlPlusError):
    code = 'AUTH_TOKEN_EXPIRED'
    retry = Retry.NOW
    remedy = 'Normal and self-healing. The token aged out mid-flight and one refresh fixes it. If you\'re seeing this in a loop, the circuit breaker will trip and hand you AUTH_REFRESH_LOOP instead.'


@register
class AuthRefreshLoop(SpotdlPlusError):
    code = 'AUTH_REFRESH_LOOP'
    retry = Retry.NEVER
    remedy = 'We refreshed the token over and over and Spotify kept answering 401, so something is wrong beyond the token. Usually a revoked app, a clock that is off by minutes, or a scope you no longer hold. The run stopped instead of spinning forever. Check your system clock first, it is the culprit more often than you would think.'


# ----------------------------------------------------------------------------
# NET: the wire. We don't control it. We just live on it.
# ----------------------------------------------------------------------------

@register
class Offline(SpotdlPlusError):
    code = 'NET_OFFLINE'
    retry = Retry.PARK
    remedy = 'No route to the network. The run is parked, not lost. Every finished track is still on disk and the queue is intact. Reconnect and run "spotdlp resume", nothing needs redoing.'


@register
class TlsUntrusted(SpotdlPlusError):
    code = 'NET_TLS_UNTRUSTED'
    retry = Retry.NEVER
    remedy = (
        'We couldn\'t verify the server\'s certificate, so we refused to talk to it. '
        'This is almost never the server. The usual cause is antivirus software '
        '(AVG, Avast, Kaspersky, ESET, Bitdefender) or a corporate proxy that '
        'decrypts HTTPS and re-signs it with its own root certificate. Install the '
        'truststore package so we verify against your OS trust store instead of a '
        'bundled one, point SSL_CERT_FILE at a bundle containing that root, or turn '
        'off the HTTPS-scanning feature. We will not skip verification. That would '
        'be locking the door and leaving the window open.'
    )


@register
class NetTimeout(SpotdlPlusError):
    code = 'NET_TIMEOUT'
    retry = Retry.BACKOFF
    remedy = 'The far end went quiet. Retried automatically. If one host keeps timing out it is usually throttling you, so drop concurrency in config.toml.'


@register
class RateLimited(SpotdlPlusError):
    code = 'RATE_LIMITED'
    retry = Retry.AFTER
    remedy = 'You are going too fast and the server said so. We wait exactly as long as it asked, to the second. If this dominates a run, drop concurrency to 2. Speed here gets you banned, not finished sooner.'


@register
class NetDns(SpotdlPlusError):
    code = 'NET_DNS'
    retry = Retry.BACKOFF
    remedy = 'Couldn\'t find the server. DNS problem, not tool-bound. Typically a bad wifi connection. Try reconnecting, switching the DNS to 1.1.1.1, and trying again.'


@register
class NetCaptive(SpotdlPlusError):
    code = 'NET_CAPTIVE'
    retry = Retry.PARK
    remedy = 'You have to get through one of those captive network pages (the "click to join the wifi" screen) before anything works. Open a browser, clear it, then "spotdlp resume".'


@register
class NetClockSkew(SpotdlPlusError):
    code = 'NET_CLOCK_SKEW'
    retry = Retry.NEVER
    remedy = 'Fix your fucking system clock NOTETOSELF//change this'


# ----------------------------------------------------------------------------
# META: providers. They know things. They're not always right.
# ----------------------------------------------------------------------------

@register
class ProviderFailed(SpotdlPlusError):
    code = 'META_PROVIDER_FAILED'
    retry = Retry.BACKOFF
    remedy = 'A metadata provider returned junk. Enrichment is best-effort, so the run keeps going with what we do have. "spotdlp status --errors" shows which provider, if you care.'


@register
class EntityNotFound(SpotdlPlusError):
    code = 'META_NOT_FOUND'
    retry = Retry.NEVER
    remedy = 'The URL or ID doesn\'t resolve to anything: private playlist, deleted track, or a typo. Not much we can do from here.'


@register
class MetadataForbidden(SpotdlPlusError):
    code = 'META_FORBIDDEN'
    retry = Retry.NEVER
    remedy = 'The metadata provider has this, but not for us, and on Spotify that is one of two things. Either the playlist is private or collaborative: we sign in as an app, never as you, so we only ever see public things, your own private playlists included. Set it to public (the playlist menu, "Make public") and run it again. Or your Spotify app was created after February 2026, when Spotify stopped granting new apps playlist access; album and track reads still work, which is the tell, and a fresh app is the cause rather than the fix. For that one, use an app made before then, or the free web-player sign-in that needs no app at all. "spotdlp doctor" tells the two apart: if it also refuses the built-in public test playlist, the app is the problem, not your playlist.'


@register
class EntityRegionLocked(SpotdlPlusError):
    code = 'META_REGION'
    retry = Retry.NEVER
    remedy = 'Spotify won\'t serve this one where you are, it is region-locked to another country. Not much to do from here short of a VPN.'


@register
class BadRequest(SpotdlPlusError):
    code = 'META_BAD_REQUEST'
    retry = Retry.NEVER
    remedy = 'A provider rejected the shape of our request. Retrying would send the identical bytes and get the identical refusal, so we didn\'t. This one is our bug. The request and the response body are in the error context, please file it.'


# ----------------------------------------------------------------------------
# SPOTIFY WEB: the free sign-in, where the fragile parts live. Every remedy
# points at bring-your-own-app, because that is the one move a stuck user can
# make right now without waiting on me.
# ----------------------------------------------------------------------------

@register
class SpotifyWebSecretStale(SpotdlPlusError):
    code = 'SPOTIFY_WEB_SECRET_STALE'
    retry = Retry.NEVER
    remedy = 'The free Spotify sign-in broke because Spotify rotated the login secret it checks, and a fresh one didn\'t come through. Turn "spotify_secret_autofetch" on so the tool can grab the current one, or update spotdl+, or add your own free Spotify app credentials to skip the anonymous sign-in entirely. Run "spotdlp doctor" to see which of those applies.'


@register
class SpotifyWebSessionFailed(SpotdlPlusError):
    code = 'SPOTIFY_WEB_SESSION_FAILED'
    retry = Retry.BACKOFF
    remedy = 'Couldn\'t open a session at open.spotify.com to start the free sign-in. Usually the network, sometimes Spotify changed the page shape. Check the connection and run "spotdlp doctor --network". If it keeps happening the fix is on me, or add your own Spotify app key to route around it.'


@register
class SpotifyWebTokenFailed(SpotdlPlusError):
    code = 'SPOTIFY_WEB_TOKEN_FAILED'
    retry = Retry.NEVER
    remedy = 'The free Spotify token endpoint refused us for a reason that isn\'t the login secret, most likely it moved. Update spotdl+, or add your own Spotify app credentials to sign in the steady way.'


@register
class SpotifyWebClientTokenFailed(SpotdlPlusError):
    code = 'SPOTIFY_WEB_CLIENTTOKEN_FAILED'
    retry = Retry.BACKOFF
    remedy = 'Couldn\'t get the client-token that pairs with the free access token. Without it Spotify walls the requests, so we stopped rather than march into that. Try again in a moment, or add your own Spotify app key.'


@register
class SpotifyWebWalled(SpotdlPlusError):
    code = 'SPOTIFY_WEB_WALLED'
    retry = Retry.AFTER
    remedy = 'Spotify handed the free sign-in a multi-hour rate limit, which means the paired signature isn\'t being accepted or this IP is genuinely throttled. Wait it out, or add your own Spotify app credentials for higher, steadier limits.'


@register
class SpotifyWebQueryStale(SpotdlPlusError):
    code = 'SPOTIFY_WEB_QUERY_STALE'
    retry = Retry.NEVER
    remedy = 'The free Spotify path talks to the web player\'s own API, which names each query by an id that Spotify rotates when it ships a build. This build\'s ids stopped being recognised and re-reading the current ones from the web player didn\'t take. Update spotdl+ to pick up the new ids, or add your own Spotify app credentials to use the steady API instead. "spotdlp doctor --spotify-web" shows which query broke.'


@register
class AmbiguousQuery(SpotdlPlusError):
    code = 'META_AMBIGUOUS'
    retry = Retry.NEVER
    remedy = 'Your search string matched several plausible entities, and we\'d rather ask than guess. Re-run with "--pick" to choose interactively, or paste the exact Spotify URL.'


# ----------------------------------------------------------------------------
# MATCH: the part where naive tools hand you a nightcore cover
# ----------------------------------------------------------------------------

@register
class NoAcceptableMatch(SpotdlPlusError):
    code = 'MATCH_NONE'
    retry = Retry.NEVER
    remedy = 'We found candidates but none scored above the confidence floor, and skipping a track beats handing you the wrong recording. Run "spotdlp relink <track>" to see every candidate with its score breakdown and pick one yourself. Use "spotdlp relink <track> --url <url>" to force a source we never found.'


@register
class MatchAmbiguous(SpotdlPlusError):
    code = 'MATCH_AMBIGUOUS'
    retry = Retry.NEVER
    remedy = 'Two candidates scored within the tie threshold of each other. Rather than flip a coin, we stopped. "spotdlp relink <track>" lets you pick.'


# ----------------------------------------------------------------------------
# FETCH: actually getting the audio
# ----------------------------------------------------------------------------

@register
class DownloadFailed(SpotdlPlusError):
    code = 'FETCH_FAILED'
    retry = Retry.BACKOFF
    remedy = 'The audio stream didn\'t come down cleanly. Usually transient. The partial file was thrown away, nothing half-written ever lands in your library.'


@register
class SourceBlocked(SpotdlPlusError):
    code = 'FETCH_BLOCKED'
    retry = Retry.BACKOFF
    remedy = 'The source refused us. Bot detection, age gate, or region lock. The best fix is to lend it your browser, so set youtube_cookies_from_browser = "chrome" (or firefox/edge) in config.toml, or re-run "spotdlp setup". That clears most age-gates and bot checks. If a whole run is still hitting this then your IP is being rate-limited. Wait ~30 minutes, run "spotdlp resume --retry-failed", and drop concurrency to 1.'


@register
class CookiesUnreadable(SpotdlPlusError):
    code = 'COOKIES_UNREADABLE'
    retry = Retry.NEVER
    remedy = 'Couldn\'t read your browser\'s cookies. It is probably open and holding the file. Close it fully and retry, or use a different browser.'


# ----------------------------------------------------------------------------
# MEDIA / TAG / FS: the local half, where failures are at least honest
# ----------------------------------------------------------------------------

@register
class TranscodeFailed(SpotdlPlusError):
    code = 'MEDIA_TRANSCODE'
    retry = Retry.NEVER
    remedy = 'ffmpeg rejected the source stream. The raw download is kept under the cache dir so you can inspect it, and the error context carries the exact ffmpeg command line we ran.'


@register
class ToolHung(SpotdlPlusError):
    code = 'TOOL_HUNG'
    retry = Retry.BACKOFF
    remedy = 'ffmpeg/ffprobe took too long and the process got killed. We retry these automatically, so this has happened multiple times. Check if your CPU/disk are overworked currently and run "spotdlp resume --retry-failed" to pick back up.'


@register
class VerifyFailed(SpotdlPlusError):
    code = 'MEDIA_VERIFY'
    retry = Retry.NEVER
    remedy = 'The placed file didn\'t survive verification. Bad hash on read-back, zero size, or a duration that disagrees with the source by too much. We left it in place for inspection but did NOT record it as owned. Run "spotdlp relink <track>" to refetch from a different source.'


@register
class TagWriteFailed(SpotdlPlusError):
    code = 'TAG_WRITE'
    retry = Retry.NEVER
    remedy = 'The audio is fine. The metadata just wouldn\'t embed. The transcoded file is preserved in the cache, so once the cause is fixed, "spotdlp resume --retry-failed" re-runs the tag stage without refetching anything.'


@register
class NoSpace(SpotdlPlusError):
    code = 'FS_NO_SPACE'
    retry = Retry.PARK
    remedy = 'The output volume is full. The run is parked with its queue intact. Free some space and run "spotdlp resume", nothing needs redoing.'


@register
class PathUnwritable(SpotdlPlusError):
    code = 'FS_UNWRITABLE'
    retry = Retry.NEVER
    remedy = 'We can\'t write to the output directory. Check permissions. On Windows, also check that the path is under 260 chars or that long paths are enabled (yes, that limit is still around. Yes, it still bites).'


@register
class FsDriveLost(SpotdlPlusError):
    code = 'FS_DRIVE_LOST'
    retry = Retry.PARK
    remedy = 'The output location is gone. Typically an unplugged drive. Plug it back in and run "spotdlp resume".'


@register
class WouldOverwrite(SpotdlPlusError):
    code = 'FS_WOULD_OVERWRITE'
    retry = Retry.NEVER
    remedy = 'A file we do not own is already sitting where this track would go, so we stopped instead of replacing it. It is probably yours from before you pointed spotdl+ at this folder. Move or rename it and run "spotdlp resume", or point output_dir somewhere of our own.'


# ----------------------------------------------------------------------------
# STORE: the job database, where two copies of you collide
# ----------------------------------------------------------------------------

@register
class StoreBusy(SpotdlPlusError):
    code = 'STORE_BUSY'
    retry = Retry.BACKOFF
    remedy = 'The library database is open elsewhere. Either another spotdlp process is open or your antivirus is being weird. Close one and/or wait, then try again.'


@register
class StoreCorrupt(SpotdlPlusError):
    code = 'STORE_CORRUPT'
    retry = Retry.NEVER
    remedy = 'The run database got corrupted, usually a crash or antivirus mid-write. Your files are all fine on disk. Delete jobs.db to start clean, and "spotdlp doctor" shows you where it lives.'


@register
class ToolMissing(SpotdlPlusError):
    code = 'TOOL_MISSING'
    retry = Retry.NEVER
    remedy = 'A required external binary isn\'t on PATH. Run "spotdlp doctor" and it will name the binary and tell you how to install it. This should have been caught before the run started, so if it wasn\'t that is a bug worth filing.'


@register
class ToolQuarantined(SpotdlPlusError):
    code = 'TOOL_QUARANTINED'
    retry = Retry.NEVER
    remedy = 'Your antivirus grabbed one of our tools (normally ffmpeg) mid-run because we didn\'t pay Windows $5 billion for a license. Restore it from quarantine and add this folder to your AV\'s exceptions.'


@register
class Unexpected(SpotdlPlusError):
    code = 'ERR_UNEXPECTED'
    retry = Retry.NEVER
    remedy = 'An exception escaped a stage without being classified. That is our bug, not yours. The run kept going and the track is marked FAILED. The original traceback is in the error context, please file it.'


@register
class ToolOutdated(SpotdlPlusError):
    code = 'TOOL_OUTDATED'
    retry = Retry.NEVER
    remedy = 'An external binary is too old for what we ask of it. yt-dlp in particular rots fast. update it before assuming a fetch bug is ours.'

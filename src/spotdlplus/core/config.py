'''
config.py - config loading and precedence

Lowest to highest: defaults, user config.toml, project ./spotdl+.toml,
environment, then CLI flags. Every value remembers where it came from so
`doctor` can show you which file actually won.
'''

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigInvalid

AudioFormat = Literal['opus', 'mp3', 'flac', 'm4a', 'wav', 'alac']
_FORMATS = ('opus', 'mp3', 'flac', 'm4a', 'wav', 'alac')


@dataclass(frozen=True, slots=True)
class DeliverySpec:
    '''What a named delivery preset actually resolves to.'''
    audio_format: AudioFormat
    bitrate: str


#: Delivery presets. A plain-language way to pick the format a target device
#: wants, without anyone having to know a codec name. 'archive' means 'keep the
#: configured format' (opus by default), so the default path stays byte-for-
#: byte what it alwasy was. 'ipod' / 'universal' are the friendly shortcuts.
#: ipod-lossless (ALAC) lands once transcode learns the codec, see the roadmap.
DELIVERY_TARGETS: dict[str, DeliverySpec | None] = {
    'archive': None,
    'ipod': DeliverySpec('m4a', '256k'),           # AAC .m4a. What iTunes rips at
    'ipod-lossless': DeliverySpec('alac', '256k'),  # Apple Lossless .m4a (bitrate ignored)
    'universal': DeliverySpec('mp3', '320k'),      # plays literally anywhere
}

#: Browsers yt-dlp can borrow YouTube cookies from. Lower-cased on the way in.
SUPPORTED_BROWSERS = ('brave', 'chrome', 'chromium', 'edge', 'firefox',
                      'opera', 'safari', 'vivaldi', 'whale')

#: Every placeholder legal in an output template. `doctor --templates` reads
#: this. `track_tag` is the computed track prefix: '05' on a single-disc album,
#: '1-05' on a multi-disc one. So single-disc albums are not littered with disc
#: numbers they do not need.
TEMPLATE_FIELDS = frozenset({
    'artist', 'artists', 'album_artist', 'album', 'title', 'year',
    'track_no', 'disc_no', 'track_tag', 'isrc', 'ext',
})

DEFAULT_TEMPLATE = '{album_artist}/{album} ({year})/{track_tag} {title}.{ext}'


def _app_dir(kind: str) -> Path:
    '''Config/cache/state homes, computed the boring correct way per platform.'''
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return Path(root) / 'spotdlplus' / kind
    if sys.platform == 'darwin':
        base = {'config': '~/Library/Application Support', 'cache': '~/Library/Caches',
                'state': '~/Library/Application Support'}[kind]
        return Path(os.path.expanduser(base)) / 'spotdlplus'
    env = {'config': 'XDG_CONFIG_HOME', 'cache': 'XDG_CACHE_HOME', 'state': 'XDG_STATE_HOME'}[kind]
    default = {'config': '~/.config', 'cache': '~/.cache', 'state': '~/.local/state'}[kind]
    return Path(os.environ.get(env) or os.path.expanduser(default)) / 'spotdlplus'


@dataclass(frozen=True, slots=True)
class Config:
    # -- wehre things go
    output_dir: Path = field(default_factory=lambda: Path.home() / 'Music' / 'spotdl+')
    cache_dir: Path = field(default_factory=lambda: _app_dir('cache'))
    state_dir: Path = field(default_factory=lambda: _app_dir('state'))
    template: str = DEFAULT_TEMPLATE

    # -- audio
    audio_format: AudioFormat = 'opus'
    bitrate: str = '192k'
    #: How files get delivered. 'archive' (default) keeps audio_format as-is;
    #: 'ipod'/'universal' resolve to a device-friendly format+bitrate at load.
    delivery: str = 'archive'

    # -- selection
    profile: str = 'canonical'
    confidence_floor: float = 0.72

    # -- pacing. the default is deliberately slow.
    concurrency: int = 2
    max_attempts: int = 3
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    batch_size: int = 16
    lease_s: float = 300.0
    #: How many downloads YouTube can block in an unbroken row (no success in
    #: between) before we decide the IP is rate-limited and PARK the whole run,
    #: queue intact, instead of grinding every tarck to FAILED. 0 disables it.
    mass_block_streak: int = 10

    # -- youtube access. Browser cookies clear age-gates adn most bot walls. :
    # the single biggest lever for making a big-artist run finish.
    youtube_cookies_from_browser: str | None = None   # 'chrome' | 'firefox' | 'edge' | ...
    youtube_cookiefile: str | None = None             # or a cookies.txt path
    #: Cap download speed: '2M', '500K', or bytes/s. Unset means full speed.
    limit_rate: str | None = None

    # -- lyrics. Time-synced from LRCLIB (free, no key), embedded by default. :
    # 'synced' embeds LRC when it exists (plain as fallback), 'plain' prefers :
    # teh unsynced text, 'off' skips lyrics entirely.
    lyrics: str = 'synced'
    #: Also drop a .lrc file next to each track. Off by default because most players
    #: read the embedded copy. IPod-adjacent setups often want the sidecar.
    lyrics_sidecar: bool = False

    # -- export profiles: named device targets, so `export --profile ipod`
    #: means something. TOML tables: [export_profiles.<name>] with to/dest/
    #: bitrate keys. Built-in defaults live in the CLI. These override/extend.
    export_profiles: dict[str, dict[str, str]] = field(default_factory=dict)

    # -- credentials
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    #: free key from acoustid.org/new-application, powers acoustic verification
    acoustid_api_key: str | None = None

    # -- provenance: field name -> where the winning value came from
    provenance: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def db_path(self) -> Path:
        return self.state_dir / 'jobs.db'

    def validate(self) -> None:
        '''Fail here, at startup, rather than at track 3 of 900.'''
        if self.audio_format not in _FORMATS:
            raise ConfigInvalid(
                f'audio_format {self.audio_format!r} is not one of {_FORMATS}',
                context={'key': 'audio_format'},
            )
        if not 1 <= self.concurrency <= 16:
            raise ConfigInvalid(
                f'concurrency must be 1..16, got {self.concurrency}. '
                'Above 4 you are mostly buying rate-limit bans.',
                context={'key': 'concurrency'},
            )
        if not 0.0 <= self.confidence_floor <= 1.0:
            raise ConfigInvalid(
                f'confidence_floor must be 0.0..1.0, got {self.confidence_floor}',
                context={'key': 'confidence_floor'},
            )
        if self.max_attempts < 1:
            raise ConfigInvalid('max_attempts must be >= 1', context={'key': 'max_attempts'})
        if self.mass_block_streak < 0:
            raise ConfigInvalid('mass_block_streak must be >= 0 (0 disables it)',
                                context={'key': 'mass_block_streak'})
        from .models import PROFILES
        if self.profile not in PROFILES:
            raise ConfigInvalid(
                f'unknown profile {self.profile!r}. known: {sorted(PROFILES)}',
                context={'key': 'profile'},
            )
        if self.delivery not in DELIVERY_TARGETS:
            raise ConfigInvalid(
                f'unknown delivery {self.delivery!r}. known: {sorted(DELIVERY_TARGETS)}',
                context={'key': 'delivery'},
            )
        if self.lyrics not in ('synced', 'plain', 'off'):
            raise ConfigInvalid(
                f"lyrics {self.lyrics!r} is not one of 'synced', 'plain', 'off'",
                context={'key': 'lyrics'},
            )
        if self.limit_rate is not None and parse_rate(self.limit_rate) is None:
            raise ConfigInvalid(
                f'limit_rate {self.limit_rate!r} makes no sense, use something '
                "like '2M', '500K', or a plain bytes-per-second number",
                context={'key': 'limit_rate'},
            )
        if self.youtube_cookies_from_browser is not None \
                and self.youtube_cookies_from_browser.lower() not in SUPPORTED_BROWSERS:
            raise ConfigInvalid(
                f'youtube_cookies_from_browser {self.youtube_cookies_from_browser!r} '
                f'is not one of {SUPPORTED_BROWSERS}',
                context={'key': 'youtube_cookies_from_browser'},
            )
        _validate_template(self.template)

    def has_spotify_credentials(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    def redacted(self) -> dict[str, Any]:
        '''For `doctor` and for logs. Secrets never leave this method intact.'''
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in ('provenance', 'export_profiles'):
                continue
            v = getattr(self, f.name)
            if 'secret' in f.name or 'client_id' in f.name or 'api_key' in f.name:
                v = '<set>' if v else '<unset>'
            out[f.name] = {'value': str(v), 'from': self.provenance.get(f.name, 'default')}
        return out


def parse_rate(text: str) -> int | None:
    '''
    '2M' becomes 2097152, '500K' becomes 512000, '30000' stays as is, and junk
    returns None. The suffixes are binary because that's what yt-dlp means by them.
    '''
    s = text.strip().upper().removesuffix('/S')
    mult = 1
    if s.endswith(('K', 'M', 'G')):
        mult = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3}[s[-1]]
        s = s[:-1]
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n * mult) if n > 0 else None


def _validate_template(tpl: str) -> None:
    '''Catch a bad placeholder now, not after 900 successful downloads.'''
    from string import Formatter
    from .errors import OutputTemplateInvalid
    used = {name for _, name, _, _ in Formatter().parse(tpl) if name}
    unknown = {u.split('.')[0].split('[')[0] for u in used} - TEMPLATE_FIELDS
    if unknown:
        raise OutputTemplateInvalid(
            f'template uses unknown fields: {sorted(unknown)}',
            context={'template': tpl, 'known': sorted(TEMPLATE_FIELDS)},
        )


_PATH_KEYS = {'output_dir', 'cache_dir', 'state_dir'}
_INT_KEYS = {'concurrency', 'max_attempts', 'batch_size', 'mass_block_streak'}
_FLOAT_KEYS = {'confidence_floor', 'backoff_base_s', 'backoff_cap_s', 'lease_s'}
_BOOL_KEYS = {'lyrics_sidecar'}

_ENV_MAP = {
    'SPOTDLPLUS_OUTPUT_DIR': 'output_dir',
    'SPOTDLPLUS_CACHE_DIR': 'cache_dir',
    'SPOTDLPLUS_STATE_DIR': 'state_dir',
    'SPOTDLPLUS_FORMAT': 'audio_format',
    'SPOTDLPLUS_BITRATE': 'bitrate',
    'SPOTDLPLUS_DELIVERY': 'delivery',
    'SPOTDLPLUS_COOKIES_FROM_BROWSER': 'youtube_cookies_from_browser',
    'SPOTDLPLUS_COOKIEFILE': 'youtube_cookiefile',
    'SPOTDLPLUS_LIMIT_RATE': 'limit_rate',
    'SPOTDLPLUS_LYRICS': 'lyrics',
    'SPOTDLPLUS_LYRICS_SIDECAR': 'lyrics_sidecar',
    'SPOTDLPLUS_PROFILE': 'profile',
    'SPOTDLPLUS_CONCURRENCY': 'concurrency',
    'SPOTDLPLUS_TEMPLATE': 'template',
    'SPOTIFY_CLIENT_ID': 'spotify_client_id',
    'SPOTIFY_CLIENT_SECRET': 'spotify_client_secret',
    'ACOUSTID_API_KEY': 'acoustid_api_key',
}

_KNOWN = {f.name for f in fields(Config)} - {'provenance'}


def _coerce(key: str, raw: Any) -> Any:
    try:
        if key in _PATH_KEYS:
            return Path(str(raw)).expanduser()
        if key in _INT_KEYS:
            return int(raw)
        if key in _FLOAT_KEYS:
            return float(raw)
        if key in _BOOL_KEYS:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
        return raw
    except (TypeError, ValueError) as exc:
        raise ConfigInvalid(f'{key}: cannot read {raw!r} as the right type',
                            context={'key': key}, cause=exc) from exc


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigInvalid(f'{path} is not valid TOML: {exc}',
                            context={'path': str(path)}, cause=exc) from exc
    unknown = set(data) - _KNOWN
    if unknown:
        raise ConfigInvalid(
            f'{path} sets unknown keys: {sorted(unknown)}',
            context={'path': str(path), 'known': sorted(_KNOWN)},
        )
    return data


def load_config(
    *,
    project_dir: Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    user_config: Path | None = None,
) -> Config:
    '''
    Resolves the config once at startup and records which source won each key.
    Everything is injectable so the precedence chain is testable without touching
    the real filesystem.
    '''
    env = os.environ if env is None else env
    cfg = Config()
    prov: dict[str, str] = {}

    def apply(source: str, data: dict[str, Any]) -> None:
        nonlocal cfg
        clean = {}
        for k, v in data.items():
            if k not in _KNOWN or v is None:
                continue
            clean[k] = _coerce(k, v)
            prov[k] = source
        if clean:
            cfg = replace(cfg, **clean)

    user_path = user_config or (_app_dir('config') / 'config.toml')
    apply(f'user:{user_path}', _read_toml(user_path))

    proj = (project_dir or Path.cwd()) / 'spotdl+.toml'
    apply(f'project:{proj}', _read_toml(proj))

    apply('env', {dest: env[src] for src, dest in _ENV_MAP.items() if env.get(src)})
    apply('cli', overrides or {})

    # Expand the delivery preset LAST, so it reflects whichever layer won the
    # `delivery` key. A concrete target (ipod/universal) sets the format+bitrate;
    # 'archive' touches nothing, keeping the configured audio_format exactly.
    spec = DELIVERY_TARGETS.get(cfg.delivery)
    if spec is not None:
        cfg = replace(cfg, audio_format=spec.audio_format, bitrate=spec.bitrate)
        prov['audio_format'] = f'delivery:{cfg.delivery}'
        prov['bitrate'] = f'delivery:{cfg.delivery}'

    cfg = replace(cfg, provenance=prov)
    cfg.validate()
    return cfg

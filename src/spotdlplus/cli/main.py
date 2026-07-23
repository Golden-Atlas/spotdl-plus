'''
main.py - the command surface

Thin on purpose. A command wires up dependencies, subscribes the renderer,
calls one pipeline function, and turns what comes back into an exit code.
Anything smarter than that belongs below, wehre it can be tested without a
terminal.
'''

from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..core import errors as E
from ..core.config import Config, load_config
from ..core.events import EventBus
from ..core.models import PROFILES, EntityKind
from ..core.store import Store
from ..match.search import YtDlpSearcher
from ..net.http import HttpClient
from ..net.ytenv import probe_environment
from ..pipeline.resolve import ResolutionCandidate
from ..pipeline.run import FREE_SPACE_RESERVE_BYTES, resume_run, run_source
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.spotify import (
    SpotifyProvider,
    parse_spotify_ref,
    spotify_token_provider,
)
from ._win import harden_console
from .render import human_bytes, pick_renderer

# Must happen before any rich Console is constructed (below), or a legacy
# Windows console will have already decided it can't print a checkmark.
harden_console()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    # We render our own typed errors as a one-line code + remedy (see main()).
    # Typer's rich traceback would bury a plain 'bad config' under a full stack
    # dump. Which is exactly what it did to a friend's `resume`.
    pretty_exceptions_enable=False,
    # -h works on the app and every subcommand: click inherits
    # help_option_names down through child contexts.
    context_settings={'help_option_names': ['-h', '-help', '--help']},
    rich_markup_mode='rich',
    help=(
        'The Music Archive Pipeline\n\n'
        'Point it at anything on Spotify, an artist, an album, a playlist, a track, '
        'or just a name, and it builds an organized, fully tagged, verified library. '
        'Runs are resumable, matches are explainable, and every failure carries its '
        'own remedy. If something breaks, it can tell you why right away.\n\n'
        '[bold]Quick start:[/bold]\n\n'
        '  spotdlp doctor                     check that everything is ready\n'
        '  spotdlp get "artist:Duster"        download a whole discography\n'
        '  spotdlp get <spotify url>          or anything a URL points at\n'
        '  spotdlp audit --fix                prove the library is healthy'
    ),
    epilog=(
        '[bold]Typical workflows:[/bold]\n\n'
        '  Price before committing:   spotdlp plan <source>  ->  spotdlp get <source> -y\n'
        '  Power went out mid-run:    spotdlp resume\n'
        '  Give failures another go:  spotdlp resume --retry-failed\n'
        '  "Why did track X fail?":   spotdlp status --errors, then spotdlp explain <CODE>\n'
        '  "That match is wrong":     spotdlp relink <track title>\n\n'
        'Config lives at %LOCALAPPDATA%\\spotdlplus\\config\\config.toml. '
        '"doctor" shows every resolved value and where it came from.'
    ),
)
console = Console(highlight=False)

#: rich_help_panel names. Commands group by what you are trying to do, not by
#: alphabet.
#: A small public playlist kept alive for exactly one job: proving "doctor
#: --network" can read playlist ITEMS, which is a separate permission from
#: reading an album and the one that actually catches a misconfigured Spotify
#: app. Hardcoded on purpose. Searching for a playlist to test with returns
#: Spotify's own editorial ones, and those are unreadable through the Web API
#: for everybody now, so a search-driven check fails on a healthy machine.
#: If this ever stops resolving, that is about this playlist and not about you,
#: which is why only a refusal counts as a failure.
REFERENCE_PLAYLIST = '6c6QIG9zKK121v5XWamwdv'

PANEL_GET = 'Getting music'
PANEL_TRUST = 'Trust & repair'
PANEL_DIAG = 'When something needs explaining'


def _fail(err: E.SpotdlPlusError) -> 'typer.Exit':
    console.print(f'[bold red][{err.code}][/] {err.message}')
    console.print(f'[grey58]{err.remedy}[/]')
    return typer.Exit(1)


#: The one command that clears each kind of failure. A run that ends with
#: problems should end with a to-do list bucketed by cause. Not 284 lines you
#: have to read one at a time.
_FIX_HINT = {
    'MATCH_NONE': 'spotdlp relink --queue',
    'MATCH_AMBIGUOUS': 'spotdlp relink --queue',
    'FETCH_FAILED': 'spotdlp resume --retry-failed',
    'FETCH_BLOCKED': 'likely rate-limited. Wait a bit, then spotdlp resume --retry-failed',
    'MEDIA_TRANSCODE': 'spotdlp resume --retry-failed',
    'MEDIA_VERIFY': 'spotdlp relink <track>',
    'TAG_WRITE': 'spotdlp resume --retry-failed',
}


def _failure_digest(store: Store, run_id: str) -> None:
    '''Group a run's failures by cause, each with the command taht clears it.'''
    from collections import Counter
    fails = store.failures(run_id)
    if not fails:
        return
    by_code = Counter(f.get('code', 'ERR_UNKNOWN') for f in fails)
    console.print(f'\n[bold]{len(fails)} track(s) need attention:[/]')
    for code, n in by_code.most_common():
        hint = _FIX_HINT.get(code, f'spotdlp explain {code}')
        console.print(f'  [bold red]{n:>4}[/] {code}  [grey58]->[/] {hint}')


#: The one option shared by every command that touches a library. Declared once
#: so the help text cannot drift between commands.
OutOption = typer.Option(
    None, '--out', '-o',
    help='use PATH as a self-contained vault: files land there AND its own '
         'database lives at PATH/.spotdlplus. Its own ownership, runs, and '
         'queue, nothing shared. Omit to use the configured library.',
)

#: Pick the file format for a device, in plain words. 'ipod' = AAC .m4a (what
#: iTunes/Apple Music uses), 'universal' = MP3, 'archive' = keep yoru
#: configured format. Omit to use whatever's set in config (opus by default).
ToOption = typer.Option(
    None, '--to',
    help="save in a device's preferred format: 'ipod' (AAC .m4a), "
         "'ipod-lossless' (Apple Lossless .m4a), 'universal' (MP3), or "
         "'archive' (your configured format).",
)


def _load(out: Path | None = None, **overrides) -> Config:
    '''
    Resolves config, treating `-o` as a vault. output_dir and state_dir move
    together.

    Pointing files somewhere new while still sharing the global ownership database
    was a footgun. Every re-download got skipped as owned while the new folder sat
    empty. A vault is its own little world now.
    '''
    clean = {k: v for k, v in overrides.items() if v is not None}
    if out is not None:
        clean['output_dir'] = str(out)
        clean['state_dir'] = str(Path(out) / '.spotdlplus')
    return load_config(overrides=clean)


def _wire(cfg: Config) -> tuple[HttpClient, SpotifyProvider, MusicBrainzProvider, Store]:
    if not cfg.has_spotify_credentials():
        # First run in a real terminal? Walk them through it instead of jsut
        # failing. This is the whole friend-can-use-it story. Scripts and CI
        # have no tty, so they still get the plain typed error below.
        if sys.stdin.isatty():
            from .setup import run_setup_wizard
            if run_setup_wizard(console, reason='No Spotify credentials found yet.'):
                fresh = load_config()
                cfg = replace(cfg,
                              spotify_client_id=fresh.spotify_client_id,
                              spotify_client_secret=fresh.spotify_client_secret)
        if not cfg.has_spotify_credentials():
            raise E.CredentialsMissing('no Spotify credentials configured')
    # Subprocess noise (deno PO-token warnings) goes to a log, not through the
    # middle of the live display. Real errors still reach the terminal.
    from ..core.config import parse_rate
    from ..net.ytenv import (
        cookie_source_readable,
        install_noise_filter,
        set_cookie_source,
        set_rate_limit,
    )
    install_noise_filter(cfg.state_dir / 'subprocess-noise.log')
    # Borrow browser cookies if configured. Clears age-gates + most bot walls.
    set_cookie_source(browser=cfg.youtube_cookies_from_browser,
                      cookiefile=cfg.youtube_cookiefile)
    if cfg.limit_rate:
        set_rate_limit(parse_rate(cfg.limit_rate))
    # ...but cookies are an enhancement, never a requirement. If the store
    # can't be read (browser open and locking it, or Chrome's app-bound
    # encryption), say so once and run without them. Far better than failing
    # every single downlaod with the same COOKIES_UNREADABLE line.
    if cfg.youtube_cookies_from_browser or cfg.youtube_cookiefile:
        ok, detail = cookie_source_readable()
        if not ok:
            src = cfg.youtube_cookies_from_browser or 'the cookie file'
            console.print(
                f'[yellow]⚠ couldn\'t read {src} cookies ({detail}). Continuing '
                f'without them. Downloads still work, just with less protection '
                f'against bot walls. Close the browser fully, or unset '
                f'youtube_cookies_from_browser, to silence this.[/]')
            set_cookie_source(browser=None, cookiefile=None)
    http = HttpClient(version=__version__)
    auth = spotify_token_provider(http, cfg.spotify_client_id, cfg.spotify_client_secret)
    return http, SpotifyProvider(http, auth), MusicBrainzProvider(http), Store(cfg.db_path)


def _interactive_pick(cands: list[ResolutionCandidate]) -> int | None:
    console.print('[yellow]that could mean a few things:[/]')
    for i, c in enumerate(cands[:5], 1):
        console.print(f'  {i}. {c.label}  [grey58]match {c.similarity:.0%}[/]')
    raw = typer.prompt('which one? (number, or q)', default='1')
    if raw.strip().lower().startswith('q'):
        return None
    try:
        n = int(raw) - 1
        return n if 0 <= n < len(cands[:5]) else None
    except ValueError:
        return None


@app.command(rich_help_panel=PANEL_GET)
def get(
    source: str = typer.Argument(..., help='spotify URL/URI, or a search like "artist:Duster"'),
    profile: str = typer.Option(None, '--profile', '-p', help=f'one of {sorted(PROFILES)}'),
    yes: bool = typer.Option(False, '--yes', '-y', help='accept the plan without asking'),
    lyrics_omit: bool = typer.Option(False, '--lyrics-omit',
                                     help='skip lyrics entirely for this run'),
    lyrics_plain: bool = typer.Option(False, '--lyrics-plain',
                                      help='prefer plain text over time-synced'),
    lyrics_sidecar: bool = typer.Option(False, '--lyrics-sidecar',
                                        help='also write a .lrc file next to each track'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
    out: Path = OutOption,
    to: str = ToOption,
) -> None:
    '''Resolve a source, show the plan, and download it into the library.'''
    lyric_mode = 'off' if lyrics_omit else ('plain' if lyrics_plain else None)
    cfg = _load(out, profile=profile, delivery=to, lyrics=lyric_mode,
                lyrics_sidecar=lyrics_sidecar or None)
    bus = EventBus()
    bus.subscribe(pick_renderer(verbose=verbose))

    def gate(plan) -> bool:
        if yes:
            return True
        need = plan.matched - plan.already_have
        return typer.confirm(
            f'download {need} tracks (~{human_bytes(plan.est_bytes_total)})?', default=True)

    try:
        http, sp, mb, store = _wire(cfg)
    except E.SpotdlPlusError as err:
        raise _fail(err)
    try:
        report = run_source(
            source, config=cfg, store=store, bus=bus, sp=sp, mb=mb,
            searcher=YtDlpSearcher(), http=http,
            pick=_interactive_pick if sys.stdin.isatty() else None,
            gate=gate,
        )
    except E.SpotdlPlusError as err:
        raise _fail(err)
    finally:
        store.close()
        http.close()

    if report.parked:
        raise typer.Exit(3)
    if report.stats.failed:
        digest_store = Store(cfg.db_path)
        try:
            _failure_digest(digest_store, report.run_id)
        finally:
            digest_store.close()
        console.print('[grey58]full detail: spotdlp status --errors[/]')
        raise typer.Exit(2)


@app.command(rich_help_panel=PANEL_GET)
def plan(
    source: str = typer.Argument(...),
    profile: str = typer.Option(None, '--profile', '-p'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
    out: Path = OutOption,
    to: str = ToOption,
) -> None:
    '''Everything except the download: resolve, filter, dedupe, match, price it.'''
    cfg = _load(out, profile=profile, delivery=to)
    bus = EventBus()
    bus.subscribe(pick_renderer(verbose=verbose))
    try:
        http, sp, mb, store = _wire(cfg)
        run_source(source, config=cfg, store=store, bus=bus, sp=sp, mb=mb,
                   searcher=YtDlpSearcher(), http=http,
                   gate=lambda _plan: False)   # look, don't touch
    except E.SpotdlPlusError as err:
        raise _fail(err)
    finally:
        try:
            store.close()
            http.close()
        except NameError:
            pass
    console.print('[grey58]nothing downloaded. "spotdlp get" when ready.[/]')


def _prune_gone(store: Store, cfg: Config, *, gone: set[str],
                except_key: str) -> tuple[int, int]:
    '''
    Deletes the files for recordings that left the source. This is a hard delete
    and it names every file as it goes. Anything another synced source still wants
    gets spared. Returns (deleted, spared).
    '''

    deleted = spared = 0
    root = Path(cfg.output_dir).resolve()
    for identity in sorted(gone):
        if store.identity_in_other_snapshots(identity, except_key=except_key):
            spared += 1
            continue
        owned = store.own(identity)
        if owned is None:
            continue
        path = Path(owned['final_path'])
        if path.is_file():
            console.print(f'  [bad]−[/] {path}')
            path.unlink()
            # fold up newly empty album/artist folders, but never past teh root
            parent = path.parent
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        store.revoke_ownership(identity)
        deleted += 1
    return deleted, spared


@app.command(rich_help_panel=PANEL_GET)
def sync(
    source: str = typer.Argument(..., help='the same things "get" takes, a spotify '
                                           'URL, or a search like "artist:Duster"'),
    diff: bool = typer.Option(False, '--diff',
                              help='just show what changed since last time, '
                                   'nothing downloads, nothing deletes'),
    prune: bool = typer.Option(False, '--prune',
                               help='DELETE files for tracks that left the source '
                                    '(each one is named as it goes). For playlists.'),
    yes: bool = typer.Option(False, '--yes', '-y', help='accept without asking'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
    profile: str = typer.Option(None, '--profile', '-p', help=f'one of {sorted(PROFILES)}'),
    out: Path = OutOption,
    to: str = ToOption,
) -> None:
    '''
    Bring a source up to date: fetch what's new, and (if you ask) drop what's gone.

    Point it at anything you've gotten before. New tracks download, owned tracks
    skip, and what the source contained is remembered so next time knows what
    changed. `--diff` previews it and `--prune` clears out what left a playlist.
    '''
    from ..pipeline.resolve import resolve
    from ..pipeline.run import source_key

    cfg = _load(out, profile=profile, delivery=to)
    bus = EventBus()
    bus.subscribe(pick_renderer(verbose=verbose))
    try:
        http, sp, mb, store = _wire(cfg)
    except E.SpotdlPlusError as err:
        raise _fail(err)
    try:
        resolution = resolve(source, sp,
                             pick=_interactive_pick if sys.stdin.isatty() else None)
        key = source_key(resolution)
        prev = store.get_snapshot(key)

        def gate(plan) -> bool:
            if diff:
                return False        # look, don't touch
            if yes:
                return True
            need = plan.matched - plan.already_have
            return typer.confirm(
                f'sync {need} new track(s) (~{human_bytes(plan.est_bytes_total)})?',
                default=True)

        report = run_source(source, config=cfg, store=store, bus=bus, sp=sp,
                            mb=mb, searcher=YtDlpSearcher(), http=http, gate=gate)

        current = report.identities
        gone = (prev[1] - current) if prev else set()
        # matched can dip below already_have when everything's owned. a
        # '-1 new' once printed here and looked exactly as silly as it sounds
        new_count = max(0, report.plan.matched - report.plan.already_have)

        console.print()
        if prev is None:
            console.print('[grey58]first sync of this source. From here on we '
                          'remember what it contains, so next time knows what '
                          'changed.[/]')
        if diff:
            console.print(f'[accent]diff[/]  +{new_count} new · '
                          f'{report.plan.already_have} unchanged · {len(gone)} gone')
            if gone:
                console.print(f'  [grey58]"spotdlp sync --prune" would delete the '
                              f'{len(gone)} that left[/]')
            return
        if report.parked:
            raise typer.Exit(3)

        if prune and gone:
            deleted, spared = _prune_gone(store, cfg, gone=gone, except_key=key)
            bits = f'[bad]{deleted} deleted[/]'
            if spared:
                bits += f' · {spared} spared [grey58](still wanted by another synced source)[/]'
            console.print(f'[accent]pruned[/]  {bits}')
        elif gone:
            console.print(f'[grey58]{len(gone)} track(s) left this source since last '
                          f'time. "sync --prune" deletes their files if you want '
                          f'that.[/]')
    except E.SpotdlPlusError as err:
        raise _fail(err)
    finally:
        store.close()
        http.close()


@app.command(rich_help_panel=PANEL_GET)
def resume(
    retry_failed: bool = typer.Option(False, '--retry-failed',
                                      help='give FAILED tracks another chance too'),
    wait: bool = typer.Option(False, '--wait',
                              help='if the run parks (rate-limited), sit out the '
                                   'cooldown and resume by itself, repeat until '
                                   'it actually finishes'),
    wait_minutes: int = typer.Option(30, '--wait-minutes',
                                     help='how long each sit-out lasts'),
    verbose: bool = typer.Option(False, '--verbose', '-v'),
    out: Path = OutOption,
) -> None:
    '''
    Pick the most recent parked or interrupted run up where it stopped.

    `--wait` babysits it. Park, cool down, resume, over and over, so a big
    overnight run finishes without you sitting there. Ctrl+C still stops it.
    '''
    import time as _time

    cfg = _load(out)
    bus = EventBus()
    bus.subscribe(pick_renderer(verbose=verbose))
    try:
        http, sp, mb, store = _wire(cfg)
    except E.SpotdlPlusError as err:
        raise _fail(err)
    try:
        run_id = store.latest_run()
        if run_id is None and retry_failed:
            # a run that completed WITH failures is 'finished'. The flag's
            # whole purpose is reaching back into exactly those
            run_id = store.latest_run_with_failures()
        if run_id is None:
            msg = ('nothing to retry. No run has failed tracks.'
                   if retry_failed else
                   'nothing to resume. No active or parked runs.')
            console.print(msg)
            raise typer.Exit(0)

        #: --wait gets 6 cycles (~3h at the default 30min) before we admit the
        #: wall isn't moving tonight. Unbounded would run until the heat death
        #: of teh universe, which helps nobody's electric bill.
        max_cycles = 6 if wait else 1
        for cycle in range(1, max_cycles + 1):
            stats = resume_run(run_id, config=cfg, store=store, bus=bus,
                               searcher=YtDlpSearcher(), http=http,
                               retry_failed=retry_failed and cycle == 1)
            if not stats.parked:
                return
            if not wait or cycle == max_cycles:
                if wait:
                    console.print(
                        f'[yellow]still parked after {max_cycles} tries, the wall '
                        f'is winning tonight. The queue is intact. Try again later '
                        f'with "spotdlp resume --wait".[/]')
                raise typer.Exit(3)
            # sit out the cooldown, visibly, one minute at a time. Ctrl+C works
            # the whole way through because it's jsut a sleep, not a lock.
            for remaining in range(wait_minutes, 0, -1):
                console.print(f'  [grey58]cooling down. Resuming in ~{remaining} min '
                              f'(cycle {cycle}/{max_cycles - 1})[/]', end='\r')
                _time.sleep(60)
            console.print()
    finally:
        store.close()
        http.close()


@app.command(rich_help_panel=PANEL_DIAG)
def status(
    errors: bool = typer.Option(False, '--errors', help='show failures with remedies'),
    out: Path = OutOption,
) -> None:
    '''Where the most recent run stands.'''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        run_id = store.latest_run(statuses=('active', 'parked', 'finished'))
        if run_id is None:
            console.print('no runs yet.')
            return
        counts = store.counts(run_id)
        plan_ = store.plan_summary(run_id)
        table = Table(title=f'run {run_id}', show_header=False, border_style='grey58')
        for state in ('done', 'discovered', 'enriched', 'matched', 'fetched',
                      'transcoded', 'tagged', 'placed', 'skipped', 'failed'):
            if counts.get(state):
                table.add_row(state, str(counts[state]))
        table.add_row('est. size', human_bytes(plan_.est_bytes_total))
        console.print(table)

        if errors:
            _failure_digest(store, run_id)
            if store.failures(run_id):
                console.print()
            for f in store.failures(run_id):
                console.print(f'[bold red][{f["code"]}][/] {f["artist"]}, {f["title"]}')
                console.print(f'  {f.get("message", "")}')
                console.print(f'  [grey58]{f.get("remedy", "")}[/]')
    finally:
        store.close()


def _mmss(seconds: int) -> str:
    '''364 -> "6:04". A duration a human reads at a glance.'''
    return f'{seconds // 60}:{seconds % 60:02d}'


def _n(count: int, word: str, plural: str | None = None) -> str:
    '''"1 track" / "2 tracks". Small, but "1 tracks" reads like a bug because it is one.'''
    return f'{count} {word if count == 1 else (plural or word + "s")}'


def _candidate_details(cands: list, want_ms: int | None) -> None:
    '''
    Prints every candidate fully unpacked instead of making you pick blind. Title,
    channel, length against the length we wanted, size, the score with its
    per-signal breakdown, and the URL.
    '''
    import json
    want_s = (want_ms or 0) // 1000
    console.print()
    for i, c in enumerate(cands, 1):
        c = dict(c)   # sqlite3.Row has no .get(). a plain dict does
        if c.get('fresh'):
            mark = '[accent]fresh find[/]'
        elif c['chosen']:
            mark = '[green]✓ our pick[/]'
        else:
            mark = '[grey58]passed over[/]'
        score = f'{c["score"]:.2f}' if c['score'] is not None else '—'
        console.print(f'  [bold]{i}[/]  score [bold]{score}[/]   {mark}')
        console.print(f'      [grey58]title  [/] {c["title"] or "—"}')
        console.print(f'      [grey58]channel[/] {c["uploader"] or "—"}')

        dur_s = (c['duration_ms'] or 0) // 1000
        if want_s and dur_s:
            d = dur_s - want_s
            sign = '+' if d >= 0 else '−'
            off = '   [yellow](length is off)[/]' if abs(d) > 3 else ''
            console.print(f'      [grey58]length [/] {_mmss(dur_s)}   '
                          f'[grey58](wanted {_mmss(want_s)}, {sign}{_mmss(abs(d))})[/]{off}')
        else:
            console.print(f'      [grey58]length [/] {_mmss(dur_s)}')

        if c['size_bytes']:
            console.print(f'      [grey58]size   [/] {human_bytes(c["size_bytes"])}')

        try:
            bd = json.loads(c['breakdown_json']) if c['breakdown_json'] else {}
        except (ValueError, TypeError):
            bd = {}
        if bd:
            parts = ' · '.join(
                f'{k} {v:+.2f}' if isinstance(v, (int, float)) else f'{k} {v}'
                for k, v in bd.items())
            console.print(f'      [grey58]scoring[/] {parts}')

        if c.get('views'):
            console.print(f'      [grey58]views  [/] {c["views"]:,}')
        if not c.get('fresh') and not c['chosen'] and c['rejected_why']:
            console.print(f'      [grey58]why not[/] {c["rejected_why"]}')
        console.print(f'      [grey58]link   [/] [blue]{c["url"]}[/]')
        console.print()


def _open_preview(url: str) -> None:
    '''Open a candidate in the browser so you can LISTEN before you commit.'''
    import webbrowser
    try:
        webbrowser.open(url)
        console.print(f'   [grey58]opened {url}. Have a listen, then come back.[/]')
    except Exception:  # noqa: BLE001  # A headless box just gets the link
        console.print(f'   [grey58]couldn\'t open a browser. The link is {url}[/]')


def _fresh_search(searcher, default_query: str, want_ms: int | None) -> tuple | None:
    '''
    The re-search submenu. Type a search, get the top 5 with the same detail the
    scoreboard shows, then pick one, preview it, search again, or back out.
    '''
    query = default_query
    while True:
        raw = typer.prompt('   search youtube for (b = back)',
                           default=query, show_default=True).strip()
        if not raw or raw.lower() == 'b':
            return None
        query = raw
        try:
            found = searcher.search(query)[:5]
        except E.SpotdlPlusError as err:
            console.print(f'   [yellow]search failed ([{err.code}]), '
                          f'try again or back out.[/]')
            continue
        if not found:
            console.print('   [grey58]youtube returned nothing for that. '
                          'Different words, maybe.[/]')
            continue
        rows = [{'chosen': 0, 'fresh': True, 'score': None, 'title': c.title,
                 'uploader': c.uploader, 'duration_ms': c.duration_ms,
                 'size_bytes': None, 'breakdown_json': None,
                 'rejected_why': None, 'views': c.view_count,
                 'url': c.url} for c in found]
        _candidate_details(rows, want_ms)
        while True:
            sub = typer.prompt('   pick a number (p<n> preview · r new search · b back)',
                               default='b', show_default=False).strip().lower()
            if not sub or sub == 'b':
                return None
            if sub == 'r':
                break   # outer loop re-prompts the query
            if sub.startswith('p') and sub[1:].strip().isdigit():
                idx = int(sub[1:].strip()) - 1
                if 0 <= idx < len(rows):
                    _open_preview(rows[idx]['url'])
                else:
                    console.print('   [grey58]that number is out of range.[/]')
                continue
            if sub.isdigit():
                idx = int(sub) - 1
                if 0 <= idx < len(rows):
                    return ('pick', rows[idx]['url'])
                console.print('   [grey58]that number is out of range.[/]')
                continue
            console.print('   [grey58]not one of the options.[/]')


def _decide_one(cands: list, want_ms: int | None, *,
                searcher=None, default_query: str = '') -> tuple:
    '''
    The per-track menu. Details, the URL prompt, a fresh youtube search, and
    previews all back out with `b` so nothing is a dead end. Returns ('pick', url),
    ('skip',), ('abandon',), or ('quit',).
    '''
    n = len(cands)
    while True:
        opts = []
        if n:
            opts += ['[accent]c[/] see details', f'[accent]1-{n}[/] pick',
                     '[accent]p[/] preview']
        if searcher is not None:
            opts += ['[accent]r[/] search youtube yourself']
        opts += ['[accent]u[/] paste a URL', '[accent]s[/] skip',
                 '[accent]x[/] give up', '[accent]q[/] quit']
        console.print('   ' + '   '.join(opts))
        choice = typer.prompt('   choice', default='s', show_default=False).strip().lower()

        if choice in ('s', ''):
            return ('skip',)
        if choice == 'q':
            return ('quit',)
        if choice == 'x':
            return ('abandon',)
        if choice == 'u':
            url = typer.prompt('   paste the source URL (b = back)',
                               default='b', show_default=False).strip()
            if not url or url.lower() == 'b':
                continue
            return ('pick', url)
        if choice == 'r' and searcher is not None:
            picked = _fresh_search(searcher, default_query, want_ms)
            if picked is not None:
                return picked
            continue
        if (choice == 'p' or (choice.startswith('p') and choice[1:].strip().isdigit())) and n:
            num = choice[1:].strip()
            if not num:
                num = typer.prompt('   preview which number? (b = back)',
                                   default='b', show_default=False).strip().lower()
                if not num or num == 'b':
                    continue
            if num.isdigit() and 0 <= int(num) - 1 < n:
                _open_preview(cands[int(num) - 1]['url'])
            else:
                console.print('   [grey58]that number is out of range.[/]')
            continue
        if choice == 'c' and n:
            _candidate_details(cands, want_ms)
            pick = typer.prompt('   pick a number (b = back)',
                                default='b', show_default=False).strip().lower()
            if not pick or pick == 'b':
                continue
            choice = pick   # fall through to the number handler
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < n:
                return ('pick', cands[idx]['url'])
            console.print('   [grey58]that number is out of range.[/]')
            continue
        if n == 0 and choice in ('c', 'p'):
            # an older refusal with no recorded scoreboard, nothing to open
            console.print('   [grey58]no recorded candidates on this one, '
                          '"r" searches youtube fresh, "u" takes a URL.[/]')
            continue
        console.print('   [grey58]not one of the options.[/]')


def _auto_queue(store: Store, cfg: Config, http: HttpClient, *,
                threshold: float, narrow: str | None = None) -> None:
    '''
    The no-sitting mode. Every queued track whose best candidate clears the
    threshold gets relinked to it, and what's left over is the genuinely ambiguous
    pile.
    '''
    queue = store.match_queue(narrow)
    if not queue:
        console.print('[green]the match queue is empty. Nothing needs you right now.[/]')
        return

    affected_runs: set[str] = set()
    accepted = left = 0
    for row in queue:
        cands = store.candidates(row['id'])
        best = max((c for c in cands if c['score'] is not None),
                   key=lambda c: c['score'], default=None)
        if best is not None and best['score'] >= threshold:
            store.force_relink(row['id'], best['url'])
            affected_runs.add(row['run_id'])
            accepted += 1
        else:
            left += 1

    console.print(f'[accent]{accepted} auto-accepted[/] at ≥{threshold:.2f} · '
                  f'{left} left for a human '
                  f'[grey58]("spotdlp relink --queue" when you have a minute)[/]')
    if affected_runs and typer.confirm(f'download the {accepted} now?', default=True):
        bus = EventBus()
        bus.subscribe(pick_renderer())
        for rid in sorted(affected_runs):
            resume_run(rid, config=cfg, store=store, bus=bus,
                       searcher=YtDlpSearcher(), http=http)


def _triage_queue(store: Store, cfg: Config, http: HttpClient,
                  narrow: str | None = None) -> None:
    '''
    The one-sitting mode. Every track waiting on a verdict, one keypress each.
    Decisions queue up and the downloads all happen at the end so the keyboard
    never waits on the network.
    '''
    if not sys.stdin.isatty():
        console.print('[yellow]relink --queue is interactive[/]. It asks you to judge each '
                      'track. Run it directly in PowerShell or Windows Terminal.')
        raise typer.Exit(1)

    queue = store.match_queue(narrow)
    if not queue:
        scope = f' matching {narrow!r}' if narrow else ''
        console.print(f'[green]the match queue{scope} is empty, '
                      'nothing needs you right now.[/]')
        return

    scope = f' matching [accent]{narrow}[/]' if narrow else ''
    console.print(f'[bold]{len(queue)}[/] tracks are waiting on a verdict{scope}.\n'
                  'The menu under each one shows exactly what applies: '
                  '[accent]c[/] opens the full candidate detail and [accent]p[/] '
                  'previews one in your browser (when we have receipts), '
                  '[accent]r[/] searches youtube yourself, a number picks straight '
                  'away. Your decisions are kept even if you [accent]q[/]uit partway.\n')

    searcher = YtDlpSearcher(results=5)
    affected_runs: set[str] = set()
    decided = skipped = abandoned = 0
    for n, row in enumerate(queue, 1):
        cands = store.candidates(row['id'])
        want_ms = row['duration_ms']
        console.print(f'[bold][{n}/{len(queue)}][/] {row["artist"]}, {row["title"]}')
        console.print(f'   [grey58]{row["album_title"] or ""} · want ~'
                      f'{_mmss((want_ms or 0) // 1000)} · {row["error_code"]} · '
                      f'{len(cands)} candidate(s)[/]')

        verdict = _decide_one(cands, want_ms, searcher=searcher,
                              default_query=f'{row["artist"]} {row["title"]}')
        if verdict[0] == 'quit':
            break
        if verdict[0] == 'skip':
            skipped += 1
            console.print()
            continue
        if verdict[0] == 'abandon':
            store.mark_skipped(row['id'], 'pruned')
            abandoned += 1
            console.print('[grey58]  abandoned.[/]\n')
            continue

        store.force_relink(row['id'], verdict[1])
        affected_runs.add(row['run_id'])
        decided += 1
        console.print('[green]  queued.[/]\n')

    console.print(f'\n[accent]{decided} decided[/] · {skipped} skipped · '
                  f'{abandoned} abandoned')
    if affected_runs and typer.confirm(f'download the {decided} now?', default=True):
        bus = EventBus()
        bus.subscribe(pick_renderer())
        for rid in sorted(affected_runs):
            resume_run(rid, config=cfg, store=store, bus=bus,
                       searcher=YtDlpSearcher(), http=http)


@app.command(rich_help_panel=PANEL_TRUST)
def relink(
    query: str = typer.Argument(None, help='part of a title or artist, or a track id'),
    url: str = typer.Option(None, '--url', help='force a specific source URL'),
    yes: bool = typer.Option(False, '--yes', '-y', help='take the first search hit'),
    now: bool = typer.Option(True, '--now/--later', help='download immediately after'),
    queue: bool = typer.Option(False, '--queue',
                               help='triage mode: walk EVERY track awaiting a match '
                                    'verdict in one sitting, one keypress each'),
    auto: float = typer.Option(None, '--auto',
                               help='no sitting at all: auto-accept every queued track '
                                    'whose best candidate scores at least THIS (e.g. '
                                    '0.65), leaving only the true judgment calls'),
    out: Path = OutOption,
) -> None:
    '''
    Override a match by hand: see every candidate we considered, pick one, or force a URL.

    The scoreboard shows the winner and every loser with the reason it lost, and
    the library entry gets revoked so the new choice actually lands. `--queue`
    walks the whole backlog and `--auto 0.65` clears the easy ones for you.
    '''
    if auto is not None:
        if not 0.0 < auto <= 1.0:
            console.print('[yellow]--auto wants a decimal between 0 and 1, '
                          'like 0.65.[/]')
            raise typer.Exit(1)
        cfg = _load(out)
        try:
            http, sp, mb, store = _wire(cfg)
        except E.SpotdlPlusError as err:
            raise _fail(err)
        try:
            _auto_queue(store, cfg, http, threshold=auto, narrow=query)
        finally:
            store.close()
            http.close()
        return
    if queue:
        cfg = _load(out)
        try:
            http, sp, mb, store = _wire(cfg)
        except E.SpotdlPlusError as err:
            raise _fail(err)
        try:
            # the positional narrows teh queue: `relink --queue pierce`
            _triage_queue(store, cfg, http, narrow=query)
        finally:
            store.close()
            http.close()
        return
    if query is None:
        raise _fail(E.ConfigInvalid(
            'relink needs a track to work on: "relink <title>" for one, '
            'or "relink --queue" for the whole backlog'))
    cfg = _load(out)
    try:
        http, sp, mb, store = _wire(cfg)
    except E.SpotdlPlusError as err:
        raise _fail(err)
    try:
        hits = store.find_tracks(query)
        if not hits:
            console.print(f'nothing in any run matches {query!r}.')
            raise typer.Exit(1)

        if len(hits) > 1 and not yes:
            for i, h in enumerate(hits, 1):
                console.print(
                    f'  {i}. {h["artist"]}, {h["title"]}  '
                    f'[grey58]{h["album_title"] or ""} · {h["state"]}[/]')
            n = int(typer.prompt('which track?', default='1')) - 1
            row = hits[max(0, min(n, len(hits) - 1))]
        else:
            row = hits[0]

        chosen_url = url
        if chosen_url is None:
            cands = store.candidates(row['id'])
            verdict = _decide_one(
                cands, row['duration_ms'], searcher=YtDlpSearcher(results=5),
                default_query=f'{row["artist"]} {row["title"]}')
            if verdict[0] != 'pick':
                console.print('[grey58]no change made.[/]')
                raise typer.Exit(0)
            chosen_url = verdict[1]

        store.force_relink(row['id'], chosen_url)
        console.print(f'[green]relinked[/] {row["title"]} -> {chosen_url}')

        if now:
            bus = EventBus()
            bus.subscribe(pick_renderer())
            stats = resume_run(row['run_id'], config=cfg, store=store, bus=bus,
                               searcher=YtDlpSearcher(), http=http)
            if stats.failed:
                raise typer.Exit(2)
        else:
            console.print('[grey58]queued. "spotdlp resume" when ready.[/]')
    finally:
        store.close()
        http.close()


@app.command(rich_help_panel=PANEL_GET)
def setup() -> None:
    '''First-time setup: connect your Spotify account and pick a music folder.'''
    from .setup import run_setup_wizard
    if not sys.stdin.isatty():
        console.print(
            '[yellow]setup needs an interactive terminal[/]. It asks you to paste '
            'a couple of values. Open PowerShell or Windows Terminal and run '
            '[bold]spotdlp setup[/] there. (Automating it? Set SPOTIFY_CLIENT_ID and '
            'SPOTIFY_CLIENT_SECRET in the environment instead.)')
        raise typer.Exit(1)
    run_setup_wizard(console)


@app.command(rich_help_panel=PANEL_GET)
def welcome() -> None:
    '''
    The friendly first run: connect Spotify and show, in plain terms, how to use it.

    This is what the installer opens when it finishes, so it has to stand on its
    own. Someone who has never opened a terminal should be able to read it and know
    what to do next.
    '''
    from .setup import run_setup_wizard

    console.print()
    console.print('[bold cyan]  Welcome to spotdl+[/]')
    console.print('  [grey58]Give it anything from Spotify and it builds you a clean, '
                  'fully tagged music library.[/]\n')

    try:
        cfg = load_config()
    except E.SpotdlPlusError:
        cfg = None

    if cfg is not None and cfg.has_spotify_credentials():
        console.print('  [green]You\'re connected and ready to go.[/]\n')
    elif sys.stdin.isatty():
        run_setup_wizard(console, reason='One-time step: let\'s connect your Spotify account.')
    else:
        console.print('  [yellow]Next step: run[/] [bold]spotdlp setup[/] '
                      '[yellow]to connect Spotify.[/]\n')

    console.print('[bold]  How to use it[/] [grey58](type these right here in this window):[/]\n')
    console.print('    [accent]spotdlp get "artist:Radiohead"[/]     a whole discography')
    console.print('    [accent]spotdlp get "<spotify link>"[/]       album, playlist, or one song')
    console.print('    [accent]spotdlp doctor[/]                      check everything is working')
    console.print('    [accent]spotdlp --help[/]                      the full list of commands\n')
    console.print('  [grey58]Keep the quotes around a link. A Spotify share link has an '
                  '"&" in it, and without them your terminal cuts the command in half.[/]')
    console.print('  [grey58]A playlist has to be public for us to see it, even your own, '
                  'because we sign in as an app and never as you.[/]\n')
    console.print('  [grey58]Your music saves to the folder you picked during setup. '
                  'Reopen this screen anytime with[/] [accent]spotdlp welcome[/][grey58], '
                  'or connect Spotify again with[/] [accent]spotdlp setup[/][grey58].[/]\n')


@app.command(rich_help_panel=PANEL_DIAG)
def explain(code: str = typer.Argument(..., help='an error code, e.g. RATE_LIMITED')) -> None:
    '''What an error code means, whether it retries, and what to do about it.'''
    cls = E.lookup(code)
    if cls is None:
        console.print(f'unknown code {code!r}. known codes:')
        for known in sorted(E.all_codes()):
            console.print(f'  {known}')
        raise typer.Exit(1)
    console.print(f'[bold]{cls.code}[/]  retry policy: {cls.retry}')
    console.print(cls.remedy)


@app.command(rich_help_panel=PANEL_DIAG)
def doctor(
    network: bool = typer.Option(
        False, '--network',
        help='also try one real download from YouTube. Proves whether this IP '
             'is blocked right now, not just that the tools are all present'),
    verbose: bool = typer.Option(
        False, '--verbose', '-v',
        help='with --network: time each layer separately (DNS, TLS+API, the '
             'download itself) so "it\'s slow" gets an actual diagnosis'),
    playlist: str = typer.Option(
        None, '--playlist',
        help='with --network: read this playlist with your Spotify credentials. '
             'Use it when a playlist gets refused but everything else looks fine'),
    out: Path = OutOption,
) -> None:
    '''Check everything up front, so a run never dies at track 3 over something we could have caught here.'''
    try:
        cfg = _load(out)
    except E.SpotdlPlusError as err:
        console.print(f'[bold red]✗ config[/]  [{err.code}] {err.message}')
        console.print(f'  [grey58]{err.remedy}[/]')
        raise typer.Exit(1)

    checks: list[tuple[str, bool, str]] = []

    checks.append(('credentials', cfg.has_spotify_credentials(),
                   'run "spotdlp setup", or set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET'))

    from ..core.runtime import bundled_tool
    for tool in ('ffmpeg', 'ffprobe'):
        found = bundled_tool(tool) is not None or shutil.which(tool) is not None
        checks.append((tool, found, f'install ffmpeg (ships {tool})'))

    env = probe_environment()
    checks.append(('yt-dlp', env['yt_dlp'] is not None, 'pip install yt-dlp'))
    checks.append(('deno (JS runtime)', env['deno'] is not None,
                   'youtube needs a JS runtime or downloads stall, see net/ytenv.py'))
    checks.append(('PO-token provider', env['bgutil_script'] is not None,
                   'without it youtube may serve dead streams on some networks'))

    from ..media.fingerprint import find_fpcalc
    checks.append(('fpcalc (chromaprint)', find_fpcalc() is not None,
                   'powers "audit --deep" and acoustic verification'))
    checks.append(('AcoustID key', bool(cfg.acoustid_api_key),
                   'free at acoustid.org/new-application, enables '
                   'is-this-actually-the-right-song verification'))

    out = Path(cfg.output_dir)
    writable = False
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe_file = out / '.spotdlplus.doctor'
        probe_file.write_text('ok')
        probe_file.unlink()
        writable = True
    except OSError:
        pass
    checks.append((f'library writable ({out})', writable, 'check permissions and the drive'))

    if writable:
        free = shutil.disk_usage(out).free
        checks.append((f'free space ({human_bytes(free)})',
                       free > FREE_SPACE_RESERVE_BYTES,
                       'the run will park rather than fill the volume'))

    from ..net.probe import OnlineProbe
    checks.append(('network', OnlineProbe().online(), 'no route to the internet'))

    hard_fail = False
    for name, ok, hint in checks:
        mark = '[green]✓[/]' if ok else '[bold red]✗[/]'
        console.print(f' {mark} {name}')
        if not ok:
            console.print(f'   [grey58]{hint}[/]')
            # advisory checks: their absence degrades verification, not downloads
            if name not in ('PO-token provider', 'fpcalc (chromaprint)', 'AcoustID key'):
                hard_fail = True

    console.print()
    for key, meta in cfg.redacted().items():
        console.print(f' [grey58]{key:<22} {meta["value"]}  ({meta["from"]})[/]')

    if network:
        from ..media.fetch import probe_youtube
        from ..net.ytenv import (
            cookie_source_readable,
            install_noise_filter,
            set_cookie_source,
        )
        install_noise_filter(cfg.state_dir / 'subprocess-noise.log')
        set_cookie_source(browser=cfg.youtube_cookies_from_browser,
                          cookiefile=cfg.youtube_cookiefile)
        console.print('\n[bold]live network check[/]')
        if cfg.youtube_cookies_from_browser or cfg.youtube_cookiefile:
            cok, detail = cookie_source_readable()
            if cok:
                console.print(' [green]✓[/] browser cookies readable')
            else:
                console.print(f' [yellow]⚠[/] browser cookies unreadable  '
                              f'[grey58]{detail}. A run would proceed without them[/]')
                set_cookie_source(browser=None, cookiefile=None)
        if verbose:
            # layer by layer, each one timed. When something's slow, the slow
            # LAYER is the diagnosis. DNS about a second means router/DNS
            # trouble, a slow download wiht fast DNS means throttling.
            import socket as _socket
            import time as _time

            t0 = _time.perf_counter()
            try:
                _socket.getaddrinfo('www.youtube.com', 443)
                console.print(f' [green]✓[/] DNS resolve         '
                              f'[grey58]{(_time.perf_counter() - t0) * 1000:6.0f} ms[/]')
            except OSError as exc:
                console.print(f' [bold red]✗[/] DNS resolve failed, {exc}')
                hard_fail = True
            import httpx as _httpx
            # Forced to IPv4, same as every download we make. Measuring the v6
            # route teh tool never uses once produced a scary 80-second reading
            # on a network whose v6 blackholes (the exact disease the force-
            # ipv4 line cures). We measure the road we actually drive.
            t0 = _time.perf_counter()
            try:
                transport = _httpx.HTTPTransport(local_address='0.0.0.0')
                with _httpx.Client(timeout=10, transport=transport) as c:
                    c.get('https://www.youtube.com/generate_204')
                console.print(f' [green]✓[/] TLS + HTTPS reach   '
                              f'[grey58]{(_time.perf_counter() - t0) * 1000:6.0f} ms '
                              f'(IPv4, the route downloads use)[/]')
            except Exception as exc:  # noqa: BLE001  # This IS the diagnostic
                console.print(f' [bold red]✗[/] TLS/HTTPS failed, {type(exc).__name__}')
                hard_fail = True
            t0 = _time.perf_counter()
        console.print(' [grey58]… pulling one real track from YouTube (the canary) …[/]')
        try:
            probe_youtube()
            if verbose:
                dl_s = _time.perf_counter() - t0
                console.print(f' [green]✓[/] canary download     '
                              f'[grey58]{dl_s:6.1f} s (~19s of audio. under ~30s '
                              f'is healthy, over a minute means throttling)[/]')
            console.print(' [green]✓ YouTube downloads work[/] '
                          '[grey58]this machine can pull audio right now[/]')
        except E.SpotdlPlusError as err:
            console.print(f' [bold red]✗ [{err.code}][/] {err.message}')
            console.print(f'   [grey58]{err.remedy}[/]')
            hard_fail = True

        # The credentials line above only proves two strings exist. Somebody's
        # whole first run came back green here while every playlist request was
        # being refused, because a Spotify app that isn't set up for the Web API
        # still HAS an id and a secret. So actually use them, in three steps,
        # because which step dies is the entire diagnosis.
        if cfg.has_spotify_credentials():
            console.print(' [grey58]… asking Spotify with your credentials …[/]')
            sp_http = HttpClient(version=__version__)
            try:
                sp = SpotifyProvider(sp_http, spotify_token_provider(
                    sp_http, cfg.spotify_client_id, cfg.spotify_client_secret))
                try:
                    sp.album('2mCuMNdJkoyiXFhsQCLLqw')
                    console.print(' [green]✓[/] spotify credentials  '
                                  '[grey58]token accepted, Web API answering[/]')
                except E.SpotdlPlusError as err:
                    console.print(f' [bold red]✗ [{err.code}][/] spotify: {err.message}')
                    console.print(f'   [grey58]{err.remedy}[/]')
                    hard_fail = True
                else:
                    # Playlists are their own permission story, and the failure
                    # we keep seeing hits ONLY playlist items. Searched for
                    # rather than hardcoded, since editorial playlists stopped
                    # being readable and any fixed id eventually rots.
                    # Playlists are their own permission story, and the failure
                    # that keeps catching people hits ONLY playlist items.
                    pid, mine = None, playlist is not None
                    if mine:
                        ref = parse_spotify_ref(playlist)
                        if ref is None or ref[0] is not EntityKind.PLAYLIST:
                            console.print(' [yellow]⚠[/] playlist access    '
                                          '[grey58]that is not a playlist link[/]')
                        else:
                            pid = ref[1]
                    else:
                        pid = REFERENCE_PLAYLIST
                    if pid is not None:
                        try:
                            head = sp.playlist(pid)
                            n = sum(1 for _ in sp.playlist_tracks(pid))
                            console.print(
                                f' [green]✓[/] playlist access    '
                                f'[grey58]read {head.get("name")!r}, {n} track(s)[/]')
                        except E.SpotdlPlusError as err:
                            refused = err.code == 'META_FORBIDDEN'
                            # Refused is about US either way. Anything else
                            # against the built-in reference is about THAT
                            # playlist, which somebody may well have deleted
                            # years after this shipped, so it must not read as
                            # your setup being broken.
                            if refused or mine:
                                console.print(
                                    f' [bold red]✗ [{err.code}][/] playlist access')
                                if refused:
                                    console.print(
                                        '   [grey58]Albums work but playlists are refused, '
                                        'so this is your Spotify app rather than your '
                                        'network. Open developer.spotify.com/dashboard, '
                                        'check the app is set up for the Web API, and give '
                                        'it a redirect URI. A fresh app plus "spotdlp '
                                        'setup" clears it.[/]')
                                else:
                                    console.print(f'   [grey58]{err.remedy}[/]')
                                hard_fail = True
                            else:
                                console.print(
                                    f' [yellow]⚠[/] playlist access    '
                                    f'[grey58]could not read the built-in test playlist '
                                    f'({err.code}). There is every chance it was deleted '
                                    f'or made private, which says nothing about your '
                                    f'setup. Re-check with --playlist "<your own playlist '
                                    f'link>" to be sure[/]')
            finally:
                sp_http.close()

    raise typer.Exit(1 if hard_fail else 0)


@app.command(rich_help_panel=PANEL_TRUST)
def library(
    verify: bool = typer.Option(False, '--verify',
                                help='confirm every owned file still exists on disk'),
    all_artists: bool = typer.Option(False, '--all',
                                     help='list every artist, including the one-track tail'),
    out: Path = OutOption,
) -> None:
    '''
    Who's in the library: the per-artist roster, and --verify checks it's all really on disk.

    The companion to `stats`. This one is the who, that one is the numbers.
    '''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        rows = store.library_rows()
        if not rows:
            console.print('the library is empty. "spotdlp get <something>" fixes that.')
            return

        total = sum(r['size_bytes'] or 0 for r in rows)
        by_artist: dict[str, int] = {}
        missing: list[str] = []
        for r in rows:
            p = Path(r['final_path'])
            try:
                artist = p.relative_to(cfg.output_dir).parts[0]
            except ValueError:
                artist = p.parent.name
            by_artist[artist] = by_artist.get(artist, 0) + 1
            if verify and not p.is_file():
                missing.append(r['final_path'])

        console.print(f'[bold]{_n(len(rows), "track")}[/] · {human_bytes(total)} · '
                      f'{_n(len(by_artist), "artist")} · {cfg.output_dir}')
        ranked = sorted(by_artist.items(), key=lambda kv: (-kv[1], kv[0]))
        singles = [a for a, n in ranked if n == 1]
        table = Table(border_style='grey58', show_header=False)
        shown = ranked if (all_artists or len(singles) < 6) else             [(a, n) for a, n in ranked if n > 1]
        for artist, n in shown:
            table.add_row(artist, str(n))
        console.print(table)
        if len(shown) < len(ranked):
            console.print(f'[grey58]… and {_n(len(singles), "artist")} with a single '
                          f'track (features, mostly), "--all" lists them[/]')

        if verify:
            if missing:
                console.print(f'[bold red]{len(missing)} owned files are MISSING from disk:[/]')
                for m in missing[:20]:
                    console.print(f'  {m}')
                console.print('[grey58]"spotdlp relink" a track to refetch it.[/]')
                raise typer.Exit(2)
            console.print('[green]every owned file is present on disk.[/]')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_TRUST)
def audit(
    fix: bool = typer.Option(False, '--fix',
                             help='repair missing tags and art in place, from stored metadata'),
    deep: bool = typer.Option(False, '--deep',
                              help='fully decode every file (~0.5s each), catches '
                                   'mid-stream corruption a header probe can\'t see'),
    identity: bool = typer.Option(False, '--identity',
                                  help='the sixth claim: fingerprint every file against '
                                       'AcoustID to confirm it\'s the recording it claims '
                                       'to be. Implies --deep. Needs an acoustid_api_key.'),
    show: int = typer.Option(20, '--show', help='how many issues to list per kind'),
    out: Path = OutOption,
) -> None:
    '''
    Make every owned file prove itself: present, sound, fully tagged, art embedded, nothing unaccounted for.

    `--fix` rewrites tags and art in place. Audio bytes are never touched.
    '''
    from ..pipeline.audit import audit_library

    cfg = _load(out)
    store = Store(cfg.db_path)
    http = HttpClient(version=__version__) if (fix or identity) else None
    acoustid = None
    if identity:
        from ..providers.acoustid import AcoustIdClient
        acoustid = AcoustIdClient(http, cfg.acoustid_api_key)
        if not acoustid.available:
            raise _fail(E.CredentialsMissing(
                'audit --identity needs an AcoustID key (free at '
                'acoustid.org/new-application). set acoustid_api_key in config.toml'))
    try:
        from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                                   SpinnerColumn, TextColumn, TimeElapsedColumn)
        label = 'auditing (deep)' if (deep or identity) else 'auditing'
        with Progress(SpinnerColumn(style='cyan'), TextColumn(label),
                      BarColumn(bar_width=28, complete_style='cyan'),
                      MofNCompleteColumn(), TimeElapsedColumn(),
                      console=console, transient=True,
                      disable=not console.is_terminal) as prog:
            task = prog.add_task('audit', total=None)

            def on_file(i: int, total: int, _path: str) -> None:
                prog.update(task, total=total, completed=i)

            report = audit_library(
                store, output_dir=cfg.output_dir, cache_dir=cfg.cache_dir,
                http=http, fix=fix, deep=deep, acoustid=acoustid,
                progress=on_file,
            )

        console.print(f'checked [bold]{report.checked}[/] owned files · '
                      f'[green]{report.healthy} fully healthy[/]'
                      + (f' · [green]{report.fixed} repaired[/]' if fix else ''))
        if identity:
            console.print(
                f'identity: [green]{report.identity_confirmed} confirmed[/] · '
                f'{report.identity_unknown} unknown to AcoustID · '
                + (f'[bold red]{report.identity_mismatch} MISMATCHED[/]'
                   if report.identity_mismatch else '0 mismatched')
                + (f' · [warn]{report.quarantined} quarantined[/]'
                   if report.quarantined else ''))
            if report.quarantined:
                console.print('[grey58]quarantined files are under '
                              f'{cfg.output_dir}\\.quarantine, "spotdlp resume" '
                              'fetches replacements.[/]')
        if report.folder_art_written:
            console.print(f'[green]{report.folder_art_written} album folders '
                          f'gained a cover.jpg[/] (players that cannot read '
                          f'embedded opus art use these)')
        if report.fix_failed:
            console.print(f'[bold red]{report.fix_failed} repairs failed[/]')

        if not report.issues:
            console.print('[green]nothing left to flag. The archive is clean![/]')
            return

        for kind, count in sorted(report.by_kind.items(), key=lambda kv: -kv[1]):
            console.print(f'\n[bold]{kind}[/] ({count})')
            shown = [i for i in report.issues if i.kind == kind][:show]
            for i in shown:
                console.print(f'  {Path(i.path).name}')
                if i.detail:
                    console.print(f'    [grey58]{i.detail}[/]')
            if count > len(shown):
                console.print(f'  [grey58]... and {count - len(shown)} more[/]')

        if not fix and any(i.fixable for i in report.issues):
            console.print('\n[grey58]"spotdlp audit --fix" repairs the fixable ones in place.[/]')
        raise typer.Exit(1)
    finally:
        store.close()
        if http is not None:
            http.close()


#: The profiles everyone gets for free. A config [export_profiles.<name>] table
#: wiht the same name overrides. New names extend. `dest` lands in the config
#: the first time you use one, so devices become one flag forever.
_BUILTIN_EXPORT_PROFILES: dict[str, dict[str, str]] = {
    'ipod': {'to': 'ipod'},
    'ipod-lossless': {'to': 'ipod-lossless'},
    'mp3-player': {'to': 'universal'},
}


def _export_profile_menu(cfg: Config) -> str | None:
    '''
    Lists every profile and walks through making a new one. Returns a name to run
    with, or None if you just looked and left.
    '''
    from ..core.config import DELIVERY_TARGETS

    merged = {**_BUILTIN_EXPORT_PROFILES, **cfg.export_profiles}
    console.print('[bold]export profiles[/]')
    for name, body in sorted(merged.items()):
        origin = 'yours' if name in cfg.export_profiles else 'built-in'
        dest = body.get('dest') or '[grey58]asks each time[/]'
        console.print(f'  [accent]{name:<16}[/] {body.get("to", "?"):<14} '
                      f'-> {dest}  [grey58]({origin})[/]')
    console.print('\n[grey58]"spotdlp export --profile <name>" runs one. '
                  'Make a new one now?[/]')
    if not typer.confirm('create a profile?', default=False):
        return None
    from .setup import clean_input, write_config
    name = clean_input(typer.prompt('name it (e.g. car-usb)')).lower().replace(' ', '-')
    if not name:
        return None
    choices = sorted(k for k, v in DELIVERY_TARGETS.items() if v is not None)
    fmt = clean_input(typer.prompt(f'format ({" / ".join(choices)})', default='ipod'))
    if fmt not in choices:
        console.print(f'[yellow]{fmt!r} isn\'t a format we know, not saved.[/]')
        return None
    dest = clean_input(typer.prompt('destination folder/drive (blank = ask each time)',
                                    default='', show_default=False))
    body = {'to': fmt}
    if dest:
        body['dest'] = dest
    write_config(export_profiles={**cfg.export_profiles, name: body})
    console.print(f'[green]saved.[/] "spotdlp export --profile {name}" from now on.')
    return name if typer.confirm(f'run {name!r} now?', default=False) else None


@app.command(rich_help_panel=PANEL_GET)
def export(
    dest: Path = typer.Option(None, '--dest', '-d',
                              help='folder (or an iPod drive) to write the copies into'),
    to: str = typer.Option(None, '--to',
                           help="device format: 'ipod' (AAC .m4a), 'ipod-lossless' "
                                "(Apple Lossless), or 'universal' (MP3)"),
    profile_name: str = typer.Option(None, '--profile', '-P',
                                     help="a named device: 'ipod', 'mp3-player', or "
                                          "one you made. '--profile list' shows them "
                                          "all and can create new ones"),
    query: str = typer.Argument(None, help='only export owned files whose path contains this'),
    force: bool = typer.Option(False, '--force',
                               help='re-export even files already at the destination'),
    out: Path = OutOption,
) -> None:
    '''
    Copy your library into a device-ready format, without ever re-downloading.

    The archive is left exactly as it is and the copies go to --dest. Safe to
    re-run since anything already there gets skipped, so adding one song copies one
    song.
    '''
    from ..core.config import DELIVERY_TARGETS
    from ..pipeline.export import export_library

    cfg = _load(out)

    if profile_name in ('list', 'new', ''):
        picked = _export_profile_menu(cfg)
        if picked is None:
            return
        profile_name = picked
        cfg = _load(out)   # the menu may have just written a new profile

    if profile_name is not None:
        merged = {**_BUILTIN_EXPORT_PROFILES, **cfg.export_profiles}
        body = merged.get(profile_name)
        if body is None:
            console.print(f'[yellow]{profile_name!r} isn\'t a profile, '
                          f'"spotdlp export --profile list" shows what exists '
                          f'(and makes new ones).[/]')
            raise typer.Exit(1)
        to = to or body.get('to')
        if dest is None and body.get('dest'):
            dest = Path(body['dest'])

    to = to or 'ipod'
    spec = DELIVERY_TARGETS.get(to)
    if spec is None:
        choices = ', '.join(k for k, v in DELIVERY_TARGETS.items() if v is not None)
        raise _fail(E.ConfigInvalid(
            f'export needs a device format, not {to!r}. Try "--to ipod" '
            f'(choices: {choices}).'))

    if dest is None:
        if not sys.stdin.isatty():
            raise _fail(E.ConfigInvalid(
                'export needs somewhere to write: pass --dest, or give the '
                'profile one with "export --profile list".'))
        dest = Path(typer.prompt('write the copies where? (folder or drive)').strip())
        if profile_name and typer.confirm(
                f'remember this destination on the {profile_name!r} profile?',
                default=True):
            from .setup import write_config
            body = dict({**_BUILTIN_EXPORT_PROFILES, **cfg.export_profiles}
                        .get(profile_name, {'to': to}))
            body['dest'] = str(dest)
            write_config(export_profiles={**cfg.export_profiles, profile_name: body})
            console.print(f'[green]saved[/]. Next time "--profile {profile_name}" '
                          f'is all you need.')
    store = Store(cfg.db_path)
    try:
        if not store.library_rows():
            console.print('the library is empty. Nothing to export. '
                          '"spotdlp get <something>" first.')
            return

        from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                                   SpinnerColumn, TextColumn, TimeElapsedColumn)
        with Progress(SpinnerColumn(style='cyan'), TextColumn(f'exporting to {to}'),
                      BarColumn(bar_width=28, complete_style='cyan'),
                      MofNCompleteColumn(), TimeElapsedColumn(),
                      console=console, transient=True,
                      disable=not console.is_terminal) as prog:
            task = prog.add_task('export', total=None)

            def on_file(i: int, total: int, _path: str) -> None:
                prog.update(task, total=total, completed=i)

            report = export_library(
                store, dest=dest, target_format=spec.audio_format,
                bitrate=spec.bitrate, template=cfg.template,
                query=query, force=force, progress=on_file,
            )

        line = (f'exported [green]{report.exported}[/] · '
                f'{report.skipped} already there')
        if report.missing_source:
            line += f' · [yellow]{report.missing_source} source file(s) missing[/]'
        if report.failed:
            line += f' · [bold red]{report.failed} failed[/]'
        console.print(f'{line}  ->  {dest}')

        for path, reason in report.problems[:20]:
            console.print(f'  [grey58]{Path(path).name}: {reason}[/]')
        if len(report.problems) > 20:
            console.print(f'  [grey58]... and {len(report.problems) - 20} more[/]')

        if report.failed:
            raise typer.Exit(2)
    finally:
        store.close()


def _fold_empty_dirs(root: Path) -> int:
    '''Remove empty folders under (never including) root. Returns how many went.'''
    import os as _os
    folded = 0
    for dirpath, _dirnames, _filenames in _os.walk(root, topdown=False):
        p = Path(dirpath)
        # bottom-up + a LIVE emptiness check: os.walk's dirnames were listed
        # before the children got removed, so trusting them misses teh cascade
        if p != root:
            try:
                if not any(p.iterdir()):
                    p.rmdir()
                    folded += 1
            except OSError:
                pass
    return folded


@app.command(rich_help_panel=PANEL_TRUST)
def cleanup(
    deep: bool = typer.Option(False, '--deep',
                              help='the long version: probe every owned file '
                                   '(audio, tags, duration) and repair in place, '
                                   'plan on minutes, not seconds'),
    datacheck: bool = typer.Option(False, '--datacheck',
                                   help='rewrite every file\'s tags fresh from the '
                                        'stored metadata, so drift dies. pairs with '
                                        '--deep for the full works'),
    covers: bool = typer.Option(False, '--covers',
                                help='backfill missing folder cover.jpg files from '
                                     'the art embedded in the tracks'),
    empty_quarantine: bool = typer.Option(False, '--empty-quarantine',
                                          help='actually delete everything in '
                                               '.quarantine (it never empties itself)'),
    yes: bool = typer.Option(False, '--yes', '-y', help='skip the confirmation'),
    out: Path = OutOption,
) -> None:
    '''
    Housekeeping in one verb: stale cache, ghost bookkeeping, empty folders, the run database.

    The quick pass takes seconds and touches no music. `--deep` audits every file,
    `--datacheck` rewrites all the metadata, and the rest of the flags say what
    they clean.
    '''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        root = Path(cfg.output_dir)
        console.print('[bold]cleanup[/]')

        # 1) stale cache artifacts. Anything for a track no longer in flight
        active = store.active_track_ids()
        stale_files, stale_bytes = [], 0
        for sub in ('fetch', 'transcode', 'lyrics'):
            d = Path(cfg.cache_dir) / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and f.stem.split('.')[0] not in active:
                    stale_files.append(f)
                    stale_bytes += f.stat().st_size
        # .part litter in the library. Torn placements from killed runs
        part_litter = list(root.rglob('*.spotdlplus.part')) if root.is_dir() else []

        # 2) ghost ownership. Owned on paper, gone on disk
        ghosts = [r for r in store.library_rows()
                  if not Path(r['final_path']).is_file()]

        q_dir = root / '.quarantine'
        q_files = [p for p in q_dir.rglob('*') if p.is_file()] if q_dir.is_dir() else []
        q_bytes = sum(p.stat().st_size for p in q_files)

        console.print(
            f'  {len(stale_files)} stale cache file(s) ({human_bytes(stale_bytes)}) · '
            f'{len(part_litter)} torn .part · {len(ghosts)} ghost claim(s) · '
            f'{len(q_files)} in quarantine ({human_bytes(q_bytes)})')
        if not yes and not typer.confirm('clean the quick stuff?', default=True):
            console.print('[grey58]left as it was.[/]')
            return

        for f in stale_files + part_litter:
            f.unlink(missing_ok=True)
        for r in ghosts:
            store.revoke_ownership(r['identity'])
            console.print(f'  [grey58]healed: {Path(r["final_path"]).name} was owned '
                          f'but gone. It can download again[/]')
        folded = _fold_empty_dirs(root) if root.is_dir() else 0
        tidied = store.tidy(keep_recent=5)
        console.print(f'  [green]✓[/] cache cleared · {folded} empty folder(s) folded · '
                      f'db compacted ({human_bytes(tidied["reclaimed_bytes"])} back)')

        if empty_quarantine and q_files:
            if yes or typer.confirm(
                    f'delete all {len(q_files)} quarantined file(s) '
                    f'({human_bytes(q_bytes)})? they were kept as evidence', default=False):
                for p in q_files:
                    p.unlink(missing_ok=True)
                _fold_empty_dirs(q_dir)
                console.print('  [green]✓[/] quarantine emptied')

        if covers:
            from ..media.covers import read_embedded_cover, write_folder_art
            wrote = 0
            for r in store.library_rows():
                p = Path(r['final_path'])
                if p.is_file() and not (p.parent / 'cover.jpg').exists():
                    art = read_embedded_cover(p)
                    if art:
                        write_folder_art(p.parent, art)
                        wrote += 1
            console.print(f'  [green]✓[/] {wrote} folder cover(s) backfilled')

        if deep or datacheck:
            from ..net.http import HttpClient
            from ..pipeline.audit import audit_library
            http = HttpClient(version=__version__)
            try:
                if deep:
                    console.print('  [grey58]deep pass: probing every owned file '
                                  '(this is the slow, thorough one)…[/]')
                    rep = audit_library(store, output_dir=root,
                                        cache_dir=Path(cfg.cache_dir),
                                        http=http, fix=True, deep=True)
                    console.print(f'  [green]✓[/] deep audit: {rep.checked} checked · '
                                  f'{rep.fixed} repaired · {rep.quarantined} quarantined')
                if datacheck:
                    console.print('  [grey58]datacheck: rewriting every file\'s tags '
                                  'fresh from stored metadata…[/]')
                    from ..media.covers import read_embedded_cover
                    from ..media.tag import write_tags
                    rewrote = failed = 0
                    for r in store.library_rows():
                        p = Path(r['final_path'])
                        track = store.metadata_for_identity(r['identity'])
                        if not p.is_file() or track is None:
                            continue
                        try:
                            write_tags(p, track, fmt=p.suffix.lstrip('.'),
                                       cover=read_embedded_cover(p))
                            rewrote += 1
                        except E.SpotdlPlusError:
                            failed += 1
                    from ..pipeline.genres import write_genres_json
                    write_genres_json(store, root)
                    console.print(f'  [green]✓[/] datacheck: {rewrote} rewritten clean'
                                  + (f' · [yellow]{failed} refused[/]' if failed else ''))
            finally:
                http.close()

        console.print('[green]cleanup done.[/]')
    finally:
        store.close()


@app.command('move-library', rich_help_panel=PANEL_TRUST)
def move_library(
    dest: Path = typer.Argument(..., help='where the library should live now'),
    yes: bool = typer.Option(False, '--yes', '-y', help='skip the confirmations'),
    out: Path = OutOption,
) -> None:
    '''
    Relocate the whole library. Files move, bookkeeping follows, nothing re-
    downloads.
    '''
    import shutil as _shutil

    cfg = _load(out)
    if out is not None:
        console.print('[yellow]a vault (-o) carries its own database inside it, '
                      'just move the folder itself and everything rides along.[/]')
        raise typer.Exit(1)
    store = Store(cfg.db_path)
    try:
        old_root = Path(cfg.output_dir).resolve()
        new_root = dest.expanduser().resolve()
        if not old_root.is_dir():
            console.print(f'[yellow]{old_root} doesn\'t exist, nothing to move.[/]')
            raise typer.Exit(1)
        if new_root == old_root:
            console.print('[grey58]that\'s already where it lives.[/]')
            return
        owned = store.library_rows()
        console.print(f'[bold]move-library[/]  {len(owned)} owned file(s)')
        console.print(f'  [grey58]from[/] {old_root}')
        console.print(f'  [grey58]to  [/] {new_root}')
        if not yes and not typer.confirm('go?', default=True):
            console.print('[grey58]left as it was.[/]')
            return

        new_root.parent.mkdir(parents=True, exist_ok=True)
        if not new_root.exists():
            # one rename when the OS allows it. the whole tree in one atom
            try:
                old_root.rename(new_root)
            except OSError:
                _shutil.move(str(old_root), str(new_root))
        else:
            # destination exists: merge file-by-file, keeping relative layout
            for r in owned:
                src = Path(r['final_path'])
                if not src.is_file():
                    continue
                rel = src.relative_to(old_root)
                target = new_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                _shutil.move(str(src), str(target))
            for extra in ('genres.json',):
                if (old_root / extra).is_file():
                    _shutil.move(str(old_root / extra), str(new_root / extra))
            _fold_empty_dirs(old_root)

        n = store.rewrite_library_root(str(old_root), str(new_root))
        console.print(f'  [green]✓[/] moved. {n} ownership record(s) re-anchored')

        if yes or typer.confirm('update the config so future downloads land there too?',
                                default=True):
            from .setup import write_config
            write_config(output_dir=str(new_root))
            console.print(f'[green]config updated[/]. Output_dir is now {new_root}')
        else:
            console.print(f'[yellow]heads up: config still points at {old_root}, '
                          f'new downloads will land THERE until you change it.[/]')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_TRUST)
def undo(
    yes: bool = typer.Option(False, '--yes', '-y', help='skip the confirmation'),
    out: Path = OutOption,
) -> None:
    '''
    Irrevocably undo the most recent run step-by-step

    This command can undo every action through the program since the last run, but
    ONLY since the last run. Resumes and relinks reuse the same run so they are
    included. Anything you owned before that run stays where it is.
    '''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        run = store.latest_run_info()
        if run is None:
            console.print('nothing to undo. No runs at all.')
            return
        placed = store.run_placements(run['id'])
        size = sum(r['size_bytes'] or 0 for r in placed)
        console.print(f'[bold]undo[/]  last run: {run["source"][:60]}')
        console.print(f'  would remove {len(placed)} file(s) ({human_bytes(size)}) '
                      f'and the run\'s bookkeeping')
        if not placed:
            console.print('  [grey58](it placed nothing. Only the bookkeeping goes)[/]')
        if not yes and not typer.confirm('undo it?', default=False):
            console.print('[grey58]left as it was.[/]')
            return

        root = Path(cfg.output_dir)
        for r in placed:
            p = Path(r['final_path'])
            if p.is_file():
                console.print(f'  [bad]−[/] {p.name}')
                p.unlink()
            store.revoke_ownership(r['identity'])
        if root.is_dir():
            _fold_empty_dirs(root)
        store.delete_run(run['id'])
        from ..pipeline.genres import write_genres_json
        write_genres_json(store, root)
        console.print(f'  [green]✓[/] undone. {len(placed)} file(s) removed. the '
                      f'library is back to before that run')
    finally:
        store.close()


def _dir_size(p: Path) -> tuple[int, int]:
    '''(bytes, files) under p, or (0, 0) if it isn't there.'''
    if not p.is_dir():
        return 0, 0
    n = size = 0
    for f in p.rglob('*'):
        if f.is_file():
            size += f.stat().st_size
            n += 1
    return size, n


@app.command(rich_help_panel=PANEL_TRUST)
def uninstall(
    purge: bool = typer.Option(False, '--purge',
                               help='also delete your config and the ownership database'),
    everything: bool = typer.Option(False, '--everything',
                                    help='also delete every file spotdl+ put in the library'),
    dry_run: bool = typer.Option(False, '--dry-run',
                                 help='show what would go, touch nothing'),
    yes: bool = typer.Option(False, '--yes', '-y', help='skip the confirmation'),
    out: Path = OutOption,
) -> None:
    '''
    Remove what spotdl+ put on this machine, and nothing else.

    Plain, it clears only what regenerates itself: the download cache and the
    tools we fetched. Your config, your ownership records, and your music all
    stay, so reinstalling picks up exactly where you left off.

    --purge also drops the config and the ownership database. Your music stays
    on disk, but spotdl+ forgets it owns any of it, so the next run downloads it
    all again.

    --everything additionally deletes the files spotdl+ downloaded. Only those.
    Anything already in that folder before you pointed us at it is left alone,
    and we say how many we left.

    None of this removes the program itself. Use Add/Remove Programs for that.
    '''
    cfg = _load(out)
    root = Path(cfg.output_dir)
    if everything:
        purge = True

    # `-o` redirects output_dir and state_dir but NOT cache_dir, so deriving the
    # app folder from the cache would reach out of the vault and delete the
    # GLOBAL config. It did exactly that to me while I was testing this command,
    # and my own credentials went with it. A vault only ever owns its own state
    # and its own files.
    vaulted = out is not None
    plan: list[tuple[str, Path, int, int]] = []
    if not vaulted:
        base = Path(cfg.cache_dir).parent
        for label, path in (('cache', Path(cfg.cache_dir)),
                            ('tools', base / 'tools')):
            size, n = _dir_size(path)
            if n:
                plan.append((label, path, size, n))
        if purge:
            size, n = _dir_size(base / 'config')
            if n:
                plan.append(('config', base / 'config', size, n))
    if purge:
        size, n = _dir_size(Path(cfg.state_dir))
        if n:
            plan.append(('state (ownership database)', Path(cfg.state_dir), size, n))

    store = Store(cfg.db_path)
    owned: list[Path] = []
    strangers = 0
    try:
        if everything:
            owned = [Path(r['final_path']) for r in store.library_rows()
                     if r['final_path']]
            mine = {p.resolve() for p in owned}
            if root.is_dir():
                for fmt in ('opus', 'mp3', 'flac', 'm4a', 'wav'):
                    for f in root.rglob(f'*.{fmt}'):
                        if f.resolve() not in mine:
                            strangers += 1
    finally:
        store.close()

    console.print('[bold]uninstall[/]')
    if vaulted:
        console.print(f'  [grey58]vault only: {root}. The shared cache, tools, and '
                      f'config outside it are not touched.[/]')
    for label, path, size, n in plan:
        console.print(f'  [bad]−[/] {label}  [grey58]{human_bytes(size)}, '
                      f'{n} file(s)  {path}[/]')
    if everything:
        live = [p for p in owned if p.is_file()]
        size = sum(p.stat().st_size for p in live)
        console.print(f'  [bad]−[/] library  [grey58]{human_bytes(size)}, '
                      f'{len(live)} file(s) we downloaded  {root}[/]')
        if strangers:
            console.print(f'  [green]keeping[/] {strangers} audio file(s) in that '
                          f'folder that were not ours')
    if not plan and not everything:
        console.print('  nothing to remove. Already clean.')
        return
    if not purge:
        console.print('  [grey58]keeping your config, ownership records, and music. '
                      'A reinstall resumes as if nothing happened.[/]')
    elif not everything:
        console.print('  [yellow]your music stays on disk, but spotdl+ will forget '
                      'it owns any of it. The next run re-downloads everything.[/]')

    if dry_run:
        console.print('[grey58]dry run. Nothing was touched.[/]')
        return
    if not yes and not typer.confirm('go ahead?', default=False):
        console.print('[grey58]left as it was.[/]')
        return

    removed = 0
    if everything:
        for p in owned:
            if p.is_file():
                p.unlink()
                removed += 1
        gj = root / 'genres.json'
        if gj.is_file():
            gj.unlink()
        quarantine = root / '.quarantine'
        if quarantine.is_dir():
            shutil.rmtree(quarantine, ignore_errors=True)
        if root.is_dir():
            _fold_empty_dirs(root)
        console.print(f'  [green]✓[/] {removed} downloaded file(s) removed')
    for label, path, _size, _n in plan:
        shutil.rmtree(path, ignore_errors=True)
        console.print(f'  [green]✓[/] {label} removed')
    console.print('  [green]✓[/] done. The program itself is still installed. '
                  'Remove it from Add/Remove Programs when you want it gone.')


@app.command(rich_help_panel=PANEL_TRUST)
def tidy(
    yes: bool = typer.Option(False, '--yes', '-y', help='skip the confirmation'),
    keep: int = typer.Option(5, '--keep', help='always keep this many newest runs'),
    out: Path = OutOption,
) -> None:
    '''
    Compact the run database, which is the database slice of `cleanup` on its own.

    Never touches your music or what you own. Runs still holding failed tracks are
    kept because those are your relink queue, and so are the newest few.
    '''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        size = cfg.db_path.stat().st_size if cfg.db_path.is_file() else 0
        console.print(f'[bold]tidy[/]  run database is {human_bytes(size)}  '
                      f'[grey58]({cfg.db_path})[/]')
        if not yes and not typer.confirm(
                'prune finished-run bookkeeping and compact?', default=True):
            console.print('[grey58]left as it was.[/]')
            return
        result = store.tidy(keep_recent=max(0, keep))
        if result['runs'] == 0:
            console.print('  [green]✓[/] nothing to prune. Compacted what was there.')
        else:
            console.print(
                f'  [green]✓[/] pruned {result["runs"]} old runs '
                f'({result["tracks"]} track rows, {result["candidates"]} candidate '
                f'rows) · reclaimed [bold]{human_bytes(result["reclaimed_bytes"])}[/]')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_DIAG)
def stats(
    details: bool = typer.Option(False, '--details', '-d',
                                 help='the deep cut: per-album sizes, newest '
                                      'additions, the last 7 days'),
    out: Path = OutOption,
) -> None:
    '''
    The library in numbers: tracks, size, hours, formats.

    The companion to `library`. That one is the who, this is the how much.
    '''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        s = store.library_stats(details=details)
        if s['tracks'] == 0:
            console.print('nothing owned yet. "spotdlp get" something first.')
            return
        hours = s['duration_ms'] / 3_600_000
        console.print(f'[bold]{_n(s["tracks"], "track")}[/] · '
                      f'[bold]{human_bytes(s["bytes"])}[/] · '
                      f'~{hours:.1f} hours of music · '
                      f'{_n(s["artists"], "artist")}')
        fmt_bits = ' · '.join(f'{n} {fmt}' for fmt, n, _ in s['formats'])
        console.print(f'[grey58]formats: {fmt_bits}[/]\n')

        table = Table(border_style='grey58')
        for col in ('artist', 'tracks', 'size', 'hours'):
            table.add_column(col)
        shown = s['per_artist'] if details else s['per_artist'][:15]
        for artist, n, size, ms in shown:
            table.add_row(artist or '?', str(n), human_bytes(size),
                          f'{ms / 3_600_000:.1f}')
        console.print(table)
        if not details and len(s['per_artist']) > 15:
            console.print(f'[grey58]…and {len(s["per_artist"]) - 15} more, '
                          f'"spotdlp stats -d" for everything[/]')

        if details:
            console.print('\n[bold]biggest albums[/]')
            for r in s['albums'][:12]:
                console.print(f'  {human_bytes(r["size"]):>9}  {r["name"]}  '
                              f'[grey58]({r["n"]} tracks)[/]')
            n7, size7 = s['last_7_days']
            console.print(f'\n[bold]last 7 days[/]  {_n(n7, "track")} · {human_bytes(size7)}')
            if s['recent']:
                console.print('[bold]newest[/]')
                for r in s['recent'][:5]:
                    console.print(f'  [grey58]{Path(r["final_path"]).name}[/]')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_DIAG)
def search(
    query: str = typer.Argument(..., help='part of a title, artist, or album'),
    complete: bool = typer.Option(False, '--complete', '-c',
                                  help='per-album completeness: do you have the '
                                       'whole thing, and if not, how short are you'),
    out: Path = OutOption,
) -> None:
    '''Find what you own. Instant and offline. This never touches the network.'''
    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        if complete:
            rows = store.album_completeness(query)
            if not rows:
                console.print(f'nothing we\'ve ever seen matches {query!r}.')
                raise typer.Exit(1)
            full = [r for r in rows if r['owned'] == r['known']]
            for r in rows:
                if r['owned'] == r['known']:
                    console.print(f'  [green]✓ complete[/]  {r["artist"]}, '
                                  f'{r["album_title"]}  [grey58]{r["owned"]}/{r["known"]}[/]')
                else:
                    console.print(f'  [yellow]○ partial [/]  {r["artist"]}, '
                                  f'{r["album_title"]}  [grey58]{r["owned"]}/{r["known"]}'
                                  f', {r["known"] - r["owned"]} short[/]')
            console.print(f'\n{len(full)}/{len(rows)} albums complete '
                          '[grey58](vs everything any run has ever seen, '
                          'not a promise about songs we never met)[/]')
            return

        rows = store.search_owned(query)
        if not rows:
            console.print(f'nothing you own matches {query!r}. '
                          '[grey58]("search -c" checks album completeness instead)[/]')
            raise typer.Exit(1)
        for r in rows:
            dur = (r['duration_ms'] or 0) // 1000
            year = f' ({r["year"]})' if r['year'] else ''
            console.print(f'  [bold]{r["title"]}[/], {r["artist"]}')
            console.print(f'    [grey58]{r["album_title"] or "single"}{year} · '
                          f'{r["format"]} · {human_bytes(r["size_bytes"] or 0)} · '
                          f'{dur // 60}:{dur % 60:02d}[/]')
            console.print(f'    [grey58]{r["final_path"]}[/]')
        console.print(f'\n[bold]{len(rows)}[/] owned track(s) match')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_DIAG)
def report(
    as_list: bool = typer.Option(False, '--list', '-l',
                                 help='dev-terms: every failure with its full '
                                      'context, one per line, ready to send'),
    out: Path = OutOption,
) -> None:
    '''
    One pasteable block about the last run. For when something broke and you
    need to show someone.
    '''
    import json as _json
    import platform

    cfg = _load(out)
    store = Store(cfg.db_path)
    try:
        run_id = store.latest_run(statuses=('active', 'parked', 'finished'))
        console.print(f'[bold]report[/]  [grey58]spotdlp {__version__}[/]')
        try:
            import yt_dlp
            ytv = yt_dlp.version.__version__
        except ImportError:
            ytv = 'missing'
        console.print(f'  [grey58]os {platform.platform()} · '
                      f'python {platform.python_version()} · yt-dlp {ytv} · '
                      f'frozen {getattr(sys, "frozen", False)}[/]')
        if run_id is None:
            console.print('  no runs yet, nothing to report.')
            return
        counts = store.counts(run_id)
        console.print(f'  run {run_id}  ' + ' · '.join(
            f'{k} {v}' for k, v in sorted(counts.items())))
        fails = store.failures(run_id)
        if not fails:
            console.print('  [green]no failures, a clean run![/]')
            return
        if as_list:
            # the raw feed. Codes, messages, context, everything a dev wants
            for f in fails:
                console.print(_json.dumps(f, default=str))
        else:
            from collections import Counter
            by_code = Counter(f.get('code', '?') for f in fails)
            for code, n in by_code.most_common():
                console.print(f'  [bold red]{n:>4}[/] × {code}')
            console.print(f'  [grey58]{len(fails)} failures total, '
                          f'"spotdlp report --list" is the full dev-terms dump[/]')
    finally:
        store.close()


@app.command(rich_help_panel=PANEL_DIAG)
def version() -> None:
    '''Which spotdlp this is.'''
    console.print(f'spotdlp {__version__}')


def main() -> None:
    '''
    Console entry point, and the last line of defense. A typed error that escapes a
    command gets rendered as its code and remedy instead of a Python traceback,
    because nobody should see a stack dump for a problem we already understand.
    '''
    try:
        app()
    except E.SpotdlPlusError as err:
        console.print(f'[bold red][{err.code}][/] {err.message}')
        console.print(f'[grey58]{err.remedy}[/]')
        sys.exit(1)


if __name__ == '__main__':
    main()

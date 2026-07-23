'''
render.py - the terminal view of the event stream

One subscriber out of however many there could be. It holds no logic, since
everything it prints it learned from an event. If somehting can't be shown
here, the fix is a richer event and never a print() in the core.
'''

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

from ..core.events import (
    DuplicatesCollapsed,
    EntityDiscovered,
    Event,
    Failed,
    MatchDecided,
    PlanReady,
    RateLimitHit,
    ReleaseFiltered,
    RunFinished,
    RunParked,
    RunStarted,
    RunThrottled,
    StageStarted,
    TrackProgress,
    TrackStateChanged,
    Warned,
)

_THEME = Theme({
    'ok': 'green',
    'bad': 'bold red',
    'warn': 'yellow',
    'dim': 'grey58',
    'accent': 'cyan',
})

#: Short human labels for the walk, shown live on each tarck's progress row.
_STAGE_LABEL = {
    'matched': 'matched',
    'fetched': 'downloading',
    'transcoded': 'converting',
    'tagged': 'tagging',
    'placed': 'placing',
    'done': 'verified',
}


def human_bytes(n: int | float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def human_duration(s: float) -> str:
    '''364 seconds is a number. "6m 04s" is a duration.'''
    s = int(s)
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m {s % 60:02d}s'
    return f'{s // 3600}h {(s % 3600) // 60:02d}m'


class Renderer:
    '''Subscribe me to a bus. Unsubscribe by dropping the return of subscribe().'''

    def __init__(self, *, verbose: bool = False, console: Console | None = None) -> None:
        self.console = console or Console(theme=_THEME, highlight=False)
        self.verbose = verbose
        self._progress_line = ''
        self._remedies_shown: set[str] = set()

    def __call__(self, event: Event) -> None:   # the bus's Handler signature
        match event:
            case RunStarted():
                self.console.print(f'[accent]▸[/] {event.source}  [dim]profile={event.profile}[/]')
            case EntityDiscovered() if event.entity_kind == 'artist':
                self.console.print(
                    f'  [accent]{event.name}[/]. {event.child_count} releases to consider')
            case EntityDiscovered() if self.verbose:
                self.console.print(f'  [dim]+ {event.name} ({event.child_count} tracks)[/]')
            case ReleaseFiltered():
                self.console.print(f'  [dim]— skipped {event.title}  ({event.reason})[/]')
            case DuplicatesCollapsed() if event.basis == 'work':
                self.console.print(
                    f'  [dim]⧉ kept the earlier master over {len(event.dropped)} reissue(s)[/]'
                ) if self.verbose else None
            case PlanReady():
                self.console.print(
                    f'[accent]plan[/]  {event.track_count} tracks · '
                    f'~{human_bytes(event.est_bytes)} · '
                    f'{event.already_have} owned · {event.unmatched} unmatched')
            case MatchDecided() if self.verbose:
                self.console.print(
                    f'  [dim]match {event.score:.2f} ({event.considered} considered)[/]')
            case TrackStateChanged() if event.new_state == 'done':
                album = f'  [dim]· {event.album}[/]' if event.album else ''
                self.console.print(f'  [ok]✓[/] {event.title}{album}')
            case TrackProgress() if event.total_bytes:
                pct = 100 * event.done_bytes / event.total_bytes
                self.console.print(
                    f'  [dim]↓ {pct:3.0f}% of {human_bytes(event.total_bytes)}[/]',
                    end='\r')
            case RateLimitHit():
                self.console.print(
                    f'  [warn]⏸ {event.host} asked for {event.wait_s:.0f}s, waiting[/]')
            case RunThrottled() if event.active:
                self.console.print(
                    f'  [warn]🐢 YouTube is pushing back. Slowing to one download at a '
                    f'time, {event.pace_s:.0f}s apart, to try to crawl under it[/]')
            case RunThrottled():
                self.console.print('  [ok]▲ a download got through. Back to full speed[/]')
            case Warned():
                self.console.print(f'  [warn]⚠ {event.message}[/]')
            case Failed() if not event.will_retry and event.error is not None:
                err = event.error
                self.console.print(f'  [bad]✗ [{err.code}][/] {err.message}')
                if err.code not in self._remedies_shown:
                    self._remedies_shown.add(err.code)
                    self.console.print(f'    [dim]{err.remedy}[/]')
            case Failed() if self.verbose and event.error is not None:
                self.console.print(
                    f'  [dim]retry after [{event.error.code}] (attempt {event.attempt})[/]')
            case RunParked():
                self.console.print(f'[warn]⏸ run parked:[/] {event.reason}')
                self.console.print(f'  [dim]{event.resume_hint}[/]')
            case RunFinished():
                self.console.print(
                    f'[accent]done[/]  [ok]{event.ok} ok[/] · '
                    f'{event.failed} failed · {event.skipped} skipped · '
                    f'{event.elapsed_s:.0f}s')
            case StageStarted() if self.verbose and event.total:
                self.console.print(f'  [dim]{event.stage}: {event.total} to do[/]')
            case _:
                pass


class LiveRenderer:
    '''
    The good looking one. A rich Live display with three parts.

    Scrolling history, where every verified track prints a permanent line above the
    live region and failures print with their remedies, so nothing you saw
    disappears. The overall bar, showing the run at a glance as tracks walk toward
    DONE. And per-track rows showing the current stage with a byte bar and transfer
    speed while fetching, pulsing until the size is known.

    Anything that isn't a TTY gets the plain Renderer instead. This one is for
    people watching.
    '''

    def __init__(self, *, verbose: bool = False, console: Console | None = None) -> None:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
            TransferSpeedColumn,
        )

        from rich.progress import TaskProgressColumn

        class _ThreeKindsProgress(Progress):
            '''
            Tasks come in three currencies and can't share columns. A track counter
            rendered through DownloadColumn reads '0/1 bytes ?', which shipped once. Counts
            get M-of-N, the run bar gets a percentage, and byte tasks get size and speed.
            '''

            _COUNT_COLS = None
            _STEP_COLS = None
            _BYTE_COLS = None

            def get_renderables(self):
                if self._COUNT_COLS is None:
                    bar = dict(bar_width=28, style='grey35', complete_style='cyan',
                               finished_style='green', pulse_style='magenta')
                    common = (
                        SpinnerColumn(style='accent'),
                        TextColumn('[progress.description]{task.description}'),
                        BarColumn(**bar),
                    )
                    self._COUNT_COLS = (*common, MofNCompleteColumn(), TimeElapsedColumn())
                    # The run bar gets a time-remaining too. It's a loose, by-
                    # step estimate (fetch steps are slow, tag steps are
                    # instant, rich just averages). Call it a vibe with a unit,
                    # not a promise.
                    from rich.progress import TimeRemainingColumn
                    self._STEP_COLS = (*common, TaskProgressColumn(), TimeElapsedColumn(),
                                       TextColumn('[grey58]~[/]'),
                                       TimeRemainingColumn(compact=True))
                    self._BYTE_COLS = (*common, DownloadColumn(),
                                       TransferSpeedColumn(), TimeElapsedColumn())
                by_kind = {'count': [], 'steps': [], 'bytes': []}
                for task in self.tasks:
                    by_kind[task.fields.get('kind', 'bytes')].append(task)
                for kind, cols in (('count', self._COUNT_COLS),
                                   ('steps', self._STEP_COLS),
                                   ('bytes', self._BYTE_COLS)):
                    if by_kind[kind]:
                        yield self._table_for(cols, by_kind[kind])

            def _table_for(self, columns, tasks):
                saved = self.columns
                self.columns = columns
                try:
                    return self.make_tasks_table(tasks)
                finally:
                    self.columns = saved

        self.console = console or Console(theme=_THEME, highlight=False)
        self.verbose = verbose
        self._progress = _ThreeKindsProgress(console=self.console, transient=False)
        self._overall = None          # TaskID for the run bar (created lazily)
        self._pending_total = None    # tracks to attempt, learned at PlanReady
        self._steps_seen = {}         # track_id -> pipeline steps walked
        self._steps_total = 0         # every pipeline step walked, all tracks
        self._ok = 0                  # tracks fully done (for the summary tally)
        self._prep = None             # TaskID for the pre-download phases
        self._rows: dict[str, object] = {}     # track_id -> TaskID
        self._titles: dict[str, str] = {}      # track_id -> title
        self._started = False
        self._counts = {'ok': 0, 'failed': 0, 'skipped': 0}
        self._remedies_shown: set[str] = set()

    #: a tarck crosses 5 states on its way home. Each is one bar step, so the
    #: bar moves through the LONG phase (fetch) instead of sitting at zero for
    #: 8 straight minutes and then teleporting at the final verify sweep. Which
    #: it did, on a real 134-track run. Not doing that again.
    _STEP_STATES = ('fetched', 'transcoded', 'tagged', 'placed', 'done')

    def _ensure_overall(self) -> None:
        '''The run bar exists only once a download actually begins.'''
        if self._overall is None and self._pending_total is not None:
            self._ensure_started()
            self._overall = self._progress.add_task(
                self._run_label(),
                total=self._pending_total * len(self._STEP_STATES), kind='steps')

    def _run_label(self) -> str:
        # Derived from forward steps, not the DONE count: stages run batch-at-
        # a-time, so nothing reaches DONE until the final verify sweep and a
        # done-count sits at 0 all run then snaps to N. Failed tracks fill the
        # bar but not this, so it lands on teh true success total.
        total = self._pending_total or 0
        done = min(self._steps_total // len(self._STEP_STATES), total) if total else 0
        return f'[bold]run  {done}/{total} tracks[/]'

    def _step(self, track_id: str, n: int = 1) -> None:
        self._ensure_overall()
        if self._overall is None:
            return
        self._steps_seen[track_id] = self._steps_seen.get(track_id, 0) + n
        self._steps_total += n
        self._progress.advance(self._overall, n)
        self._progress.update(self._overall, description=self._run_label())

    # -- plumbing -------------------------------------------------------------

    def _ensure_started(self) -> None:
        if not self._started:
            self._progress.start()
            self._started = True

    def _stop(self) -> None:
        if self._started:
            self._progress.stop()
            self._started = False

    def _print(self, text: str) -> None:
        '''History lines land above the live region and stay there.'''
        target = self._progress.console if self._started else self.console
        target.print(text)

    def _drop_row(self, track_id: str) -> None:
        task = self._rows.pop(track_id, None)
        if task is not None:
            self._progress.remove_task(task)

    def _row_for(self, track_id: str, label: str):
        task = self._rows.get(track_id)
        title = (self._titles.get(track_id) or track_id)[:34]
        desc = f'[accent]{label:<11}[/] {title}'
        if task is None:
            task = self._progress.add_task(desc, total=None)   # pulses until sized
            self._rows[track_id] = task
        else:
            self._progress.update(task, description=desc)
        return task

    # -- the Handler ----------------------------------------------------------

    def __call__(self, event: Event) -> None:
        match event:
            case RunStarted():
                self._print(f'[accent]♪[/] [bold]{event.source}[/]  [dim]{event.profile}[/]')
                self._ensure_started()
                self._prep = self._progress.add_task('[accent]preparing  [/]',
                                                     total=None, kind='count')

            case EntityDiscovered() if event.entity_kind == 'artist':
                self._print(f'  [accent]{event.name}[/], '
                            f'{event.child_count} releases to consider')

            case ReleaseFiltered():
                self._print(f'  [dim]— {event.title}  ({event.reason})[/]')

            case StageStarted() if self._prep is not None and event.total:
                self._progress.update(
                    self._prep, total=event.total, completed=0,
                    description=f'[accent]{str(event.stage):<11}[/]')

            case TrackStateChanged():
                self._titles[event.track_id] = event.title
                if self._prep is not None and event.new_state in ('enriched', 'matched'):
                    self._progress.advance(self._prep, 1)
                if event.new_state == 'done':
                    self._counts['ok'] += 1
                    self._ok += 1
                    self._drop_row(event.track_id)
                    album = f'  [dim]· {event.album}[/]' if event.album else ''
                    self._print(f'  [ok]✓[/] {event.title}{album}')
                    self._step(event.track_id)   # also refreshes the run label
                elif event.new_state == 'skipped':
                    self._counts['skipped'] += 1
                elif event.new_state in self._STEP_STATES:
                    if event.new_state == 'fetched':
                        # Download over, row leaves now. Stages run batch-at-a-
                        # time and it woudl sit stale until `done`.
                        self._drop_row(event.track_id)
                    self._step(event.track_id)

            case TrackProgress():
                self._ensure_overall()
                task = self._row_for(event.track_id, 'downloading')
                if event.total_bytes:
                    self._progress.update(task, total=event.total_bytes,
                                          completed=event.done_bytes)

            case PlanReady():
                if self._prep is not None:
                    self._progress.remove_task(self._prep)
                    self._prep = None
                self._print(
                    f'[accent]plan[/]  {event.track_count} tracks · '
                    f'~{human_bytes(event.est_bytes)} · '
                    f'{event.already_have} owned · {event.unmatched} unmatched')
                # Hand the terminal back now. The gate's confirm prompt comes
                # right after tihs event, and a prompt underneath an active
                # Live display is invisible. A real sync once sat at its
                # question forever looking exactly liek a hang. The display
                # restarts itself on the first download.
                self._stop()
                # No bar yet. The gate may decline, and a bar for a run that
                # never runs is the dangling "0/1" taht once shipped. It counts
                # only tracks that will be ATTEMPTED. Deriving it any other way
                # left a bar stranded at 45/47 forever.
                self._pending_total = max(event.matched, 1)

            case MatchDecided() if self.verbose:
                self._print(f'  [dim]match {event.score:.2f} '
                            f'({event.considered} considered)[/]')

            case RateLimitHit():
                self._print(f'  [warn]⏸ {event.host} asked for '
                            f'{event.wait_s:.0f}s, waiting[/]')

            case RunThrottled() if event.active:
                self._print(
                    f'  [warn]🐢 YouTube is pushing back. Slowing to one at a time, '
                    f'{event.pace_s:.0f}s apart, to try to crawl under it[/]')

            case RunThrottled():
                self._print('  [ok]▲ a download got through. Back to full speed[/]')

            case Warned():
                self._print(f'  [warn]⚠ {event.message}[/]')

            case Failed() if not event.will_retry and event.error is not None:
                self._counts['failed'] += 1
                self._drop_row(event.track_id or '')
                err = event.error
                self._print(f'  [bad]✗ [{err.code}][/] {err.message}')
                # 5 identical three-line remedies in one screen is spam. the
                # first one teaches and the rest just take up room
                if err.code not in self._remedies_shown:
                    self._remedies_shown.add(err.code)
                    self._print(f'    [dim]{err.remedy}[/]')
                # Fill the BAR so it still reaches 100%, but don't credit a
                # failed track to the run counter. That number is completed
                # tracks, so it must land on the real success total, not N.
                if self._overall is not None and event.track_id:
                    seen = self._steps_seen.get(event.track_id, 0)
                    remaining = len(self._STEP_STATES) - seen
                    if remaining > 0:
                        self._steps_seen[event.track_id] = seen + remaining
                        self._progress.advance(self._overall, remaining)

            case Failed() if self.verbose and event.error is not None:
                self._print(f'  [dim]retry after [{event.error.code}] '
                            f'(attempt {event.attempt})[/]')

            case RunParked():
                self._stop()
                self._print(f'[warn]⏸ parked:[/] {event.reason}')
                self._print(f'  [dim]{event.resume_hint}[/]')

            case RunFinished():
                self._stop()
                # The event carries the store's real counts. The renderer's own
                # tally misses skips that happen before any event fires, and it
                # once printed '0 skipped' over a run that skipped two.
                self._print(
                    f'[accent]done[/]  [ok]{event.ok} ok[/] · {event.failed} failed · '
                    f'{event.skipped} skipped · {human_duration(event.elapsed_s)}')

            case _:
                pass


def pick_renderer(*, verbose: bool = False):
    '''The live display for humans. Plain lines for pipes, logs, and CI.'''
    console = Console(theme=_THEME, highlight=False)
    if console.is_terminal:
        return LiveRenderer(verbose=verbose, console=console)
    return Renderer(verbose=verbose, console=console)

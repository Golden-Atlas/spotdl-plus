'''
test_render.py - the live display's arithmetic, held to account.

The renderer is a pure function of the event stream, so its counters are
testable without a terminal: feed events, read the label. The one that mattered
enough to write down: the run counter must actually climb during a run.
'''

from __future__ import annotations

from rich.console import Console

from spotdlplus.cli.render import LiveRenderer
from spotdlplus.core.events import PlanReady, TrackStateChanged

# a track walks these five states home. the pipeline runs them batch-at-a-time,
# so every track is fetched before any is transcoded, and so on.
_STATES = ('fetched', 'transcoded', 'tagged', 'placed', 'done')


def _label_count(r: LiveRenderer) -> int:
    return int(r._run_label().split('run  ')[1].split('/')[0])


def _feed_batched(r: LiveRenderer, n: int) -> list[int]:
    '''Drive n tracks through the batched pipeline, sampling the counter per phase.'''
    ids = [f't{i}' for i in range(n)]
    samples = []
    for state in _STATES:
        for tid in ids:
            r(TrackStateChanged(run_id='x', track_id=tid, title=tid,
                                new_state=state, old_state='x'))
        samples.append(_label_count(r))
    return samples


def _renderer() -> LiveRenderer:
    return LiveRenderer(console=Console(force_terminal=False))


def test_run_counter_climbs_and_is_not_pinned_at_zero():
    # The bug: because DONE only arrives in the final verify sweep, a done-based
    # counter read 0 for the whole run and then snapped to N. It must climb.
    r = _renderer()
    r(PlanReady(run_id='x', matched=281, track_count=281))
    samples = _feed_batched(r, 281)

    assert samples[0] > 0                       # moving before the final phase
    assert samples == sorted(samples)           # never goes backwards
    assert len(set(samples)) > 1                # actually changes
    assert samples[-1] == 281                   # lands exactly at N/N


def test_run_counter_ends_at_total_for_a_small_run():
    r = _renderer()
    r(PlanReady(run_id='x', matched=3, track_count=3))
    samples = _feed_batched(r, 3)
    assert samples[-1] == 3
    assert samples == sorted(samples)


def test_failed_tracks_fill_the_bar_but_do_not_inflate_the_counter():
    # Your Smashing Pumpkins run: 277 done, a few failed. The counter must land
    # on the successes (277/281), not race to 281/281 by crediting failures.
    from spotdlplus.core.errors import ErrorRecord, Retry
    from spotdlplus.core.events import Failed

    r = _renderer()
    r(PlanReady(run_id='x', matched=5, track_count=5))
    good, bad = ['t1', 't2', 't3'], ['t4', 't5']
    rec = ErrorRecord(code='FETCH_BLOCKED', message='blocked',
                      retry=Retry.NEVER, remedy='x' * 30, context={})

    # fetch phase: three download, two fail outright
    for t in good:
        r(TrackStateChanged(run_id='x', track_id=t, title=t,
                            new_state='fetched', old_state='matched'))
    for t in bad:
        r(Failed(run_id='x', track_id=t, error=rec, will_retry=False))
    # the three survivors walk the rest of the way home
    for state in ('transcoded', 'tagged', 'placed', 'done'):
        for t in good:
            r(TrackStateChanged(run_id='x', track_id=t, title=t,
                                new_state=state, old_state='x'))

    assert _label_count(r) == 3            # successes, not 5


def test_plan_ready_hands_the_terminal_back_for_the_gate_prompt():
    # The gate's confirm comes right after PlanReady, and under an active Live
    # display it was invisible, and a real `sync` sat at its question looking
    # exactly like a hang. The display must be stopped when the plan prints.
    r = _renderer()
    r._ensure_started()
    assert r._started
    r(PlanReady(run_id='x', matched=3, track_count=3))
    assert not r._started, 'the prompt needs the terminal. the bar restarts itself'
    # ...and the first download activity brings it back
    r(TrackStateChanged(run_id='x', track_id='t1', title='t1',
                        new_state='fetched', old_state='matched'))
    assert r._started


def test_counter_never_exceeds_the_total():
    r = _renderer()
    r(PlanReady(run_id='x', matched=5, track_count=5))
    # over-emit done events (a resumed run can re-touch states). still capped
    ids = [f't{i}' for i in range(5)]
    for state in _STATES:
        for tid in ids:
            r(TrackStateChanged(run_id='x', track_id=tid, title=tid,
                                new_state=state, old_state='x'))
    for tid in ids:  # a second done pass
        r(TrackStateChanged(run_id='x', track_id=tid, title=tid,
                            new_state='done', old_state='placed'))
    assert _label_count(r) == 5

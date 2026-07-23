'''
test_architecture.py - ARCHITECTURE.md, made executable.

Every rule in the contract is checked here against the real source tree. If the
code and the contract disagree, this goes red. Discipline decays. A failing test
does not.

Add a layer? Add it to LAYERS. Add a stage? It gets checked. There is no way to
quietly violate the design, including by me, six months from now, at 2am.
'''

from __future__ import annotations

import ast
import json
import sys
from collections import deque
from pathlib import Path

import pytest

from spotdlplus.core import errors as E
from spotdlplus.core.config import DEFAULT_TEMPLATE, TEMPLATE_FIELDS, Config
from spotdlplus.core.events import Event
from spotdlplus.core.models import TRANSITIONS, TrackState

SRC = Path(__file__).resolve().parents[1] / 'src'
PKG = SRC / 'spotdlplus'

#: ARCHITECTURE.md §1. Imports flow downward. Never up.
LAYERS: dict[str, int] = {
    'core': 0,
    'net': 1,
    'providers': 2,
    'match': 3,
    'media': 4,
    'pipeline': 5,
    'cli': 6,
}

#: ARCHITECTURE.md §2. Nothing outside cli/ may see a human.
RENDERERS = {'rich', 'typer', 'click', 'argparse', 'tqdm'}


def _py_files() -> list[Path]:
    return sorted(PKG.rglob('*.py'))


def _layer_of(path: Path) -> str | None:
    parts = path.relative_to(SRC).parts
    return parts[1] if len(parts) > 2 else None   # spotdlplus/<layer>/file.py


def _imports(path: Path) -> list[tuple[str, tuple[str, ...] | None]]:
    '''
    Yield (top_level_name, resolved_internal_parts_or_None) for every import.

    Relative imports are resolved against the file's own package so that
    `from ..core.errors import X` inside net/ is correctly attributed to core.
    '''
    pkg_parts = path.relative_to(SRC).parts[:-1]
    out: list[tuple[str, tuple[str, ...] | None]] = []
    tree = ast.parse(path.read_text(encoding='utf-8'))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split('.'))
                internal = parts if parts[0] == 'spotdlplus' else None
                out.append((parts[0], internal))
        elif isinstance(node, ast.ImportFrom):
            if node.level:                              # relative
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = tuple(node.module.split('.')) if node.module else ()
                out.append(('spotdlplus', base + mod))
            else:
                parts = tuple((node.module or '').split('.'))
                internal = parts if parts and parts[0] == 'spotdlplus' else None
                out.append((parts[0] if parts else '', internal))
    return out


# ----------------------------------------------------------------------------
# §1 layering
# ----------------------------------------------------------------------------

def test_imports_only_ever_flow_downward():
    violations: list[str] = []
    for py in _py_files():
        here = _layer_of(py)
        if here is None or here not in LAYERS:
            continue
        for _, internal in _imports(py):
            if not internal or len(internal) < 2:
                continue
            there = internal[1]
            if there not in LAYERS:
                continue
            if LAYERS[there] > LAYERS[here]:
                violations.append(f'{py.relative_to(SRC)} ({here}) imports up into {there}')
    assert not violations, 'layer inversion:\n  ' + '\n  '.join(violations)


def test_core_has_no_supply_chain():
    '''core/ imports the standard library and nothing else. This is load-bearing.'''
    stdlib = set(sys.stdlib_module_names)
    violations: list[str] = []
    for py in sorted((PKG / 'core').glob('*.py')):
        for top, internal in _imports(py):
            if internal or not top or top in stdlib:
                continue
            violations.append(f'{py.name} imports third-party {top!r}')
    assert not violations, 'core grew a dependency:\n  ' + '\n  '.join(violations)


# ----------------------------------------------------------------------------
# §2 the seam
# ----------------------------------------------------------------------------

def test_nothing_outside_cli_imports_a_renderer():
    violations: list[str] = []
    for py in _py_files():
        if _layer_of(py) == 'cli':
            continue
        for top, _ in _imports(py):
            if top in RENDERERS:
                violations.append(f'{py.relative_to(SRC)} imports {top!r}')
    assert not violations, 'the seam leaked:\n  ' + '\n  '.join(violations)


def test_nothing_outside_cli_prints():
    violations: list[str] = []
    for py in _py_files():
        if _layer_of(py) == 'cli':
            continue
        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == 'print':
                    violations.append(f'{py.relative_to(SRC)}:{node.lineno}')
    assert not violations, 'someone printed:\n  ' + '\n  '.join(violations)


# ----------------------------------------------------------------------------
# §7 failure is typed
# ----------------------------------------------------------------------------

def _all_error_subclasses(cls=E.SpotdlPlusError):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_error_subclasses(sub)


def test_every_error_is_registered_and_carries_a_remedy():
    registry = E.all_codes()
    for cls in _all_error_subclasses():
        assert cls.code in registry, f'{cls.__name__} is not @register-ed'
        assert registry[cls.code] is cls, f'{cls.code} is claimed by another class'
        assert cls.remedy != E.SpotdlPlusError.remedy, f'{cls.code} has no remedy'


def test_error_codes_are_unique_and_shouty():
    for code in E.all_codes():
        assert code == code.upper(), f'{code} is not upper case'
        assert '_' in code, f'{code} has no category prefix'


def test_retry_policy_is_data_not_code():
    '''The Engine reads these. A stage must never branch on them itself.'''
    assert E.Offline.retry is E.Retry.PARK
    assert E.RateLimited.retry is E.Retry.AFTER
    assert E.DownloadFailed.retry is E.Retry.BACKOFF
    assert E.AuthRefreshLoop.retry is E.Retry.NEVER


# ----------------------------------------------------------------------------
# events must survive the wire. a GUI or a JSONL log is downstream of this
# ----------------------------------------------------------------------------

def _all_event_subclasses(cls=Event):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_event_subclasses(sub)


def test_every_event_serializes_to_json():
    for cls in _all_event_subclasses():
        ev = cls(run_id='r1')
        blob = json.dumps(ev.to_dict())
        assert json.loads(blob)['kind'] == cls.__name__


def test_events_are_frozen():
    '''An event a subscriber can mutate is an event the next subscriber cannot trust.'''
    for cls in _all_event_subclasses():
        ev = cls(run_id='r1')
        with pytest.raises((AttributeError, TypeError)):
            ev.run_id = 'r2'   # type: ignore[misc]


# ----------------------------------------------------------------------------
# §5 the state machine
# ----------------------------------------------------------------------------

def test_transition_table_covers_every_state():
    assert set(TRANSITIONS) == set(TrackState)
    for src, targets in TRANSITIONS.items():
        for t in targets:
            assert isinstance(t, TrackState), f'{src} -> {t!r} is not a TrackState'


def test_done_is_reachable_and_terminal():
    seen, queue = {TrackState.DISCOVERED}, deque([TrackState.DISCOVERED])
    while queue:
        for nxt in TRANSITIONS[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert seen == set(TrackState), f'unreachable states: {set(TrackState) - seen}'
    assert TRANSITIONS[TrackState.DONE] == frozenset(), 'DONE must be a sink'


def test_a_half_fetched_file_cannot_be_tagged():
    '''The specific corruption the state machine exists to prevent.'''
    assert TrackState.TAGGED not in TRANSITIONS[TrackState.MATCHED]
    assert TrackState.PLACED not in TRANSITIONS[TrackState.FETCHED]
    assert TrackState.DONE not in TRANSITIONS[TrackState.TAGGED]


def test_every_pipeline_stage_walks_a_legal_edge():
    '''
    A stage's (consumes -> produces) pair must be an edge the state machine
    allows, or the Engine will advance it straight into StateTransitionError at
    runtime. Checked here so a new stage cannot compile its way past the map.
    '''
    from spotdlplus.net.http import HttpClient
    from spotdlplus.pipeline.stages import download_stages, resolve_stages

    class _NullSearcher:
        def search(self, query):
            return []

    stages = resolve_stages(_NullSearcher()) + download_stages(
        _NullSearcher(), HttpClient.__new__(HttpClient))
    assert len(stages) == 7
    for stage in stages:
        assert stage.produces in TRANSITIONS[stage.consumes], \
            f'{type(stage).__name__}: {stage.consumes} -> {stage.produces} is not a legal move'

    # ...and chained together they walk DISCOVERED all the way to DONE.
    assert stages[0].consumes is TrackState.DISCOVERED
    assert stages[-1].produces is TrackState.DONE
    for prev, nxt in zip(stages, stages[1:]):
        assert prev.produces is nxt.consumes, \
            f'{type(prev).__name__} -> {type(nxt).__name__} leaves a gap'


# ----------------------------------------------------------------------------
# config drift
# ----------------------------------------------------------------------------

def test_default_template_only_uses_declared_fields():
    from string import Formatter
    used = {n.split('.')[0].split('[')[0] for _, n, _, _ in Formatter().parse(DEFAULT_TEMPLATE) if n}
    assert used <= TEMPLATE_FIELDS, f'default template uses undeclared: {used - TEMPLATE_FIELDS}'


def test_default_config_is_valid_and_slow_on_purpose():
    cfg = Config()
    cfg.validate()
    assert cfg.concurrency <= 2, 'the default must not be a way to get rate-limited'
    assert cfg.profile == 'canonical'


def test_secrets_never_survive_redaction():
    cfg = Config(spotify_client_id='real-id', spotify_client_secret='real-secret')
    blob = json.dumps(cfg.redacted())
    assert 'real-id' not in blob and 'real-secret' not in blob

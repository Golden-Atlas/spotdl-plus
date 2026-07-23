'''
test_delivery.py - delivery presets pick the device format, and 'archive' still
means 'change nothing'.

Every case is isolated from the real machine config via injected user_config /
env / project_dir, so a developer's own config.toml can't sway the result.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.config import DELIVERY_TARGETS, load_config
from spotdlplus.core.errors import ConfigInvalid


def cfg(tmp_path, **overrides):
    '''A config resolved from defaults only, plus whatever overrides we pass.'''
    return load_config(
        project_dir=tmp_path,
        user_config=tmp_path / 'nonexistent.toml',
        env={},
        overrides=overrides or None,
    )


def test_default_is_archive_and_keeps_opus(tmp_path):
    c = cfg(tmp_path)
    assert c.delivery == 'archive'
    assert c.audio_format == 'opus'
    assert c.bitrate == '192k'


def test_ipod_preset_is_aac_m4a(tmp_path):
    c = cfg(tmp_path, delivery='ipod')
    assert c.audio_format == 'm4a'
    assert c.bitrate == '256k'


def test_universal_preset_is_mp3(tmp_path):
    c = cfg(tmp_path, delivery='universal')
    assert c.audio_format == 'mp3'
    assert c.bitrate == '320k'


def test_ipod_lossless_is_alac_in_m4a(tmp_path):
    from spotdlplus.media.transcode import ext_for
    c = cfg(tmp_path, delivery='ipod-lossless')
    assert c.audio_format == 'alac'
    # the codec is alac, but the file an iPod reads is .m4a
    assert ext_for('alac') == 'm4a'


def test_extension_matches_format_for_everything_but_alac():
    from spotdlplus.media.transcode import ext_for
    for fmt in ('opus', 'mp3', 'flac', 'm4a', 'wav'):
        assert ext_for(fmt) == fmt
    assert ext_for('alac') == 'm4a'


def test_archive_does_not_override_an_explicit_format(tmp_path):
    '''delivery='archive' must leave a chosen audio_format untouched.'''
    c = cfg(tmp_path, delivery='archive', audio_format='flac')
    assert c.audio_format == 'flac'


def test_unknown_delivery_is_rejected(tmp_path):
    with pytest.raises(ConfigInvalid, match='delivery'):
        cfg(tmp_path, delivery='walkman')


def test_delivery_from_env(tmp_path):
    c = load_config(
        project_dir=tmp_path,
        user_config=tmp_path / 'nonexistent.toml',
        env={'SPOTDLPLUS_DELIVERY': 'ipod'},
    )
    assert c.audio_format == 'm4a'


def test_delivery_from_config_file(tmp_path):
    conf = tmp_path / 'config.toml'
    conf.write_text('delivery = "ipod"\n', encoding='utf-8')
    c = load_config(project_dir=tmp_path, user_config=conf, env={})
    assert c.audio_format == 'm4a' and c.bitrate == '256k'


def test_provenance_credits_the_delivery_preset(tmp_path):
    c = cfg(tmp_path, delivery='ipod')
    meta = c.redacted()
    assert meta['audio_format']['from'] == 'delivery:ipod'
    assert meta['delivery']['value'] == 'ipod'


def test_archive_maps_to_no_override(tmp_path):
    '''The table's contract: only 'archive' is the pass-through None.'''
    assert DELIVERY_TARGETS['archive'] is None
    assert DELIVERY_TARGETS['ipod'] is not None


# ----------------------------------------------------------------------------
# the CLI surface: --to on get/plan, and _load threading it through
# ----------------------------------------------------------------------------

def test_load_threads_to_into_delivery(tmp_path):
    '''`_load(out, delivery='ipod')` must reach the resolved format.'''
    from spotdlplus.cli.main import _load
    c = _load(tmp_path / 'V', delivery='ipod')
    assert c.audio_format == 'm4a' and c.bitrate == '256k'


def test_load_with_no_to_uses_configured_format(tmp_path):
    '''An unset --to arrives as None and must not force a delivery.'''
    from spotdlplus.cli.main import _load
    c = _load(tmp_path / 'V', delivery=None)
    # reads the real machine config, so don't pin the value, just that None
    # didn't crash and left a valid, resolved delivery in place.
    assert c.delivery in DELIVERY_TARGETS


def test_get_and_plan_advertise_to(command_options):
    for cmd in ('get', 'plan'):
        assert '--to' in command_options(cmd)

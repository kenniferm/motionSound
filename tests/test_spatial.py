import numpy as np
import pytest
from scipy.signal import convolve
from soundlab.spatial import dataset, render
from soundlab.spatial.scene import Source


def source(audio, directions, frames=None):
    audio = np.asarray(audio, dtype=float)
    frames = frames if frames is not None else [0, len(audio)-1]
    return Source(audio, np.array(frames), np.array(directions, dtype=float))


def test_coordinate_and_direction_interpolation():
    axes = dataset.spherical([[0, 0], [90, 0], [-90, 0], [0, 90]])
    np.testing.assert_allclose(axes, [[1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1]], atol=1e-15)
    mid = source(np.ones(5), axes[:2]).at([2])
    np.testing.assert_allclose(mid, [[2**-.5, 2**-.5, 0]])


def test_dynamic_crossfade_history_and_tail_against_direct_reference():
    n = 300
    # Impulses before the boundary and on the final frame exercise filter history and tail.
    signal = np.zeros(n)
    signal[[0, 126, 129, 250, 299]] = [1, 2, 3, 4, 5]
    dirs = np.eye(3)
    hrir = np.random.default_rng(5).normal(size=(3, 2, 185))
    s = source(signal, dirs[[0, 1, 2, 2]], [0, 128, 256, n-1])
    actual, _ = render.moving(s, dirs, hrir, 128)
    static = [np.stack([convolve(signal, ear, mode='full', method='direct')
                        for ear in pair], axis=1) for pair in hrir]
    w = np.arange(128)[:, None] / 128
    expected = np.concatenate([(1-w)*static[0][:128] + w*static[1][:128],
                              (1-w)*static[1][128:256] + w*static[2][128:256],
                              static[2][256:]])
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-8)
    fixed, _ = render.moving(source(signal, [dirs[0]]*2), dirs, hrir, 128)
    np.testing.assert_allclose(fixed, static[0], rtol=1e-6, atol=1e-8)
    same_filters, _ = render.moving(s, dirs, np.repeat(hrir[:1], 3, axis=0), 128)
    np.testing.assert_allclose(same_filters, static[0], rtol=1e-6, atol=1e-8)


def test_resampling_matches_pyfar_and_delays_are_applied_once():
    import pyfar

    impulse = np.zeros((1, 2, 256))
    impulse[0, :, [60, 75]] = 1
    ref = pyfar.dsp.resample(pyfar.Signal(impulse, 44100), 48000, match_amplitude='freq')
    actual = dataset.resample_ir(impulse, 44100)
    np.testing.assert_allclose(actual, ref.time, rtol=1e-6, atol=1e-8)
    assert np.argmax(actual[0, 0]) == round(60*160/147)
    assert actual.sum(axis=-1) == pytest.approx(2, abs=.005)
    delayed, common = dataset.apply_delay(np.ones((1, 2, 1)), [[0, 3]])
    assert common == 0
    np.testing.assert_array_equal(delayed, [[[1, 0, 0, 0], [0, 0, 0, 1]]])
    fractional, common = dataset.apply_delay(np.ones((1, 2, 1)), [[0, 2.5]])
    assert common == 16
    np.testing.assert_allclose(fractional.sum(axis=-1), 1)
    assert fractional[0, 1, 18] != 0 and fractional[0, 1, 19] != 0




def test_viewer_sub_bass_uses_material_frequency_and_time():
    from soundlab.spatial_view_bass import bass_flags

    rate = 48000
    time = np.arange(rate // 2) / rate
    samples = np.concatenate([np.zeros_like(time), .1*np.sin(2*np.pi*40*time),
                              .1*np.sin(2*np.pi*440*time), .0001*np.sin(2*np.pi*40*time)])
    assert bass_flags(samples, rate) == [False, True, False, False]


def test_viewer_reads_source_envelope_and_keeps_silence_at_zero(tmp_path):
    from base64 import b64decode

    from soundlab.spatial_view_bass import add_audio_features

    rate = 48000
    frames_per_bin = rate // 50
    time = np.arange(frames_per_bin) / rate
    tone = np.sin(2 * np.pi * 1000 * time)
    clip = np.concatenate([np.zeros_like(tone), .04 * tone, .4 * tone, .02 * tone[:13]])
    np.save(tmp_path/'sound.npy', clip.astype(np.float32))
    source = {'id': 'sound', 'frames': len(clip)}

    add_audio_features({'rate': rate, 'sources': [source]}, tmp_path)

    silent, quiet, loud, tail = b64decode(source['wave'])
    assert silent == 0
    assert 0 < quiet < loud < 255
    assert tail > 0
    assert len(source['bass']) == 1


def test_viewer_trace_preserves_signed_source_extrema_and_short_tail(tmp_path):
    from base64 import b64decode

    from soundlab.spatial_view_bass import add_audio_features

    rate = 48000
    clip = np.zeros(rate // 1000 * 2 + 3, dtype=np.float32)
    clip[53], clip[77], clip[-1] = .64, -.04, -.25
    np.save(tmp_path/'a.npy', clip)
    np.save(tmp_path/'b.npy', -clip)
    a, b = [{'id': name, 'frames': len(clip)} for name in ('a', 'b')]

    add_audio_features({'rate': rate, 'sources': [a, b]}, tmp_path)

    assert a['wave'] == b['wave']
    trace = lambda source: np.frombuffer(b64decode(source['trace']), dtype=np.int8).reshape(-1, 2)
    np.testing.assert_array_equal(trace(a), [[0, 0], [-25, 102], [-64, 0]])
    np.testing.assert_array_equal(trace(b), [[0, 0], [-102, 25], [0, 64]])

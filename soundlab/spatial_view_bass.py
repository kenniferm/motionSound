"""Measure signal levels in saved mono materials for the viewer."""
from base64 import b64encode
from functools import lru_cache
from pathlib import Path

import numpy as np

from .runner import safe_artifact


WAVE_RATE = 50
TRACE_RATE = 1000


def _signal_blocks(samples, rate, bin_rate):
    count = (len(samples) * bin_rate + rate - 1) // rate
    block_bins = 5 * bin_rate
    for first in range(0, count, block_bins):
        last = min(first + block_bins, count)
        edges = np.arange(first, last + 1, dtype=np.int64) * rate // bin_rate
        edges[-1] = min(edges[-1], len(samples))
        yield samples[edges[0]:edges[-1]], edges - edges[0]


def waveform_bins(samples, rate):
    """Encode the RMS of each 20 ms interval with additional room for quiet sounds."""
    bins = bytearray()
    for clip, edges in _signal_blocks(samples, rate, WAVE_RATE):
        with np.errstate(over='ignore', invalid='ignore'):
            squared = np.square(clip, dtype=np.float64)
            mean_square = np.add.reduceat(squared, edges[:-1]) / np.diff(edges)
        mean_square = np.nan_to_num(mean_square, nan=0, posinf=0, neginf=0)
        level = np.sqrt(np.sqrt(np.clip(mean_square, 0, 1)))
        bins.extend(np.rint(255 * level).astype(np.uint8).tobytes())
    return b64encode(bins).decode('ascii')


def waveform_trace(samples, rate):
    """Encode signed minimum and maximum samples for every millisecond."""
    bins = bytearray()
    for clip, edges in _signal_blocks(samples, rate, TRACE_RATE):
        extrema = np.column_stack((np.minimum.reduceat(clip, edges[:-1]),
                                   np.maximum.reduceat(clip, edges[:-1])))
        extrema = np.nan_to_num(extrema, nan=0, posinf=0, neginf=0)
        levels = np.copysign(np.sqrt(np.minimum(np.abs(extrema), 1)), extrema)
        bins.extend(np.rint(127 * levels).astype(np.int8).tobytes())
    return b64encode(bins).decode('ascii')


def bass_flags(samples, rate):
    """Measure 20–80 Hz RMS in half-second windows above -45 dBFS."""
    frames = rate // 2
    window = np.hanning(frames)
    frequencies = np.fft.rfftfreq(frames, 1 / rate)
    band = (frequencies >= 20) & (frequencies <= 80)
    scale = 2 / (frames * np.sum(window**2))
    flags = []
    for start in range(0, len(samples), frames):
        clip = samples[start:start + frames]
        if len(clip) < frames:
            # Center a short ending window so an audible tail is still represented.
            missing = frames - len(clip)
            clip = np.pad(clip, (missing // 2, missing - missing // 2))
        energy = np.sum(np.abs(np.fft.rfft(clip * window)[band])**2)
        flags.append(bool(energy * scale >= 10**(-45 / 10)))
    return flags


@lru_cache(maxsize=256)
def saved_audio_features(path: Path, rate: int, frames: int, mtime_ns: int, size: int):
    if rate < WAVE_RATE or size > frames * 8 + 10000:
        return {}
    clip = np.load(path, mmap_mode='r', allow_pickle=False)
    if not isinstance(clip, np.ndarray):
        clip.close()
        return {}
    if clip.shape not in ((frames,), (frames, 1)) or clip.dtype not in (np.float32, np.float64):
        return {}
    samples = clip.reshape(-1)
    features = {'wave': waveform_bins(samples, rate), 'bass': bass_flags(samples, rate)}
    if rate >= TRACE_RATE:
        features['trace'] = waveform_trace(samples, rate)
    return features


def add_audio_features(data, directory):
    for source in data['sources']:
        try:
            path = safe_artifact(directory/f"{source['id']}.npy", directory)
            stat = path.stat()
            source.update(saved_audio_features(
                path, data['rate'], source['frames'], stat.st_mtime_ns, stat.st_size))
        except (OSError, ValueError, EOFError):
            continue

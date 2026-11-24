"""Reference DSP for local experiments. Generated scripts remain self-contained."""

import numpy as np
from scipy.signal import butter, sosfiltfilt


def friction_material(frames, rate, seed):
    """Build a reusable, sustained friction source with irregular resonances."""
    rng = np.random.default_rng(seed)
    excitation = rng.normal(size=frames)
    t = np.arange(frames) / rate
    excitation *= .55 + .4 * np.sin(t * 19 + .7 * np.sin(t * 11))
    material = np.zeros(frames)
    for center, width, gain in ((190, 72, 1.), (730, 100, .63), (2040, 245, .43)):
        band = butter(2, [center - width, center + width], fs=rate, btype='bandpass', output='sos')
        material += gain * sosfiltfilt(band, excitation)
    material = np.tanh(1.8 * material)
    return material


def variable_read(material, positions, rate):
    """Read a material at arbitrary positions, with conservative anti-alias filtering."""
    positions = np.asarray(positions, dtype=float)
    speed = np.max(np.abs(np.diff(positions)), initial=1.)
    if speed > 1:
        lowpass = butter(8, .82 * rate / (2 * speed), fs=rate, output='sos')
        material = sosfiltfilt(lowpass, material)
    return np.interp(positions, np.arange(len(material)), material, left=0., right=0.)


def assemble_events(frames, events):
    """Layer tapered material reads from (frame, samples, gain) events."""
    output = np.zeros(frames)
    for start, samples, gain in events:
        count = min(len(samples), frames - start)
        if not 0 <= start < frames or count < 2:
            continue
        output[start:start + count] += samples[:count] * np.hanning(count) * gain
    return output

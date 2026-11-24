"""Output-time-varying binaural FIR with shared input history."""
import numpy as np
from scipy.signal import oaconvolve
from scipy.spatial import KDTree
from ..cancellation import check

CONTROL_HOP = 256


def static(audio, hrir):
    return oaconvolve(audio[:, None], hrir.T, axes=0)


def control_frames(frames, hop):
    return np.r_[np.arange(0, frames - 1, hop), frames - 1]


def moving(source, directions, filters, hop=CONTROL_HOP):
    if hop not in (128, 256, 512):
        raise ValueError('control hopは128、256、512のいずれかです')
    audio = source.audio
    last = len(audio) - 1
    knots = control_frames(len(audio), hop)
    requested = source.at(knots)
    chosen = KDTree(directions).query(requested)[1]
    angular_step = np.rad2deg(np.arccos(np.clip(
        np.sum(requested[:-1] * requested[1:], axis=1), -1, 1)))
    max_step = float(angular_step.max(initial=0))
    if np.all(chosen == chosen[0]):
        return static(audio, filters[chosen[0]]), max_step
    history = filters.shape[-1] - 1
    padded = np.pad(audio, (history, 0))
    output = np.empty((len(audio) + history, 2))
    for start, end, first, second in zip(knots, knots[1:], chosen, chosen[1:]):
        check()
        block = padded[start:end + history]
        length = end - start
        # Convolution outputs before history index need samples from an earlier block.
        left = static(block, filters[first])[history:history+length]
        if first != second:
            right = static(block, filters[second])[history:history+length]
            weight = np.arange(length)[:, None] / length
            left = (1 - weight) * left + weight * right
        output[start:end] = left
    # Last content frame and full causal tail use the final filter.
    output[last:] = static(padded[last:], filters[chosen[-1]])[history:]
    return output, max_step



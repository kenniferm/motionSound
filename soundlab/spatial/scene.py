"""Read mono clips and interpolate their trajectories."""
from dataclasses import dataclass
from zipfile import BadZipFile

import numpy as np
from scipy.spatial import geometric_slerp

from ..runner import safe_artifact

RATE = 48000


@dataclass
class Source:
    audio: np.ndarray
    frames: np.ndarray
    directions: np.ndarray

    def at(self, samples):
        samples = np.asarray(samples)
        if np.array_equal(samples, self.frames):
            return self.directions.copy()
        result = np.empty((len(samples), 3))
        if len(self.frames) == 1:
            result[:] = self.directions[0]
            return result
        segments = np.clip(np.searchsorted(self.frames, samples, side='right') - 1,
                           0, len(self.frames) - 2)
        for i in np.unique(segments):
            mask = segments == i
            start, end = self.frames[i:i+2]
            fraction = np.clip((samples[mask] - start) / (end - start), 0, 1)
            result[mask] = geometric_slerp(*self.directions[i:i+2], fraction)
        return result


def read_mono(path, base, frames):
    safe_artifact(path, base)
    if path.stat().st_size > frames * 8 + 10000:
        raise ValueError('mono音源ファイルが大きすぎます')
    try:
        audio = np.load(path, mmap_mode='r', allow_pickle=False)
    except (ValueError, EOFError, BadZipFile) as exc:
        raise ValueError('mono音源のNPYを読み込めません') from exc
    if not isinstance(audio, np.ndarray):
        audio.close()
        raise ValueError('mono音源をNPY配列として保存してください')
    if (audio.shape not in ((frames,), (frames, 1))
            or audio.dtype not in (np.float32, np.float64) or not np.isfinite(audio).all()
            or audio.max() > 1 or audio.min() < -1):
        raise ValueError('音源は指定長の有限値float mono配列にしてください')
    return audio.reshape(-1)



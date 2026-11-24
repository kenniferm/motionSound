"""Import the verified FABIAN release and cache effective 48 kHz filters."""
import json
import math
from pathlib import Path
import shutil

import numpy as np
from scipy.signal import fftconvolve, resample_poly
import sofar

from ..runner import file_hash
from ..store import write_json
from .scene import RATE

DATASET = 'FABIAN_HRIR_measured_HATO_0'
URL = f'https://sofacoustics.org/data/database/tu-berlin/{DATASET}.sofa'
SHA256 = '83ebbcd9a09d17679b95d201c9775438c0bb1199d565c3fc7a25448a905cdc3c'


def spherical(angles):
    azimuth, elevation = np.deg2rad(np.asarray(angles)).T
    return np.column_stack((np.cos(elevation) * np.cos(azimuth),
                            np.cos(elevation) * np.sin(azimuth), np.sin(elevation)))


def resample_ir(ir, rate):
    divisor = math.gcd(RATE, rate)
    return resample_poly(ir, RATE // divisor, rate // divisor, axis=-1) * (rate / RATE)


def apply_delay(ir, delay):
    """Apply additional delays in target samples once, preserving interaural timing."""
    delay = np.broadcast_to(delay, ir.shape[:2])
    if not np.isfinite(delay).all() or np.any(delay < 0) or np.any(delay > RATE):
        raise ValueError('Data.Delayが対応範囲外です')
    integer = np.floor(delay).astype(int)
    fraction = delay - integer
    half = 16 if np.any(fraction > 1e-9) else 0
    if not half and not integer.any():
        return ir, 0
    result = np.zeros((*ir.shape[:2], ir.shape[2] + integer.max() + 2 * half))
    for i, j in np.ndindex(delay.shape):
        taps = ir[i, j]
        if half:
            kernel = np.sinc(np.arange(-half, half+1) - fraction[i, j]) * np.hanning(2*half+1)
            taps = fftconvolve(taps, kernel / kernel.sum())
        start = integer[i, j]
        result[i, j, start:start+len(taps)] = taps
    return result, half


def import_dataset(source, root):
    source = Path(source)
    if file_hash(source) != SHA256:
        raise ValueError('FABIANのSHA-256が確認済み公式ファイルと一致しません')
    sofa = sofar.read_sofa(str(source))
    if (sofa.GLOBAL_SOFAConventions != 'SimpleFreeFieldHRIR' or sofa.GLOBAL_DataType != 'FIR'
            or sofa.SourcePosition_Type != 'spherical'
            or [s.strip() for s in sofa.SourcePosition_Units.split(',')] != ['degree', 'degree', 'metre']
            or sofa.ReceiverPosition_Type != 'cartesian' or sofa.ReceiverPosition_Units != 'metre'
            or sofa.ListenerView_Type != 'cartesian' or sofa.Data_SamplingRate_Units != 'hertz'
            or not np.array_equal(sofa.ListenerPosition, [[0, 0, 0]])
            or not np.array_equal(sofa.ListenerView, [[1, 0, 0]])
            or not np.array_equal(sofa.ListenerUp, [[0, 0, 1]])
            or np.any(sofa.EmitterPosition)
            or 'creativecommons.org/licenses/by/4.0' not in sofa.GLOBAL_License):
        raise ValueError('SOFAの座標・単位・convention・ライセンスが想定と異なります')
    ir = np.asarray(sofa.Data_IR, dtype=float)
    positions = np.asarray(sofa.SourcePosition)
    receivers = np.asarray(sofa.ReceiverPosition).reshape(2, 3)
    if (ir.shape != (len(positions), 2, 256) or not np.isfinite(ir).all()
            or not np.isfinite(positions).all() or not np.isfinite(receivers).all()
            or not np.allclose(positions[:, 2], 1.7)
            or not (receivers[:, 1].min() < 0 < receivers[:, 1].max())):
        raise ValueError('SOFAのIR・耳位置・測定半径が想定と異なります')
    order = np.argsort(-receivers[:, 1])
    rate = int(sofa.Data_SamplingRate)
    if rate != 44100:
        raise ValueError('FABIANの元サンプルレートが想定と異なります')
    delay = np.asarray(sofa.Data_Delay)
    effective, latency = apply_delay(resample_ir(ir[:, order], rate), delay[..., order] * RATE / rate)
    folder = Path(root) / 'datasets' / DATASET
    folder.mkdir(parents=True)
    original = folder / f'{DATASET}.sofa'
    if not original.exists():
        with source.open('rb') as src, original.open('xb') as dest:
            shutil.copyfileobj(src, dest)
    if file_hash(original) != SHA256:
        raise ValueError('保存済みSOFAのハッシュが一致しません')
    cache = folder / 'hrir_48000.npz'
    with cache.open('xb') as f:
        np.savez(f, directions=spherical(positions[:, :2]), filters=effective)
    metadata = {
        'id': DATASET, 'source': URL, 'sha256': SHA256, 'cache_sha256': file_hash(cache),
        'license': 'CC BY 4.0', 'license_url': 'https://creativecommons.org/licenses/by/4.0/',
        'credit': 'Brinkmann et al., FABIAN HRIR database, doi:10.17743/jaes.2017.0033; doi:10.14279/depositonce-5718.4',
        'documentation': 'https://sofacoustics.org/data/database/tu-berlin/FABIAN_Documentation.pdf',
        'title': sofa.GLOBAL_Title, 'origin': sofa.GLOBAL_Origin, 'scope': sofa.GLOBAL_Comment,
        'hato_degrees': 0, 'measurement_radius_m': 1.7, 'convention': sofa.GLOBAL_SOFAConventions,
        'positions': len(positions), 'original_rate': rate, 'sample_rate': RATE,
        'original_taps': ir.shape[-1], 'effective_taps': effective.shape[-1],
        'receiver_positions_m': receivers.tolist(), 'ear_order': ['left', 'right'],
        'original_receiver_indices': order.tolist(), 'source_position_units': sofa.SourcePosition_Units,
        'additional_delay_original_samples': delay.tolist(),
        'additional_delay_seconds': (delay / rate).tolist(),
        'common_fractional_delay_samples': latency,
        'processing': 'SciPy resample_poly 160/147 × 147/160; additional delay applied once; no EQ or gain normalization',
    }
    write_json(folder / 'manifest.json', metadata)
    return metadata


def load_dataset(root):
    folder = Path(root) / 'datasets' / DATASET
    manifest_path = folder / 'manifest.json'
    if not manifest_path.is_file():
        raise ValueError('実測FABIANデータが未導入です。docs/FABIAN.md を参照してください')
    metadata = json.loads(manifest_path.read_text())
    cache = folder / 'hrir_48000.npz'
    if (metadata['sha256'] != SHA256 or file_hash(folder / f'{DATASET}.sofa') != SHA256
            or file_hash(cache) != metadata['cache_sha256']):
        raise ValueError('FABIANデータまたはキャッシュのハッシュが一致しません')
    with np.load(cache, allow_pickle=False) as data:
        return data['directions'], data['filters'], metadata

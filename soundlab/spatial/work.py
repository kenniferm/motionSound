"""Render planned events into one finished headphone work."""
import numpy as np
from ..cancellation import check

from .scene import RATE, Source, read_mono
from .render import moving
from .dataset import load_dataset
from ..runner import convert, file_hash


DIRECT_REFERENCE_DISTANCE = 1.7
DIRECT_MAX_GAIN = 2.


def validate_clips(folder, events, expected_hashes=None):
    clips = folder / 'events'
    if clips.is_symlink() or not clips.is_dir():
        raise ValueError('発音素材の保存先が不正です')
    hashes = {}
    for event in events:
        check()
        path = clips / (event['id'] + '.npy')
        read_mono(path, clips, event['frames'])
        hashes[event['id']] = file_hash(path)
    if expected_hashes is not None and hashes != expected_hashes:
        raise ValueError('保存した発音素材が変更されています')
    return hashes


def mix_work(folder, clips, events, frames, root):
    directions, filters, dataset = load_dataset(root)
    mix = np.zeros((frames, 2), dtype=np.float32)
    wet = np.zeros_like(mix)
    for event in events:
        check()
        path = clips / (event['id'] + '.npy')
        signal = read_mono(path, clips, event['frames'])
        keys = event['keys']
        indexes = np.array([k['frame'] for k in keys])
        vectors = np.array([k['vector'] for k in keys])
        source = Source(signal, indexes, vectors)
        stereo, _ = moving(source, directions, filters)
        start = event['start_frame']
        stereo = stereo[:frames-start]
        depth = np.interp(np.arange(len(stereo)), indexes, [k['distance'] for k in keys])
        target = slice(start, start+len(stereo))
        mix[target] += stereo * np.clip(DIRECT_REFERENCE_DISTANCE/depth, .08, DIRECT_MAX_GAIN)[:, None]
        wet[target] += stereo * event['reverb']
    for seconds, gain, cross in ((.029,.22,False),(.047,.18,True),(.091,.12,False),(.167,.08,True),(.311,.04,False)):
        shift = round(seconds*RATE)
        if shift < frames:
            mix[shift:] += gain * (wet[:-shift, ::-1] if cross else wet[:-shift])
    mix[-4800:] *= np.linspace(1, 0, 4800)[:, None]
    mix *= min(1., .95/max(.95, float(np.max(np.abs(mix)))))
    return encode_mix(folder, mix), dataset


def dry_mix(folder, events, frames):
    mix = np.zeros(frames, dtype=np.float32)
    clips = folder / 'events'
    for event in events:
        check()
        start = event['start_frame']
        end = start + event['frames']
        mix[start:end] += read_mono(clips / (event['id'] + '.npy'), clips, event['frames'])
    mix *= min(1., .95/max(.95, float(np.max(np.abs(mix)))))
    return encode_mix(folder, np.column_stack((mix, mix)))


def encode_mix(folder, mix):
    path = folder / 'work.npy'
    with path.open('xb') as output:
        np.save(output, mix, allow_pickle=False)
    try:
        return convert(path, folder, {'duration_seconds': len(mix)//RATE, 'sample_rate': RATE})
    finally:
        path.unlink()

"""Run the production API against mock responses and test-only saved media."""
import asyncio
import json
import os
from pathlib import Path
import socket

import numpy as np
from scipy.io.wavfile import write

from conftest import mock_client
from soundlab import sound_first, structured_api
from soundlab.api import create_app
from soundlab.store import Store, now, uid


for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(name, None)
os.environ['OPENAI_MODEL'] = 'browser-mock'
os.environ['SOUNDLAB_SOUND_GENERATION_MODE'] = 'direct'

original_connect = socket.socket.connect


def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
        raise RuntimeError('External connections are forbidden in browser tests')
    return original_connect(sock, address)


socket.socket.connect = local_connect
root = Path(os.environ['SOUNDLAB_DATA_DIR'])
client = mock_client([])
reply = client.responses.parse


async def controlled_reply(**kwargs):
    payload = json.loads(kwargs['input'][1]['content'])
    if kwargs['text_format'] is sound_first.DirectProposal:
        prompt = payload['prompt']
        if prompt == '先行試聴テスト' and payload['candidate_number'] > 1:
            async with asyncio.timeout(45):
                while not (root/'release-later').is_file():
                    await asyncio.sleep(.1)
        if prompt == '削除待機テスト':
            await asyncio.sleep(60)
    return reply(**kwargs)


client.responses.parse = controlled_reply
structured_api.resolve_client = lambda override: ('browser-mock', override or client)

store = Store(root)
rid, cid, run_id = uid(), uid(), uid()
filename = root/'runs'/run_id/'work.wav'
filename.parent.mkdir(parents=True)
signal = .1 * np.sin(2*np.pi*220*np.arange(5 * 48000)/48000)
write(filename, 48000, np.column_stack((signal, signal)).astype(np.float32))
store.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?,?)',
              (rid, store.project('旧作品'), None, '旧WAVの部屋', 'demo', 'completed',
               '{"render_context":{"duration_seconds":5}}', None, now()))
store.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?)', (cid, rid, 0, '保存済みWAV', '', '[]'))
store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
              (run_id, cid, 'succeeded', '{}', json.dumps({'audio': {
                  'path': str(filename), 'frames': 240000, 'sample_rate': 48000}}), now()))
store.save_audio(run_id)
store.execute('INSERT INTO reviews(id,round_id,candidate_id,text,token,created) VALUES(?,?,?,?,?,?)',
              (uid(), rid, cid, '以前の聴き取りメモ', uid(), now()))
app = create_app(root)

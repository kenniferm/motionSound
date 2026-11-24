"""Real Chromium tests against an isolated, mock-only Starlette server."""
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PROMPT = 'ブラウザ回帰テストの交差する持続音'


@pytest.fixture(scope='session')
def app_server(tmp_path_factory):
    root = tmp_path_factory.mktemp('browser-workspace')
    dataset = ROOT / 'data' / 'datasets'
    if not (dataset / 'FABIAN_HRIR_measured_HATO_0' / 'manifest.json').is_file():
        pytest.fail('Browser regressions require the verified FABIAN dataset')
    (root / 'datasets').symlink_to(dataset, target_is_directory=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items()
           if k in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT', 'LANG', 'LC_ALL', 'VIRTUAL_ENV')}
    env.update(SOUNDLAB_DATA_DIR=str(root), PYTHONPATH=str(ROOT))
    assert (ROOT/'frontend/dist/index.html').is_file(), 'Run npm --prefix frontend run build first'
    with (root / 'server.log').open('w+') as log:
        process = subprocess.Popen([
            sys.executable, '-m', 'uvicorn', 'browser_app:app', '--app-dir', 'tests',
            '--host', '127.0.0.1', '--port', str(port),
        ], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen(url + '/api/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                log.seek(0)
                pytest.fail('Test server failed to start:\n' + log.read())
            yield url, root
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv('SOUNDLAB_BROWSER_EXECUTABLE'), args=['--mute-audio'])
        yield browser
        browser.close()


@pytest.fixture
def page(browser, app_server, request):
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, accept_downloads=True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_default_timeout(30_000)
    page.goto(app_server[0])
    page.get_by_role('button', name='音を生成', exact=True).wait_for()
    yield page
    evidence = Path(os.getenv('SOUNDLAB_BROWSER_EVIDENCE_DIR', app_server[1] / 'evidence'))
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f'{request.node.name}.png'), full_page=True)
    context.close()
    assert not errors, errors


@pytest.fixture(scope='session')
def generated_work(browser, app_server):
    # Generate through the real UI once; each test then opens a fresh browser session.
    with browser.new_context() as context:
        page = context.new_page()
        page.set_default_timeout(60_000)
        page.goto(app_server[0])
        page.get_by_label('音の指示', exact=True).fill(PROMPT)
        page.get_by_label('長さ（秒）', exact=True).fill('5')
        page.get_by_label('生成する数', exact=True).fill('1')
        page.get_by_role('button', name='音を生成', exact=True).click()
        card = page.get_by_role('article', name='空間作品1', exact=True)
        card.get_by_role('button', name='この作品を改善', exact=True).wait_for()
        card.get_by_text('作品の詳細', exact=True).click()
        card.get_by_role('button', name='作品を保存', exact=True).click()
        card.get_by_role('button', name='保存済み', exact=True).wait_for()
    return PROMPT


def rows(root, query):
    with sqlite3.connect(root / 'history.sqlite3') as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query)]

import re

from playwright.sync_api import expect

from browser_tests.conftest import rows


def room_button(page, prompt):
    return page.get_by_role('navigation', name='部屋').get_by_role('button', name=prompt, exact=True)


def open_work(page, prompt, title='空間作品1'):
    room_button(page, prompt).click()
    round_ = page.locator('.round').filter(has=page.locator('.round-title').get_by_text(prompt, exact=True))
    card = round_.get_by_role('article', name=title, exact=True)
    card.get_by_role('button', name=title + 'を再生', exact=True).wait_for()
    return card


def generate(page, prompt, count='1'):
    page.get_by_label('音の指示', exact=True).fill(prompt)
    page.get_by_label('長さ（秒）', exact=True).fill('5')
    page.get_by_label('生成する数', exact=True).fill(count)
    with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/api/rooms')) as submitted:
        page.get_by_role('button', name='音を生成', exact=True).click()
    assert submitted.value.status == 202
    return submitted.value.json()['roomId']


def test_home_requires_explicit_generation_and_phone_history_can_be_opened(page, app_server):
    before = len(rows(app_server[1], 'SELECT id FROM rounds'))
    expect(page.get_by_label('長さ（秒）', exact=True)).to_have_value('60')
    expect(page.get_by_label('生成する数', exact=True)).to_have_value('1')
    page.get_by_label('音の指示', exact=True).fill('入力だけでは生成しない')
    page.get_by_role('button', name='新しく生成', exact=True).click()
    expect(page.get_by_label('音の指示', exact=True)).to_be_empty()
    assert len(rows(app_server[1], 'SELECT id FROM rounds')) == before
    page.set_viewport_size({'width': 390, 'height': 800})
    expect(room_button(page, '旧WAVの部屋')).not_to_be_visible()
    toggle = page.get_by_role('button', name='生成履歴', exact=True)
    toggle.click()
    dialog = page.get_by_role('dialog', name='生成履歴', exact=True)
    expect(dialog).to_be_visible()
    for _ in range(15):
        page.keyboard.press('Tab')
        assert dialog.evaluate("(dialog) => dialog.contains(document.activeElement) || document.activeElement === document.body")
    page.keyboard.press('Escape')
    expect(dialog).not_to_be_visible()
    expect(toggle).to_be_focused()
    toggle.click()
    expect(room_button(page, '旧WAVの部屋')).to_be_visible()
    room_button(page, '旧WAVの部屋').click()
    expect(room_button(page, '旧WAVの部屋')).not_to_be_visible()
    card = page.get_by_role('article', name='保存済みWAV', exact=True)
    expect(card).to_be_visible()
    assert page.locator('.composer').bounding_box()['y'] < page.locator('.results').bounding_box()['y']
    assert page.evaluate('document.documentElement.scrollWidth') == 390


def test_early_playback_seek_and_room_change_with_one_player(page, app_server):
    rid = generate(page, '先行試聴テスト', '2')
    try:
        history = page.get_by_role('navigation', name='部屋')
        expect(history.locator('.history-item').first).to_contain_text('先行試聴テスト')
        expect(page).to_have_url(re.compile(r'room=' + rid))
        card = page.get_by_role('article', name='空間作品1', exact=True)
        card.get_by_role('button', name='空間作品1を再生', exact=True).click()
        expect(page.get_by_role('progressbar')).to_be_visible()
        expect(page.locator('article[data-state="ready"]')).to_have_count(1)
        audio = page.locator('audio')
        assert audio.count() == 1
        page.wait_for_function('document.querySelector("audio").currentTime > .1')
        seek = page.get_by_label('再生位置', exact=True)
        expect(seek).to_be_enabled()
        expect(card.locator('.waveform')).to_have_count(0)
        expect(page.locator('.seek-track svg rect').first).to_be_attached()
        box = seek.bounding_box()
        seek.click(position={'x': box['width'] * .35, 'y': box['height']/2})
        page.wait_for_function('document.querySelector("audio").currentTime > 1.3')
        before = audio.evaluate('(audio) => [audio.currentSrc, audio.currentTime]')
        old_room = room_button(page, '旧WAVの部屋')
        old_room.click()
        expect(page.get_by_role('article', name='保存済みWAV', exact=True)).to_be_visible()
        page.wait_for_function('(time) => document.querySelector("audio").currentTime > time', arg=before[1])
        assert audio.evaluate('(audio) => audio.currentSrc') == before[0]
        assert room_button(page, '先行試聴テスト').is_visible()
    finally:
        (app_server[1]/'release-later').touch()
    expect(page.get_by_role('button', name='次の音源', exact=True)).to_be_enabled(timeout=20_000)
    page.get_by_role('button', name='次の音源', exact=True).click()
    expect(page.get_by_role('contentinfo', name='共通プレーヤー')).to_contain_text('空間作品2')
    expect(page.get_by_role('article', name='保存済みWAV', exact=True)).to_be_visible()
    page.get_by_role('button', name='前の音源', exact=True).click()
    expect(page.get_by_role('contentinfo', name='共通プレーヤー')).to_contain_text('空間作品1')


def test_generation_stages_3d_and_mp3_and_legacy_wav_download(page, app_server, generated_work):
    card = open_work(page, generated_work)
    url, root = app_server
    room_id = page.url.split('room=')[-1]
    detail = page.request.get(url + '/api/rooms/' + room_id).json()
    candidate = detail['rounds'][0]['candidates'][0]
    track = next(preview for preview in candidate['previews'] if preview['stage'] == 'spatial')
    _, cid, scene, attempt = track['id'].split('.')
    (root/'candidates'/cid/'layers'/'spatial'/scene/'previews'/attempt/'work.mp3').unlink()
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_text('元の指示', exact=True)).to_have_count(0)
    expect(card.get_by_text('制作方針', exact=True)).to_be_visible()
    card.get_by_role('button', name='空間設計の試聴音を復元', exact=True).click()
    for stage in ('原音', '空間設計', 'アレンジ後の完成版'):
        card.get_by_role('button', name=stage + 'を再生', exact=True).click()
        expect(page.get_by_role('contentinfo', name='共通プレーヤー')).to_contain_text(stage)
    card.get_by_role('button', name='空間作品1の3D音源ビュー', exact=True).click()
    dialog = page.get_by_role('dialog', name='3D音源ビュー', exact=True)
    expect(dialog.get_by_role('img', name='3D空間での直達音の広がりの推定')).to_be_visible()
    dialog.get_by_role('combobox', name='表示', exact=True).select_option('wave')
    dialog.get_by_role('button', name='拡大', exact=True).click()
    dialog.get_by_role('button', name='閉じる', exact=True).click()
    expect(dialog).not_to_be_visible()
    with page.expect_download() as mp3:
        card.get_by_role('link', name='空間作品1をダウンロード', exact=True).click()
    assert mp3.value.suggested_filename.endswith('.mp3')
    assert mp3.value.path().stat().st_size > 1000
    legacy = open_work(page, '旧WAVの部屋', '保存済みWAV')
    legacy.get_by_role('button', name='保存済みWAVを再生', exact=True).click()
    page.wait_for_function('document.querySelector("audio").currentTime > .1')
    with page.expect_download() as wav:
        legacy.get_by_role('link', name='保存済みWAVをダウンロード', exact=True).click()
    assert wav.value.suggested_filename.endswith('.wav')
    with wav.value.path().open('rb') as file:
        assert file.read(4) == b'RIFF'
    expect(page.locator('audio')).to_have_count(1)


def test_saved_work_keeps_review_and_improvement_in_same_room(page, app_server, generated_work):
    card = open_work(page, generated_work)
    card.get_by_role('button', name='空間作品1を再生', exact=True).click()
    page.wait_for_function('document.querySelector("audio").currentTime > .1')
    run_id = page.locator('audio').evaluate('(audio) => audio.currentSrc.split("/").pop()')
    card.get_by_text('作品の詳細', exact=True).click()
    card.get_by_role('button', name='レビュー', exact=True).click()
    card.get_by_label('聴いた感想', exact=True).fill('ブラウザから保存した空間の感想')
    card.get_by_text('詳しく評価', exact=True).click()
    card.get_by_label('この音は好みですか？', exact=True).select_option('liked')
    notes = card.get_by_role('textbox', name='聴いた感想', exact=True)
    card.get_by_text('作品の詳細', exact=True).click()
    expect(notes).not_to_be_visible()
    card.get_by_text('作品の詳細', exact=True).click()
    expect(notes).to_be_visible()
    expect(notes).to_have_value('ブラウザから保存した空間の感想')
    expect(card.get_by_label('この音は好みですか？', exact=True)).to_have_value('liked')
    card.get_by_role('button', name='レビューを保存', exact=True).click()
    expect(card.get_by_text('レビューを保存しました', exact=True)).to_be_visible()
    assert any(review['text'] == 'ブラウザから保存した空間の感想' and review['run_id'] == run_id
               for review in rows(app_server[1], 'SELECT text,run_id FROM reviews'))
    page.reload()
    page.get_by_role('button', name='保存した作品', exact=True).click()
    saved = page.get_by_role('article', name='空間作品1', exact=True)
    saved.get_by_text('作品の詳細', exact=True).click()
    saved.get_by_role('button', name='レビュー', exact=True).click()
    expect(saved.get_by_text('ブラウザから保存した空間の感想', exact=True)).to_be_visible()
    expect(saved.get_by_text('この音は好みですか？ · 好き', exact=True)).to_be_visible()
    saved.get_by_role('button', name='この作品を改善', exact=True).click()
    page.get_by_label('作品全体を作り直す', exact=True).check()
    page.get_by_label('どう改善したい？', exact=True).fill('明るい余韻に変える')
    page.get_by_label('生成する数', exact=True).fill('1')
    with page.expect_response(lambda response: response.url.endswith('/improve')) as submitted:
        page.get_by_role('button', name='改善した作品を生成', exact=True).click()
    assert submitted.value.status == 202
    room_id = next(row['round_id'] for row in rows(app_server[1], 'SELECT round_id,run_id FROM reviews')
                   if row['run_id'] == run_id)
    expect(page).to_have_url(re.compile(r'room=' + room_id))
    expect(page.locator('article[data-state="ready"]')).to_have_count(2, timeout=35_000)
    expect(page.locator('.round-title')).to_have_text([generated_work, '明るい余韻に変える'])


def test_round_redo_undo_and_room_rename(page, app_server):
    rid = generate(page, 'やり直し前の指示')
    card = page.get_by_role('article', name='空間作品1', exact=True)
    card.get_by_role('button', name='空間作品1を再生', exact=True).click()
    page.wait_for_function('document.querySelector("audio").currentTime > .1')
    previous = page.locator('audio').evaluate('(audio) => audio.currentSrc')
    page.get_by_role('button', name='指示を編集', exact=True).click()
    expect(page.get_by_label('音の指示', exact=True)).to_have_value('やり直し前の指示')
    page.get_by_label('音の指示', exact=True).fill('編集した指示')
    page.get_by_role('button', name='この指示でやり直す', exact=True).click()
    expect(page.locator('.player-error')).to_contain_text('置き換えられました')
    expect(page.locator('audio')).not_to_have_attribute('src', re.compile('.+'))
    expect(page.locator('.round-title')).to_have_text(['編集した指示'])
    page.get_by_role('button', name='ひとつ前に戻す', exact=True).click()
    expect(page.locator('.round-title')).to_have_text(['やり直し前の指示'])
    card.get_by_role('button', name='空間作品1を再生', exact=True).click()
    page.wait_for_function('(url) => document.querySelector("audio").currentSrc === url', arg=previous)
    page.get_by_role('button', name='やり直し前の指示の名前を変更', exact=True).click()
    page.get_by_label('新しい名前').fill('名前を変えた部屋')
    page.get_by_role('complementary', name='生成履歴').get_by_role('button', name='保存', exact=True).click()
    expect(room_button(page, '名前を変えた部屋')).to_have_attribute('aria-current', 'page')
    assert page.url.endswith(rid)


def test_delete_a_running_room(page, app_server):
    rid = generate(page, '削除待機テスト')
    expect(room_button(page, '削除待機テスト')).to_be_visible()
    expect(page.get_by_role('progressbar')).to_be_visible()
    page.get_by_role('button', name='削除待機テストを削除', exact=True).click()
    expect(room_button(page, '削除待機テスト')).not_to_be_visible()
    assert page.request.get(app_server[0] + '/api/rooms/' + rid).status == 404
    page.wait_for_function("async () => !(await (await fetch('/api/rooms')).json()).rooms.some(room => room.title === '削除待機テスト')")
    page.wait_for_function("async rid => (await fetch('/api/rooms/' + rid)).status === 404", arg=rid)


def test_late_acceptance_keeps_a_new_draft_and_does_not_leak_edit_settings(page, generated_work):
    card = open_work(page, generated_work)
    card.get_by_role('button', name='この作品を改善', exact=True).click()
    expect(page.get_by_label('長さ（秒）', exact=True)).not_to_be_editable()
    page.get_by_label('作品全体を作り直す', exact=True).check()
    expect(page.get_by_label('長さ（秒）', exact=True)).to_be_editable()
    page.get_by_label('どう改善したい？').fill('前の部屋で編集した指示')
    page.get_by_label('長さ（秒）').fill('15')
    room_button(page, '旧WAVの部屋').click()
    expect(page.get_by_label('音の指示')).to_be_empty()
    expect(page.get_by_label('長さ（秒）')).to_have_value('60')
    page.get_by_role('article', name='保存済みWAV').get_by_role('button', name='この作品を改善').click()
    expect(page.get_by_label('作品全体を作り直す', exact=True)).to_be_checked()
    expect(page.get_by_label('作品全体を作り直す', exact=True)).to_be_disabled()

    page.get_by_role('button', name='新しく生成', exact=True).click()
    pending = []

    def hold_submission(route):
        if route.request.method == 'POST':
            pending.append(route)
        else:
            route.continue_()

    page.route('**/api/rooms', hold_submission)
    page.get_by_label('音の指示').fill('応答待ちの指示')
    page.get_by_label('長さ（秒）').fill('5')
    page.get_by_label('生成する数').fill('1')
    with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/api/rooms')):
        page.get_by_role('button', name='音を生成', exact=True).click()
    room_button(page, '旧WAVの部屋').click()
    page.get_by_label('音の指示').fill('別の下書き')
    assert pending
    with page.expect_response(lambda reply: reply.request.method == 'POST' and reply.url.endswith('/api/rooms')) as accepted:
        pending[0].continue_()
    assert accepted.value.status == 202
    expect(room_button(page, '応答待ちの指示')).to_be_visible()
    expect(room_button(page, '旧WAVの部屋')).to_have_attribute('aria-current', 'page')
    expect(page.get_by_label('音の指示')).to_have_value('別の下書き')


def test_delayed_room_deletion_keeps_other_rooms_audio(page):
    rid = generate(page, '遅れて削除される部屋')
    first = page.get_by_role('article', name='空間作品1')
    first.get_by_role('button', name='空間作品1を再生').click()
    pending = []

    def hold_deletion(route):
        if route.request.method == 'DELETE':
            pending.append(route)
        else:
            route.continue_()

    page.route('**/api/rooms/' + rid, hold_deletion)
    with page.expect_request(lambda request: request.method == 'DELETE' and request.url.endswith(rid)):
        page.get_by_role('button', name='遅れて削除される部屋を削除').click()
    other = open_work(page, '旧WAVの部屋', '保存済みWAV')
    other.get_by_role('button', name='保存済みWAVを再生').click()
    page.wait_for_function('document.querySelector("audio").currentTime > .1')
    audio = page.locator('audio')
    other_source = audio.evaluate('(audio) => audio.currentSrc')
    with page.expect_response(lambda reply: reply.request.method == 'DELETE' and reply.url.endswith(rid)):
        pending[0].continue_()
    expect(room_button(page, '遅れて削除される部屋')).not_to_be_visible()
    expect(room_button(page, '旧WAVの部屋')).to_have_attribute('aria-current', 'page')
    assert audio.evaluate('(audio) => audio.currentSrc') == other_source


def test_edited_review_is_saved_after_first_response_was_lost(page, app_server):
    card = open_work(page, '旧WAVの部屋', '保存済みWAV')
    expect(card.get_by_text(re.compile(r'^デモ · '))).to_be_visible()
    card.get_by_text('作品の詳細', exact=True).click()
    card.get_by_role('button', name='レビュー', exact=True).click()
    legacy = card.locator('.past-review').filter(has_text='以前の聴き取りメモ')
    expect(legacy.get_by_text('試聴音源が未記録の旧レビュー', exact=True)).to_be_visible()
    original, revised = '試験用レビューの原文', '試験用レビューの修正版'
    submitted = []

    def lose_first_reply(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        submitted.append(route.request.post_data_json)
        if len(submitted) == 1:
            assert route.fetch().status == 200
            route.fulfill(status=502, json={'error': '応答が途切れました'})
        else:
            route.continue_()

    page.route('**/api/audio/*/reviews', lose_first_reply)
    notes = card.get_by_label('聴いた感想')
    notes.fill(original)
    card.get_by_role('button', name='レビューを保存').click()
    expect(card.get_by_text('応答が途切れました')).to_be_visible()
    notes.fill(revised)
    card.get_by_role('button', name='レビューを保存').click()
    expect(card.get_by_text('レビューを保存しました', exact=True)).to_be_visible()
    expect(card.locator('.past-review').get_by_text(revised, exact=True)).to_be_visible()
    assert submitted[0]['token'] != submitted[1]['token']
    reviews = {row['text'] for row in rows(app_server[1], 'SELECT text FROM reviews')}
    assert {original, revised} <= reviews


def test_late_polish_preserves_manual_prompt_until_it_is_applied(page):
    prompt = page.get_by_label('音の指示', exact=True)
    prompt.fill('低く響く音')
    pending = []
    page.route('**/api/prompt/polish', lambda route: pending.append(route))
    with page.expect_request('**/api/prompt/polish'):
        page.get_by_role('button', name='指示を整える', exact=True).click()
    prompt.fill('低く響く音から急に静まる')
    pending[0].fulfill(status=200, json={'prompt': 'ゆっくりと立ち上がる低音'})
    use = page.get_by_role('button', name='整えた指示を使う', exact=True)
    expect(use).to_be_visible()
    expect(prompt).to_have_value('低く響く音から急に静まる')
    use.click()
    expect(prompt).to_have_value('ゆっくりと立ち上がる低音')

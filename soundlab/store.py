import json
import math
import os
from tempfile import NamedTemporaryFile
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .questionnaire import validate_questionnaire

ID_PATTERN = r'[0-9a-f]{32}'


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    # Readers see a complete file. Hard-link publication also preserves the no-overwrite rule.
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.link(f.name, path)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'history.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
              parent_id TEXT REFERENCES candidates(id), prompt TEXT NOT NULL, mode TEXT NOT NULL,
              status TEXT NOT NULL, context TEXT NOT NULL, error TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
              position INTEGER NOT NULL, title TEXT NOT NULL, approach TEXT NOT NULL, refs TEXT NOT NULL,
              UNIQUE(round_id, position));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id),
              status TEXT NOT NULL, approval TEXT NOT NULL, result TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
              candidate_id TEXT NOT NULL REFERENCES candidates(id), text TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
              created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audio_selections(id TEXT PRIMARY KEY,
              round_id TEXT NOT NULL REFERENCES rounds(id), candidate_id TEXT NOT NULL REFERENCES candidates(id),
              run_id TEXT NOT NULL REFERENCES runs(id), review_id TEXT NOT NULL REFERENCES reviews(id), created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS room_history(round_id TEXT PRIMARY KEY REFERENCES rounds(id),
              name TEXT, trashed_at TEXT);
            CREATE TABLE IF NOT EXISTS saved_audio(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
              created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS name_changes(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
              target_id TEXT NOT NULL, previous_name TEXT NOT NULL, name TEXT NOT NULL, created TEXT NOT NULL);
            ''')
            # Serialize the additive migration, including concurrent studio sessions.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(reviews)')}
            if 'context' not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
            if 'run_id' not in columns:
                db.execute('ALTER TABLE reviews ADD COLUMN run_id TEXT REFERENCES runs(id)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        if not rows:
            raise ValueError('記録が見つかりません')
        return rows[0]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def project(self, name):
        if not name.strip():
            raise ValueError('作品名を入力してください')
        pid = uid()
        self.execute('INSERT INTO projects VALUES(?,?,?)', (pid, name, now()))
        return pid

    def save_audio(self, run_id):
        self.execute("""INSERT OR IGNORE INTO saved_audio(run_id, created)
            SELECT u.id, ? FROM runs u
            JOIN candidates c ON c.id=u.candidate_id JOIN rounds r ON r.id=c.round_id
            WHERE u.id=? AND u.status='succeeded' AND r.status!='superseded'""",
                     (now(), run_id))

    def saved_audio(self):
        return self.rows("""SELECT u.id
            FROM saved_audio s JOIN runs u ON u.id=s.run_id
            JOIN candidates c ON c.id=u.candidate_id JOIN rounds r ON r.id=c.round_id
            WHERE r.status!='superseded' AND u.status='succeeded'
            ORDER BY s.created DESC, u.id DESC""")

    def candidate_dir(self, cid):
        # IDs originate in SQLite, not in generated artifact names.
        self.one('SELECT id FROM candidates WHERE id=?', (cid,))
        return self.root / 'candidates' / cid

    def rename(self, kind, target_id, name):
        tables = {'project': ('projects', 'name'), 'audio': ('candidates', 'title')}
        if kind not in tables or not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError('名前は1〜120文字で入力してください')
        table, column = tables[kind]
        with self.connect() as db:
            row = db.execute(f'SELECT {column} FROM {table} WHERE id=?', (target_id,)).fetchone()
            if not row:
                raise ValueError('名前を変更する対象が見つかりません')
            name = name.strip()
            if row[0] != name:
                db.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (name, target_id))
                db.execute('INSERT INTO name_changes VALUES(?,?,?,?,?,?)', (uid(),kind,target_id,row[0],name,now()))

    def review(self, rid, cid, text, token, context=None, *, run_id=None):
        context = dict(context or {})
        questionnaire = context.get('questionnaire')
        if 'questionnaire' in context:
            validate_questionnaire(questionnaire)
        if not text.strip() and questionnaire is None:
            raise ValueError('質問に答えるか、感想を入力してください')
        self.one('SELECT id FROM candidates WHERE id=? AND round_id=?', (cid, rid))
        runs = self.rows("SELECT * FROM runs WHERE candidate_id=? AND status='succeeded' ORDER BY created DESC,id DESC", (cid,))
        if not runs:
            raise ValueError('生成に成功した候補を選んでください')
        run = next((run for run in runs if run_id is None or run['id'] == run_id), None)
        if run is None:
            raise ValueError('レビュー対象の音声がこの候補と一致しません')
        run_id = run['id']
        if context.get('playback_mode', 'unknown') not in ('unknown', 'headphones', 'stereo_speakers'):
            raise ValueError('試聴環境が不正です')
        if context.get('normalization', 'unknown') not in ('unknown', 'not_applied', 'external', 'app_rms_matched'):
            raise ValueError('音量調整の記録が不正です')
        for field in ('equipment', 'perceived_direction', 'height', 'distance', 'objecthood', 'uncertainty'):
            if field in context and not isinstance(context[field], str):
                raise ValueError('試聴メモは文字列で記録してください')
        start, end = context.get('time_start'), context.get('time_end')
        if start is not None or end is not None:
            if (isinstance(start, bool) or not isinstance(start, (int, float))
                    or isinstance(end, bool) or not isinstance(end, (int, float))):
                raise ValueError('対象区間の開始・終了を両方指定してください')
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError('対象区間の開始・終了は有限の数値で指定してください')
            audio = json.loads(run['result'] or '{}').get('audio') or {}
            if audio.get('sample_rate') and audio.get('frames') is not None:
                duration = audio['frames']/audio['sample_rate']
            else:
                request_path = self.root/'runs'/run_id/'request.json'
                if not request_path.exists():
                    request_path = self.candidate_dir(cid)/'request.json'
                duration = json.loads(request_path.read_text())['duration_seconds']
            if not 0 <= start < end <= duration:
                raise ValueError('対象区間は音源の長さの範囲で指定してください')
        review_id = uid()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM reviews WHERE token=?', (token,)).fetchone()
            if existing:
                if (existing['round_id'], existing['candidate_id'], existing['run_id']) != (rid, cid, run_id):
                    raise ValueError('レビューの保存先が一致しません')
                if existing['text'] != text or json.loads(existing['context']) != context:
                    raise ValueError('同じ送信IDのレビュー内容が一致しません')
                return existing['id']
            db.execute('INSERT INTO reviews(id,round_id,candidate_id,text,token,created,context,run_id) VALUES(?,?,?,?,?,?,?,?)',
                       (review_id, rid, cid, text, token, now(), json.dumps(context, ensure_ascii=False), run_id))
            return review_id

"""Permanent deletion of production room records and owned files."""
import re
import shutil

from .store import ID_PATTERN


def deletion_requested(root, rid):
    return (root / '.room_deletions' / rid).is_file()


def request_room_deletion(root, rid):
    if not re.fullmatch(ID_PATTERN, rid):
        raise ValueError('保存されたIDが不正です')
    folder = root / '.room_deletions'
    folder.mkdir(exist_ok=True)
    (folder / rid).touch()


def finish_room_deletion(studio, rid):
    store = studio.store
    with studio.operation(rid, allow_missing_round=True):
        if not deletion_requested(store.root, rid):
            return
        if store.rows('SELECT id FROM rounds WHERE id=?', (rid,)):
            delete_room(store, rid)
        (store.root / '.room_deletions' / rid).unlink(missing_ok=True)


def delete_room(store, rid):
    """Delete the room, retaining independent continuation rounds."""
    with store.connect() as db:
        db.execute('BEGIN')
        row = db.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise ValueError('部屋が見つかりません')
        if row['status'] in ('planning', 'rendering'):
            raise ValueError('生成が終わってから削除してください')
        inline = [row[0] for row in db.execute(
            "SELECT id FROM rounds WHERE json_extract(context, '$.inline_room_id')=?", (rid,))]
        round_ids = (*inline, rid)
        placeholders = ','.join('?' for _ in round_ids)
        candidates = [row[0] for row in db.execute(
            f'SELECT id FROM candidates WHERE round_id IN ({placeholders})', round_ids)]
        runs = [row[0] for row in db.execute(
            f'''SELECT r.id FROM runs r JOIN candidates c ON c.id=r.candidate_id
                WHERE c.round_id IN ({placeholders})''', round_ids)]
        paths = [*[('rounds', item) for item in round_ids], *[('candidates', cid) for cid in candidates],
                 *[('runs', run) for run in runs]]
        if any(not re.fullmatch(ID_PATTERN, item) for _, item in paths):
            raise ValueError('保存されたIDが不正なため削除できません')
        if any((store.root / category).is_symlink() for category, _ in paths):
            raise OSError('外部にリンクされた保存先は自動削除できません')

    # Keep the IDs in SQLite until all files are gone, including after an interruption.
    for category, item in paths:
        parent = store.root / category
        path = parent / item
        if parent.is_symlink():
            raise OSError('外部にリンクされた保存先は自動削除できません')
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for round_id in round_ids:
            db.execute('DELETE FROM audio_selections WHERE round_id=?', (round_id,))
            db.execute('DELETE FROM reviews WHERE round_id=?', (round_id,))
            db.execute('DELETE FROM runs WHERE candidate_id IN (SELECT id FROM candidates WHERE round_id=?)', (round_id,))
            db.execute('UPDATE rounds SET parent_id=NULL WHERE parent_id IN (SELECT id FROM candidates WHERE round_id=?)', (round_id,))
            db.execute("DELETE FROM name_changes WHERE kind='audio' AND target_id IN (SELECT id FROM candidates WHERE round_id=?)", (round_id,))
            db.execute('DELETE FROM candidates WHERE round_id=?', (round_id,))
            db.execute('DELETE FROM room_history WHERE round_id=?', (round_id,))
            db.execute('DELETE FROM rounds WHERE id=?', (round_id,))

"""Display metadata for production rooms, separate from their audio and reviews."""


def room_title(row):
    return row['name'] or row['prompt'].strip().partition('\n')[0][:80] or '無題'


class RoomHistory:
    def __init__(self, store):
        self.store = store

    def list(self):
        return self.store.rows('''SELECT r.id,r.prompt,r.created,h.name
            FROM rounds r LEFT JOIN room_history h ON h.round_id=r.id
            WHERE json_extract(r.context, '$.inline_room_id') IS NULL AND h.trashed_at IS NULL
            ORDER BY r.created DESC,r.id DESC''')

    def steps(self, room_id):
        return self.store.rows('''SELECT r.*, slot.id AS slot_id
            FROM rounds r JOIN rounds slot
              ON slot.id=COALESCE(json_extract(r.context, '$.prompt_slot_id'),r.id)
            WHERE (r.id=? OR json_extract(r.context, '$.inline_room_id')=?)
              AND r.status!='superseded'
            ORDER BY CASE WHEN slot.id=? THEN 0 ELSE 1 END,slot.created,slot.id''',
            (room_id, room_id, room_id))

    def rename(self, rid, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError('部屋名を1〜80文字で入力してください')
        self.store.execute('''INSERT INTO room_history(round_id,name) VALUES(?,?)
            ON CONFLICT(round_id) DO UPDATE SET name=excluded.name''', (rid, name))

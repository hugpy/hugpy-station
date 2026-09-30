"""C -> B -> A arbitration. Original operator messages remain immutable."""
import json
import time


def lane(text):
    value = text.lstrip()
    if value.startswith("!"):
        return "interrupt", value[1:].lstrip()
    if value.startswith("+"):
        return "append", value[1:].lstrip()
    if value.startswith("?"):
        return "capture", value[1:].lstrip()
    if value.lower().startswith("todo:"):
        return "capture", value[5:].lstrip()
    return "coalesce", text


class Arbitration:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS arbitration (
                turn_id TEXT PRIMARY KEY, lane TEXT NOT NULL, merged_into TEXT DEFAULT '',
                mediated INTEGER NOT NULL DEFAULT 0, reconcile TEXT DEFAULT '')""")

    def intake(self, seat):
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT id,prompt FROM turns WHERE seat=? AND status='queued'", (seat,)).fetchall():
                disposition, _ = lane(row['prompt'])
                c.execute("INSERT OR IGNORE INTO arbitration(turn_id,lane) VALUES (?,?)", (row['id'], disposition))

    def controls(self, seat):
        self.intake(seat)
        with self.store.connect() as c:
            return [dict(r) for r in c.execute("""SELECT t.*,a.lane FROM turns t JOIN arbitration a ON a.turn_id=t.id
                WHERE t.seat=? AND t.status='queued' AND a.lane IN ('capture','interrupt') ORDER BY t.created,t.rowid""", (seat,))]

    def reconcile(self, turn, aborted):
        note = ("C explicitly interrupted turn " + aborted['id'] + ". The workspace may contain partial changes. "
                "Read the aborted prompt if needed: " + aborted['context'] + ". Verify workspace state before continuing.") if aborted else (
                "C selected interrupt after the active turn finished; this request was coalesced instead.")
        with self.store.connect() as c:
            c.execute("UPDATE arbitration SET lane='coalesce',reconcile=? WHERE turn_id=?", (note, turn))
            row = c.execute("SELECT seat,prompt FROM turns WHERE id=?", (turn,)).fetchone()
            has_files = c.execute("SELECT 1 FROM objects WHERE turn_id=? AND kind='attachment'", (turn,)).fetchone()
            if not lane(row['prompt'])[1].strip() and not has_files:
                c.execute("UPDATE turns SET status='held',updated=? WHERE id=?", (time.time(), turn))
                note += ' Reconcile brief held until the next operator message.'
            self.store._event(c, row['seat'], turn, 'status', note)

    def captured(self, turn, reference):
        with self.store.connect() as c:
            row = c.execute("SELECT seat FROM turns WHERE id=?", (turn,)).fetchone()
            c.execute("UPDATE turns SET status='captured',updated=? WHERE id=?", (time.time(), turn))
            self.store._event(c, row['seat'], turn, 'status', 'Captured on the board; no A turn', {'board': reference})

    def batch(self, seat, connection=None):
        if connection is None:
            self.intake(seat)
            with self.store.connect() as c:
                return self.batch(seat, c)
        c = connection
        state = c.execute("SELECT busy FROM seats WHERE seat=?", (seat,)).fetchone()
        if not state or state['busy'] or c.execute("SELECT 1 FROM turns WHERE seat=? AND status IN ('sending','sent','working','uncertain')", (seat,)).fetchone():
            return []
        rows = [dict(r) for r in c.execute("""SELECT t.*,a.lane,a.mediated,a.reconcile FROM turns t
            JOIN arbitration a ON a.turn_id=t.id WHERE t.seat=? AND t.status IN ('queued','held')
            ORDER BY t.created,t.rowid""", (seat,))]
        # A prepared digest gets delivered once; arrivals during A's turn form the next digest.
        prepared = [r for r in rows if r['mediated']]
        if prepared:
            return prepared[:1]
        held = [r for r in rows if r['status'] == 'held']
        coalesced = [r for r in rows if r['lane'] == 'coalesce' and r['status'] != 'held']
        ready = coalesced or [r for r in rows if r['lane'] == 'append'][:1]
        return held + ready if ready else []

    def payload(self, rows):
        result = []
        for row in rows:
            context = json.loads(self.store.pull(row['context']))
            result.append({'id': row['id'], 'text': lane(row['prompt'])[1], 'lane': row['lane'],
                           'context': row['context'], 'attachments': context.get('attachments', []),
                           'reconcile': row.get('reconcile', '')})
        return result

    def commit(self, seat, rows, digest):
        if not isinstance(digest, str) or not digest.strip():
            raise ValueError('B returned an empty digest')
        self.intake(seat)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = self.batch(seat, c)
            if [r['id'] for r in current] != [r['id'] for r in rows]:
                return False  # Another message arrived or C cancelled; recompile the pending batch.
            root = rows[0]
            attachments, sources = [], []
            for row in rows:
                obj = c.execute('SELECT body FROM objects WHERE id=?', (row['context'].rsplit('/', 1)[-1],)).fetchone()
                manifest = json.loads(obj['body'])
                attachments.extend(manifest.get('attachments', []))
                sources.append({'turn': row['id'], 'context': row['context'], 'lane': row['lane']})
            reconcile = '\n'.join(r['reconcile'] for r in rows if r.get('reconcile'))
            # Deliver the operator's words verbatim. A single message reaches A
            # exactly as typed (no marker); only a genuine multi-message merge or
            # a reconcile brief gets a neutral header.
            parts = []
            if len(rows) > 1:
                parts.append('[Operator sent ' + str(len(rows)) + ' messages, delivered verbatim]')
            if reconcile:
                parts.append('[Reconcile brief]\n' + reconcile)
            parts.append(digest.strip())
            text = '\n\n'.join(parts)
            prompt = self.store._object(c, seat, root['id'], 'prompt', text.encode(), 'b-digest.md', 'text/markdown', 'B')
            context = self.store._object(c, seat, root['id'], 'context', json.dumps({
                'schema': 'mct.context/1', 'origin': 'B', 'prompt': prompt,
                'attachments': attachments, 'sources': sources}).encode(), 'context.json', 'application/json', 'B')
            c.execute("UPDATE turns SET context=?,status='queued',updated=? WHERE id=?", (context, time.time(), root['id']))
            c.execute('UPDATE arbitration SET mediated=1 WHERE turn_id=?', (root['id'],))
            for row in rows[1:]:
                c.execute("UPDATE turns SET status='coalesced',updated=? WHERE id=?", (time.time(), row['id']))
                c.execute('UPDATE arbitration SET merged_into=?,mediated=1 WHERE turn_id=?', (root['id'], row['id']))
                self.store._event(c, seat, row['id'], 'status', 'Included in B digest', {'turn': root['id']})
            self.store._event(c, seat, root['id'], 'status', 'B compiled ' + str(len(rows)) + ' message(s) into one prompt',
                              {'sources': sources, 'context': context})
            return True

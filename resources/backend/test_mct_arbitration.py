import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest

from mct_arbitration import Arbitration, lane
from mct_gateway import Broker, Store


class ArbitrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.store = Store(self.home / 'mct/renderer')
        self.arb = Arbitration(self.store)
        self.store.adopt('codex', 'fixture.jsonl', 'epoch')
        self.serial = 0

    def submit(self, text, files=()):
        self.serial += 1
        return self.store.submit('codex', 'request_%04d' % self.serial, text, files)

    def test_all_idle_messages_pass_through_b(self):
        original = self.submit('please fix it')
        batch = self.arb.batch('codex')
        self.assertFalse(batch[0]['mediated'])
        self.assertTrue(self.arb.commit('codex', batch, 'Please fix the reported issue.'))
        prepared = self.arb.batch('codex')[0]
        manifest = json.loads(self.store.pull(prepared['context']))
        self.assertEqual(manifest['origin'], 'B')
        self.assertIn('B-compiled', manifest['operator_prompt'])
        self.assertEqual(manifest['sources'][0]['context'], original['context'])
        self.assertEqual(json.loads(self.store.pull(original['context']))['operator_prompt'], 'please fix it')

    def test_busy_messages_form_one_digest_with_every_attachment(self):
        with self.store.connect() as c:
            c.execute("UPDATE seats SET busy=1 WHERE seat='codex'")
        first = self.submit('implement settings')
        second = self.submit('also sessions', [{'name': 'plan.txt', 'dataUrl': 'data:text/plain;base64,' + base64.b64encode(b'plan').decode()}])
        third = self.submit('sessions means the actual live session')
        self.assertEqual(self.arb.batch('codex'), [])
        with self.store.connect() as c:
            c.execute("UPDATE seats SET busy=0 WHERE seat='codex'")
        batch = self.arb.batch('codex')
        self.assertEqual(len(batch), 3)
        self.assertTrue(self.arb.commit('codex', batch, 'Implement settings and track the actual live session.'))
        root = self.arb.batch('codex')[0]
        manifest = json.loads(self.store.pull(root['context']))
        self.assertEqual(len(manifest['sources']), 3)
        self.assertEqual(self.store.pull(manifest['attachments'][0]['object']), b'plan')
        self.assertEqual(len(self.store.events('codex')['turns']), 1)
        # A restart retains the prepared digest and never promotes each source into a separate turn.
        restored = Arbitration(Store(self.home / 'mct/renderer'))
        self.assertEqual(restored.batch('codex')[0]['context'], root['context'])
        self.assertEqual(self.store.next_turn('codex', root['id'])['id'], first['id'])
        self.assertIsNone(self.store.next_turn('codex'))
        # Idempotency still compares the original operator text and original attachments.
        self.assertEqual(self.store.submit('codex', 'request_0002', 'also sessions',
            [{'name': 'plan.txt', 'dataUrl': 'data:text/plain;base64,' + base64.b64encode(b'plan').decode()}])['id'], second['id'])

    def test_message_during_compilation_rebuilds_batch(self):
        self.submit('first')
        batch = self.arb.batch('codex')
        self.submit('correction')
        self.assertFalse(self.arb.commit('codex', batch, 'stale digest'))
        self.assertEqual(len(self.arb.batch('codex')), 2)

    def test_cancel_during_compilation_never_dispatches_cancelled_input(self):
        first = self.submit('first')
        batch = self.arb.batch('codex')
        self.store.cancel('codex', first['id'])
        self.assertFalse(self.arb.commit('codex', batch, 'obsolete'))
        self.assertEqual(self.arb.batch('codex'), [])

    def test_append_keeps_own_turn_after_digest(self):
        separate = self.submit('+ separate task')
        combined = self.submit('amend current work')
        batch = self.arb.batch('codex')
        self.assertEqual([r['id'] for r in batch], [combined['id']])
        self.arb.commit('codex', batch, 'Amend current work.')
        root = self.store.next_turn('codex', combined['id'])
        self.assertEqual(root['id'], combined['id'])
        with self.store.connect() as c:
            c.execute("UPDATE turns SET status='complete' WHERE id=?", (combined['id'],))
        self.assertEqual(self.arb.batch('codex')[0]['id'], separate['id'])

    def test_only_explicit_control_prefixes_choose_lanes(self):
        self.assertEqual(lane('please stop doing that')[0], 'coalesce')
        self.assertEqual(lane('! change goal'), ('interrupt', 'change goal'))
        self.assertEqual(lane('todo: save this'), ('capture', 'save this'))
        self.assertEqual(lane('? later'), ('capture', 'later'))
        self.assertEqual(lane('+ separate'), ('append', 'separate'))

    def test_interrupt_reconcile_survives_b_rephrasing(self):
        first = self.submit('old goal')
        follow = self.submit('! corrected goal')
        self.store.cancel('codex', first['id'])
        self.arb.controls('codex')
        self.arb.reconcile(follow['id'], first)
        batch = self.arb.batch('codex')
        self.arb.commit('codex', batch, 'Corrected goal.')
        text = json.loads(self.store.pull(self.arb.batch('codex')[0]['context']))['operator_prompt']
        self.assertIn('partial changes', text)
        self.assertIn(first['context'], text)

    def test_capture_has_no_a_turn(self):
        item = self.submit('todo: save for later')
        controls = self.arb.controls('codex')
        self.assertEqual(controls[0]['lane'], 'capture')
        self.arb.captured(item['id'], 't1')
        self.assertEqual(self.arb.batch('codex'), [])
        self.assertEqual(self.store.events('codex')['turns'], [])

    def test_bare_interrupt_holds_reconcile_until_next_message(self):
        old = self.submit('old goal')
        interrupt = self.submit('!')
        self.store.cancel('codex', old['id'])
        self.arb.controls('codex')
        self.arb.reconcile(interrupt['id'], old)
        self.assertEqual(self.arb.batch('codex'), [])
        self.submit('the corrected goal')
        batch = self.arb.batch('codex')
        self.assertEqual(len(batch), 2)
        self.assertTrue(self.arb.commit('codex', batch, 'The corrected goal.'))
        root = self.store.next_turn('codex', batch[0]['id'])
        text = json.loads(self.store.pull(root['context']))['operator_prompt']
        self.assertIn(old['context'], text)

    def test_broker_does_not_bypass_b_on_failure(self):
        source = self.home / 'rollout.jsonl'
        source.write_text('')
        sent = []
        class Adapter:
            def probe(self): return str(source), 'epoch'
            async def send(self, text): sent.append(text); return True
        def fail(rows): raise RuntimeError('B unavailable')
        broker = Broker(self.home, collate=fail)
        broker.adapters['codex'] = Adapter()
        self.submit('retained request')
        asyncio.run(broker.tick('codex'))
        self.assertEqual(sent, [])
        self.assertIn('B unavailable', broker.errors['codex'])
        self.assertEqual(broker.store.events('codex')['turns'][0]['status'], 'queued')

    def test_broker_delivers_one_b_pointer_for_three_midturn_messages(self):
        source = self.home / 'rollout.jsonl'
        source.write_text('')
        sent, compiled = [], []
        class Adapter:
            def probe(self): return str(source), 'epoch'
            async def send(self, text): sent.append(text); return True
        def collate(rows):
            compiled.append(rows)
            return 'Combined operator requirements.'
        broker = Broker(self.home, collate=collate)
        broker.adapters['codex'] = Adapter()
        broker.store.adopt('codex', str(source), 'epoch')
        with self.store.connect() as c:
            c.execute("UPDATE seats SET busy=1 WHERE seat='codex'")
        for text in ['first', 'second', 'correction']:
            self.submit(text)
        asyncio.run(broker.tick('codex'))
        self.assertEqual(compiled, [])
        with self.store.connect() as c:
            c.execute("UPDATE seats SET busy=0 WHERE seat='codex'")
        asyncio.run(broker.tick('codex'))
        self.assertEqual(len(compiled), 1)
        self.assertEqual(len(compiled[0]), 3)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith('MCT context '))
        asyncio.run(broker.tick('codex'))
        self.assertEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()

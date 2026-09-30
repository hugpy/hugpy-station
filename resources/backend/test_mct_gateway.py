"""Lossless storage, queue recovery and provider protocol regression checks."""
import asyncio
import base64
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mct_gateway import Broker, Store, normalize


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger')
        self.offset = 0
        self.store.adopt('codex', 'fixture.jsonl', 'epoch1')

    def ingest(self, items, seat='codex'):
        raw = json.dumps(items).encode() + b'\n'
        self.store.ingest(seat, 'fixture.jsonl', self.offset, raw, items, self.offset + len(raw))
        self.offset += len(raw)

    def test_lossless_prompt_attachment_and_idempotency(self):
        text = '  Hello 🌻\n\n' + 'long body\n' * 30000 + '  '
        attachment = bytes(range(256)) * 20
        files = [{'name':'../image.bin', 'dataUrl':'data:application/octet-stream;base64,' + base64.b64encode(attachment).decode()}]
        first = self.store.submit('codex', 'request_0001', text, files)
        self.assertEqual(self.store.submit('codex', 'request_0001', text, files)['id'], first['id'])
        context = json.loads(self.store.pull(first['context']))
        self.assertEqual(context['operator_prompt'], text)
        self.assertEqual(self.store.pull(context['attachments'][0]['object']), attachment)
        self.assertEqual(self.store.events('codex')['events'][0]['text'], text)
        with self.assertRaises(ValueError): self.store.submit('codex', 'request_0001', text, [])
        with self.assertRaises(ValueError): self.store.submit('codex', 'request_0001', 'changed', files)
        with self.assertRaises(ValueError): self.store.pull(first['context'].replace(Store.scope('codex'), Store.scope('claude-code')))

    def test_integrity_and_no_overwrite(self):
        t = self.store.submit('codex', 'request_0002', 'exact')
        target = Path(self.tmp.name) / 'out'
        self.store.pull(t['context'], target)
        with self.assertRaises(FileExistsError): self.store.pull(t['context'], target)
        with self.store.connect() as c:
            c.execute("UPDATE objects SET body=? WHERE kind='prompt'", (b'changed',))
        with self.assertRaises(ValueError): self.store.pull(t['context'])

    def test_queue_cancel_late_output_and_restart(self):
        first = self.store.submit('codex', 'request_0003', 'first')
        second = self.store.submit('codex', 'request_0004', 'second')
        self.assertEqual(self.store.next_turn('codex')['id'], first['id'])
        self.assertIsNone(self.store.next_turn('codex'))
        self.store.sent(first['id'], True)
        self.ingest([('user', 'MCT context ' + first['context'] + '. Pull it.', {})])
        self.assertTrue(self.store.cancel('codex', first['id']))
        self.assertIsNone(self.store.next_turn('codex'))
        self.ingest([('assistant', 'late reply must not display', {}), ('complete', '', {})])
        self.assertFalse(any(e['text'] == 'late reply must not display' for e in self.store.events('codex')['events']))
        recovered = Store(self.store.root)
        self.assertEqual(recovered.next_turn('codex')['id'], second['id'])
        self.assertEqual(sum(e['kind'] == 'user' for e in recovered.events('codex')['events']), 2)

    def test_ambiguous_delivery_blocks_resend(self):
        t = self.store.submit('codex', 'request_0005', 'first')
        self.store.next_turn('codex'); self.store.sent(t['id'], False)
        self.store.submit('codex', 'request_0006', 'second')
        self.assertIsNone(Store(self.store.root).next_turn('codex'))
        self.store.cancel('codex', t['id'])
        self.assertIsNotNone(self.store.next_turn('codex'))

    def test_epoch_change_never_resends_inflight(self):
        t = self.store.submit('codex', 'request_0007', 'first')
        self.store.next_turn('codex'); self.store.sent(t['id'], True)
        self.store.adopt('codex', 'new.jsonl', 'epoch2')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM turns WHERE id=?', (t['id'],)).fetchone()[0], 'interrupted')
        self.assertIsNone(self.store.next_turn('codex'))

    def test_raw_replay_and_compressed_db_snapshot(self):
        raw = b'{"unrecognized":"stored exactly"}\n'
        for _ in range(2): self.store.ingest('codex', 'fixture.jsonl', 0, raw, [('user','hello',{})], len(raw))
        self.assertEqual(len(self.store.events('codex')['events']), 1)
        generation, snapshot = self.store.snapshot()
        backup = Path(self.tmp.name) / 'restored.db'; backup.write_bytes(gzip.decompress(snapshot))
        with sqlite3.connect(backup) as c: self.assertEqual(c.execute('SELECT body FROM records').fetchone()[0], raw)
        self.store.submit('codex', 'request_0008', 'new after snapshot')
        self.store.archived(generation)
        self.assertIsNotNone(self.store.snapshot())
        latest, _ = self.store.snapshot(); self.store.archived(latest)
        self.assertIsNone(self.store.snapshot())
        self.store.submit('codex', 'request_0009', 'changed again')
        self.assertIsNotNone(self.store.snapshot())

    def test_imported_native_turn_has_pullable_response(self):
        self.ingest([('user','original',{}),('assistant','answer',{}),('complete','',{})])
        reply = next(e for e in self.store.events('codex')['events'] if e['kind']=='assistant')
        self.assertEqual(self.store.pull(reply['detail']['object']), b'answer')

    def test_claude_completion_seals_final_text(self):
        self.store.adopt('claude-code', 'fixture.jsonl', 'claude1')
        self.ingest([('user','hello',{}), ('commentary','Claude reply',{}), ('complete','',{})], seat='claude-code')
        answer = next(e for e in self.store.events('claude-code')['events'] if e['kind']=='assistant')
        self.assertIn('replaces', answer['detail'])
        self.assertEqual(self.store.pull(answer['detail']['object']), b'Claude reply')

    def test_prefixed_pointer_matches_sent_turn_and_unblocks_queue(self):
        first = self.store.submit('codex', 'request_prefixed', 'from MCT')
        second = self.store.submit('codex', 'request_followup', 'next question')
        self.store.next_turn('codex'); self.store.sent(first['id'], True)
        self.ingest([('user', 'unfinished native draft MCT context ' + first['context'] + '. Pull it.', {})])
        self.assertEqual(self.store.state('codex')['current_turn'], first['id'])
        self.assertEqual(sum(e['kind']=='user' for e in self.store.events('codex')['events']), 2)
        manifest = json.loads(self.store.pull(first['context']))
        file = Path(manifest['response_file']); file.write_text('  exact reply 🌻\n\n')
        handle = self.store.publish_reply(first['context'], file)
        self.assertEqual(self.store.publish_reply(first['context'], file), handle)
        self.assertEqual(self.store.pull(handle), b'  exact reply \xf0\x9f\x8c\xbb\n\n')
        self.ingest([('assistant','MCT response ' + handle,{}), ('complete','',{})])
        events = self.store.events('codex')['events']
        answers = [e for e in events if e['kind']=='response']
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]['text'], '  exact reply 🌻\n\n')
        self.assertEqual(answers[0]['detail']['object'], handle)
        self.assertFalse(any(e['kind']=='assistant' for e in events))   # the ack pointer line is archived, not shown
        self.assertEqual(self.store.next_turn('codex')['id'], second['id'])

    def test_reconciliation_requires_finished_provider_user_evidence(self):
        t = self.store.submit('codex', 'request_legacy', 'legacy')
        self.store.next_turn('codex'); self.store.sent(t['id'], True)
        raw = json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'draft MCT context ' + t['context'] + '. Pull.'}]}}).encode() + b'\n'
        # Reproduce the old parser: it imported the pointer as a separate user.
        self.store.ingest('codex', 'fixture.jsonl', 0, raw, [('user','unmatched legacy text',{})], len(raw))
        self.assertEqual(self.store.reconcile_delivered('codex'), [])
        self.store.ingest('codex', 'fixture.jsonl', len(raw), b'{}\n', [('complete','',{})], len(raw)+3)
        self.assertEqual(self.store.reconcile_delivered('codex'), [t['id']])
        self.assertEqual(self.store.reconcile_delivered('codex'), [])

    def test_file_reply_preserves_late_answer_and_rejects_conflicting_write(self):
        t = self.store.submit('codex', 'request_file', 'hello')
        self.store.next_turn('codex'); self.store.sent(t['id'], True)
        path = self.store.reply_path(t['id']); path.write_text('first')
        self.store.publish_reply(t['context'], path)
        path.write_text('changed')
        with self.assertRaises(ValueError): self.store.publish_reply(t['context'], path)
        other = self.store.submit('claude-code', 'request_cancel', 'hello')
        path = self.store.reply_path(other['id']); path.write_text('late')
        self.store.cancel('claude-code', other['id'])
        handle = self.store.publish_reply(other['context'], path)
        self.assertEqual(self.store.pull(handle), b'late')
        events = self.store.events('claude-code')['events']
        self.assertTrue(any(e['text'] == 'Complete · reply published after interrupt' for e in events))

    def test_missing_reply_file_is_visible_failure(self):
        t = self.store.submit('codex', 'request_missing', 'hello')
        self.store.next_turn('codex'); self.store.sent(t['id'], True)
        self.ingest([('user','MCT context '+t['context']+'.',{}), ('assistant','did not write file',{}), ('complete','',{})])
        events = self.store.events('codex')['events']
        self.assertFalse(any(e['kind']=='assistant' for e in events))
        self.assertTrue(any(e['kind']=='error' and 'publishing its reply' in e['text'] for e in events))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.broker = Broker(self.tmp.name, collate=lambda rows: '\n'.join(r['text'] for r in rows))
        self.source = Path(self.tmp.name) / 'native.jsonl'; self.source.write_text('')
        self.deliveries = []
        outer = self
        class FakeAdapter:
            def probe(self): return str(outer.source), 'native1'
            async def send(self, text): outer.deliveries.append(text); return True
            async def cancel(self): pass
        self.broker.adapters = {p:FakeAdapter() for p in ('codex','claude-code')}

    async def test_provider_dispatches_only_handle_and_survives_restart(self):
        for provider in ('codex', 'claude-code'):
            turn = self.broker.store.submit(provider, 'request_0010', 'secret operator input')
            await self.broker.tick(provider)
            self.assertTrue(self.deliveries[-1].startswith('MCT context ')); self.assertNotIn('secret operator input', self.deliveries[-1])
            handle = self.deliveries[-1].split()[2].rstrip('.')
            self.assertEqual(json.loads(self.broker.store.pull(handle))['sources'][0]['context'], turn['context'])
            await self.broker.tick(provider)
        self.assertEqual(len(self.deliveries), 2)

    async def test_partial_tail_never_dispatches(self):
        self.source.write_bytes(b'{"unfinished":')
        self.broker.store.submit('codex', 'request_0011', 'wait')
        await self.broker.tick('codex')
        self.assertEqual(self.deliveries, [])
        self.source.write_bytes(b'{"unfinished":true}\n')
        await self.broker.tick('codex'); self.assertEqual(len(self.deliveries), 1)

    async def test_two_file_reply_turns_dispatch_without_a_browser(self):
        first = self.broker.store.submit('codex','request_cycle_1','first')
        second = self.broker.store.submit('codex','request_cycle_2','+ second')
        await self.broker.tick('codex')
        self.assertEqual(len(self.deliveries), 1)
        def record(row):
            with self.source.open('a') as f: f.write(json.dumps(row)+'\n')
        for index, turn in enumerate((first, second), 1):
            turn['context'] = self.deliveries[-1].split()[2].rstrip('.')
            record({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':self.deliveries[-1]}]}})
            await self.broker.tick('codex')
            manifest=json.loads(self.broker.store.pull(turn['context']))
            file=Path(manifest['response_file']); file.write_text('Answer '+str(index))
            handle=self.broker.store.publish_reply(turn['context'],file)
            record({'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':handle}]}})
            record({'type':'event_msg','payload':{'type':'task_complete'}})
            await self.broker.tick('codex')
        self.assertEqual(len(self.deliveries),2)
        events=self.broker.store.events('codex')['events']
        self.assertEqual([e['text'] for e in events if e['kind']=='response'], ['Answer 1','Answer 2'])
        self.assertFalse(self.broker.store.state('codex')['busy'])
        self.assertEqual(self.broker.store.events('codex')['turns'],[])

    async def test_restart_holds_queue_until_verified(self):
        self.broker.store.submit('codex','request_restart','wait for restart')
        marker=self.broker.store.root/'codex-restart.json'
        marker.write_text(json.dumps({'status':'waiting'}))
        await self.broker.tick('codex')
        self.assertFalse(self.deliveries)
        marker.write_text(json.dumps({'status':'complete'}))
        await self.broker.tick('codex')
        self.assertEqual(len(self.deliveries),1)

    async def test_restart_preserves_guidance_and_overrides_old_permissions(self):
        from restart_codex import resume_argv
        argv=resume_argv('/bin/codex',['codex','-c','developer_instructions="hello"','-c','approval_policy="on-request"'], 'test-thread','test-model','high')
        self.assertIn('developer_instructions="hello"',argv)
        self.assertNotIn('approval_policy="on-request"',argv)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox',argv)
        self.assertEqual(argv[1:3],['resume','test-thread'])

    async def test_disabled_gate_and_missing_native_never_dispatch(self):
        self.broker.store.submit('codex', 'request_0012', 'wait')
        await self.broker.tick('codex', allow_send=False); self.assertFalse(self.deliveries)
        def unavailable(): raise RuntimeError('Native model is not running')
        self.broker.adapters['codex'].probe = unavailable
        await self.broker.tick('codex'); self.assertFalse(self.deliveries)
        self.assertIn('not running', self.broker.errors['codex'])

    async def test_old_prompt_files_and_attachments_are_imported(self):
        directory = Path(self.tmp.name) / 'mct/repl/prompt-inbox/test'; directory.mkdir(parents=True)
        prompt = directory / 'prompt.md'; prompt.write_text('  original\n')
        (directory / 'data.bin').write_bytes(b'\x00\xff')
        text, detail = self.broker.recover_prompt('Handle the operator prompt at ' + str(prompt) + ' — read it via a pull, then respond.')
        self.broker.store.adopt('codex', str(self.source), 'native1')
        self.broker.store.ingest('codex', str(self.source), 0, b'raw\n', [('user',text,detail)], 4)
        event = self.broker.store.events('codex')['events'][0]
        self.assertEqual(event['text'], '  original\n')
        self.assertEqual(self.broker.store.pull(event['detail']['attachments'][0]['object']), b'\x00\xff')

    async def test_normalizers_skip_reasoning_and_duplicate_summaries(self):
        self.assertEqual(normalize({'type':'response_item','payload':{'type':'reasoning','text':'private'}}, 'codex'), [])
        self.assertEqual(normalize({'type':'event_msg','payload':{'type':'task_complete','last_agent_message':'duplicate'}}, 'codex'), [('complete','',{})])
        row = {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':' exact\n'}]}}
        self.assertEqual(normalize(row,'codex'), [('assistant',' exact\n',{})])
        self.assertEqual(normalize({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'private'}]}}, 'claude-code'), [])
        self.assertEqual(normalize({'type':'system','subtype':'turn_duration'},'claude-code'), [('complete','',{})])


if __name__ == '__main__':
    unittest.main()

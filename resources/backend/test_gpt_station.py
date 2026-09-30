import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import gpt_station as gpt


class GptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / 'config.json'

    def test_partial_update_preserves_wrapper_settings_and_is_private(self):
        self.config.write_text(json.dumps({'custom': {'keep': True}, 'default_model': 'old'}))
        gpt.update_config(self.config, {'dangerous': False, 'model_reasoning_effort': 'high'})
        gpt.update_config(self.config, {'default_model': 'new-model'})
        cfg = gpt.read_config(self.config)
        self.assertEqual(cfg['custom'], {'keep': True})
        self.assertFalse(cfg['dangerous'])
        self.assertEqual(cfg['model_reasoning_effort'], 'high')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_reject_invalid_patch_without_clobbering_file(self):
        self.config.write_text('{"default_model":"kept"}')
        original = self.config.read_bytes()
        for bad in ({'dangerous': 'false'}, {'model_reasoning_effort': 'oops'},
                    {'default_model': 'x; echo bad'}, {'unknown': True}, [], {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gpt.update_config(self.config, bad)
            self.assertEqual(self.config.read_bytes(), original)
        self.config.write_text('broken')
        with self.assertRaises(ValueError):
            gpt.update_config(self.config, {'dangerous': True})
        self.assertEqual(self.config.read_text(), 'broken')

    def test_launch_uses_wrapper_config_and_explicit_permissions(self):
        cfg = {'default_model': 'chosen', 'dangerous': True, 'model_reasoning_effort': 'high'}
        with patch.object(gpt, 'wrapper_binary', return_value='/test/abstract-gpt'):
            argv = shlex.split(gpt.launch_command(cfg, self.config, 'literal $(never)'))
        self.assertEqual(argv[:5], ['env', 'AG_ROOT=' + str(self.root), '/test/abstract-gpt', 'launch', '--'])
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn('developer_instructions="literal $(never)"', argv)
        cfg['dangerous'] = False
        flags = gpt.launch_flags(cfg)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', flags)
        self.assertEqual(flags[:4], ['--sandbox', 'workspace-write', '--ask-for-approval', 'on-request'])

    def fixture(self, pid, argv, children=''):
        proc = self.root / str(pid)
        (proc / 'task' / str(pid)).mkdir(parents=True)
        (proc / 'fd').mkdir()
        (proc / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv))
        (proc / 'task' / str(pid) / 'children').write_text(children)
        return proc

    def test_tracker_follows_child_and_uses_session_meta_even_outside_tail(self):
        self.fixture(10, ['abstract-gpt', 'launch'], '11')
        proc = self.fixture(11, ['/bin/codex', '--dangerously-bypass-approvals-and-sandbox'])
        path = self.root / 'rollout-live.jsonl'
        rows = [dict(type='session_meta', payload={'id': 'actual-thread', 'cwd': '/work', 'cli_version': 'test'}),
                dict(type='response_item', payload={'text': 'private-content-' + 'x' * 1100000}),
                dict(type='turn_context', payload={'model': 'actual-model', 'effort': 'high',
                     'approval_policy': 'never', 'sandbox_policy': {'type': 'danger-full-access'}}),
                dict(type='event_msg', payload={'type': 'token_count', 'info': {
                     'total_token_usage': {'total_tokens': 123}, 'model_context_window': 1000}})]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        (proc / 'fd' / '4').symlink_to(path)
        result = gpt.tracker(proc_root=self.root, pane_pid=10)
        self.assertEqual(result['thread_id'], 'actual-thread')
        self.assertEqual(result['pid'], 11)
        self.assertEqual(result['model'], 'actual-model')
        self.assertTrue(result['unrestricted'])
        self.assertEqual(result['usage']['total_token_usage']['total_tokens'], 123)
        self.assertNotIn('private-content', json.dumps(result))

    def test_no_unrelated_transcript_fallback(self):
        self.fixture(10, ['bash'])
        (self.root / 'rollout-other.jsonl').write_text('{"type":"session_meta","payload":{"id":"other"}}')
        result = gpt.tracker(proc_root=self.root, pane_pid=10)
        self.assertFalse(result['alive'])
        self.assertIsNone(result['thread_id'])

    def test_long_turn_retains_model_and_permissions(self):
        path = self.root / 'rollout-long.jsonl'
        rows = [dict(type='session_meta', payload={'id': 'long-thread'}),
                dict(type='turn_context', payload={'model': 'long-model', 'approval_policy': 'never',
                     'sandbox_policy': {'type': 'danger-full-access'}}),
                dict(type='response_item', payload={'text': 'x' * 1200000}),
                dict(type='event_msg', payload={'type': 'task_started'})]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        data = gpt.transcript_metadata(path)
        self.assertEqual(data['model'], 'long-model')
        self.assertEqual(data['approval_policy'], 'never')


if __name__ == '__main__':
    unittest.main()

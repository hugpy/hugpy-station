"""Empty local-model results must not be serialized into operator digests."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SERVER = Path(__file__).with_name('server.py')
TREE = ast.parse(SERVER.read_text())
CHECK = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == '_hugpy_chat_check')
ONESHOT = next(ast.literal_eval(n.value) for n in TREE.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == '_HUGPY_CHAT_ONESHOT' for t in n.targets))
NAMESPACE = {}
exec(compile(ast.Module(body=[CHECK], type_ignores=[]), str(SERVER), 'exec'), NAMESPACE)


class BResponseTests(unittest.TestCase):
    def test_empty_invalid_and_error_results_rejected(self):
        for value in ('', ' \n ', None, {'text': 'object'}, '[error: worker unavailable]',
                      "ChatResult(ok=True, text='', error=None, native_tool_calls=[])"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                NAMESPACE['_hugpy_chat_check'](value)

    def test_plain_answer_preserved(self):
        self.assertEqual(NAMESPACE['_hugpy_chat_check']('  Keep every operator constraint.\n'),
                         'Keep every operator constraint.')

    def oneshot(self, answer, ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'hugpy_agent'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'config.py').write_text('def load_config(**kwargs): return {}\n')
            (package / 'gateway.py').write_text('''from types import SimpleNamespace
class Gateway:
    @classmethod
    def from_config(cls, cfg): return cls()
    def chat(self, *args, **kwargs):
        return SimpleNamespace(ok=%r, text=%r, content=None, error='worker failed')
''' % (ok, answer))
            env = dict(os.environ, PYTHONPATH=tmp)
            return subprocess.run([sys.executable, '-c', ONESHOT], cwd=tmp, env=env,
                                  input=json.dumps({'messages': [], 'max_tokens': 10}),
                                  text=True, capture_output=True, timeout=10)

    def test_success_with_no_text_does_not_fall_back_to_object_repr(self):
        result = self.oneshot('')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('B returned no text', result.stderr)

    def test_oneshot_failure_not_converted_to_answer(self):
        result = self.oneshot('', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_oneshot_returns_only_model_text(self):
        result = self.oneshot('Compile the three messages.')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'text': 'Compile the three messages.'})


if __name__ == '__main__':
    unittest.main()

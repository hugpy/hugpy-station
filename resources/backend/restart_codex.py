"""One-shot, operator-authorized restart of the exact Codex thread after a turn.

Run as a user service, outside the Codex process being replaced. The MCT broker
observes the maintenance record and holds queued messages until verified ready.
No credentials or prompts are printed. Failed restart attempts remain visible.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from mct_gateway import Store


def tmux(*args):
    return subprocess.run(['tmux', '-L', 'console', *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


def status(path, state, message, **details):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'status': state, 'message': message, 'updated': time.time(), **details}))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def resume_argv(binary, original, thread, model, effort):
    # Retain explicit configuration overrides, including the Station directive.
    config = []
    for i, arg in enumerate(original):
        if arg in ('-c', '--config') and i+1 < len(original):
            value = original[i+1]
            if value.partition('=')[0] not in ('approval_policy','sandbox_mode','model','model_reasoning_effort'):
                config.extend(['-c', value])
    return [binary, 'resume', thread, '--dangerously-bypass-approvals-and-sandbox',
            '--model', model, '-c', 'model_reasoning_effort=' + json.dumps(effort), *config]


def run(args):
    store = Store(Path(args.home)/'mct/renderer')
    marker = store.root/'codex-restart.json'
    target = '=keeper-codex:'
    try:
        pid = int(tmux('display-message','-p','-t',target,'#{pane_pid}'))
        if pid != args.pid:
            raise RuntimeError('The Codex PID changed before restart; nothing was replaced')
        proc = Path('/proc')/str(pid)
        original = [os.fsdecode(p) for p in (proc/'cmdline').read_bytes().split(b'\0') if p]
        binary = os.readlink(proc/'exe')
        if Path(original[0]).name != 'codex':
            raise RuntimeError('The selected pane is not the expected Codex process')
        source = Path(store.state('codex')['source'])
        if not source.name.endswith(args.thread+'.jsonl'):
            raise RuntimeError('The transcript does not belong to the requested thread')
        cwd = os.readlink(proc/'cwd')
        command = resume_argv(binary, original, args.thread, args.model, args.effort)
        status(marker, 'waiting', 'Restart scheduled after this reply; MCT messages are saved', thread=args.thread, old_pid=pid)
        # Arm before the current turn completes; ignore partial JSONL appends.
        offset = source.stat().st_size
        deadline = time.monotonic()+1800
        done = False
        while time.monotonic() < deadline and not done:
            with source.open('rb') as stream:
                stream.seek(offset)
                for raw in stream:
                    if not raw.endswith(b'\n'):
                        break
                    offset += len(raw)
                    row = json.loads(raw); p = row.get('payload') or {}
                    if row.get('type') == 'event_msg' and p.get('type') == 'task_complete' and p.get('turn_id') == args.turn:
                        done = True
                    if done and row.get('type') == 'event_msg' and p.get('type') == 'task_started':
                        raise RuntimeError('Another direct turn started; restart paused to preserve it')
            if not done:
                time.sleep(.25)
        if not done:
            raise RuntimeError('Timed out waiting for the specified turn; current process preserved')
        if int(tmux('display-message','-p','-t',target,'#{pane_pid}')) != pid:
            raise RuntimeError('The Codex PID changed while waiting; nothing was replaced')
        status(marker, 'restarting', 'Resuming the same Codex thread with unrestricted permissions', thread=args.thread, old_pid=pid)
        tmux('respawn-pane','-k','-t',target,'-c',cwd,shlex.join(command))
        for _ in range(90):
            newpid = int(tmux('display-message','-p','-t',target,'#{pane_pid}'))
            if newpid != pid:
                try:
                    argv = (Path('/proc')/str(newpid)/'cmdline').read_bytes().split(b'\0')
                    if b'--dangerously-bypass-approvals-and-sandbox' in argv and args.thread.encode() in argv:
                        # Give the native TUI time to open its existing transcript
                        # and finish loading; input delivered after that is queued.
                        time.sleep(3)
                        argv = (Path('/proc')/str(newpid)/'cmdline').read_bytes().split(b'\0')
                        if b'--dangerously-bypass-approvals-and-sandbox' not in argv:
                            continue
                        status(marker,'complete','Codex resumed with unrestricted permissions',thread=args.thread,old_pid=pid,new_pid=newpid)
                        print(json.dumps({'ok':True,'thread':args.thread,'old_pid':pid,'new_pid':newpid,'unrestricted':True}),flush=True)
                        return
                except FileNotFoundError:
                    pass
            time.sleep(1)
        raise RuntimeError('The replacement process was not verified; MCT delivery remains paused')
    except Exception as exc:
        status(marker,'failed',str(exc))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--thread', required=True)
    parser.add_argument('--turn', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', required=True)
    run(parser.parse_args())

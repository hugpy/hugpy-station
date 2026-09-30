"""Station HTTP integration for the durable MCT conversation renderer."""
import asyncio
import base64
import contextlib
import os
import re
import time
import json
from urllib.parse import quote

from aiohttp import web
from mct_gateway import Broker, NATIVE_SEATS
from mct_arbitration import lane


# --- t266: pending TUI choice prompts on tmux-backed seats -------------------
# A frontier seat that stops on an AskUserQuestion / permission / select box
# makes no further transcript writes, so the MCT turn looks "working" forever.
# We read the seat's own pane (the same `tmux -L console` socket and
# `=<session>:` target the NativeAdapter drives) and publish what we find on
# the live status route so the console can answer without the operator having
# to switch to the raw terminal.
TMUX_SOCK = os.environ.get('MCT_TMUX_SOCK', 'console')
CHOICE_SCAN_LINES = 60        # scrollback inspected
CHOICE_NEAR_BOTTOM = 24       # an option block this close to the bottom is live UI
CHOICE_TTL = 1.0              # seconds; the live route polls sub-second
_EDGE = '│┃▌▐┆╎┊|'          # box sides drawn around TUI panels
_CURSORS = '❯▶▸➤➜»'              # ❯ ▶ ▸ ➤ ➜ »
_NUM = re.compile(r'^(?P<mark>[' + _CURSORS + r'>]\s+)?(?P<n>\d{1,2})[.):]\s+(?P<label>\S.*)$')
_MARK = re.compile(r'^(?P<mark>[' + _CURSORS + r'])\s+(?P<label>\S.*)$')
_ASKING = re.compile(r'(\?\s*$)|\b(do you want|would you like|allow|approve|proceed|select|choose|which|confirm)\b', re.I)
_KEYS = {'Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Space', 'Tab', 'BSpace'}


def _norm(line):
    """Strip the panel borders TUIs draw so option lines match on their own."""
    s = line.replace('\t', '    ').rstrip()
    while s and s[0] in _EDGE:
        s = s[1:]
    while s and s[-1] in _EDGE:
        s = s[:-1]
    return s.rstrip()


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def _numbered(lines):
    end = None
    for i in range(len(lines) - 1, max(-1, len(lines) - CHOICE_NEAR_BOTTOM - 1), -1):
        if _NUM.match(lines[i].strip()):
            end = i
            break
    if end is None:
        return []
    rows, expect, i = [], None, end
    while i >= 0:
        m = _NUM.match(lines[i].strip())
        if not m:
            break
        n = int(m.group('n'))
        if expect is not None and n != expect:
            break
        rows.append((i, m, n))
        expect, i = n - 1, i - 1
    rows.reverse()
    if len(rows) < 2 or rows[0][2] not in (0, 1):
        return []
    marked = [k for k, (_, m, _n) in enumerate(rows) if m.group('mark')]
    if len(marked) > 1:
        return []
    if not marked and not any(_ASKING.search(lines[j].strip()) for j in range(max(0, rows[0][0] - 4), rows[0][0])):
        return []      # bare numbered text, not a live selector
    return [{'line': i, 'number': n, 'label': m.group('label').strip(),
             'selected': bool(m.group('mark'))} for i, m, n in rows]


def _marker(lines):
    idx = mark = None
    for i in range(len(lines) - 1, max(-1, len(lines) - CHOICE_NEAR_BOTTOM - 1), -1):
        m = _MARK.match(lines[i].strip())
        if m and m.group('label').strip():
            idx, mark = i, m
            break
    if idx is None:
        return []
    col = _indent(lines[idx]) + 2                    # cursor glyph + its space
    def sibling(j):
        return (lines[j].strip() and _indent(lines[j]) == col
                and not _MARK.match(lines[j].strip()) and not _NUM.match(lines[j].strip()))
    lo = idx
    while lo - 1 >= 0 and sibling(lo - 1):
        lo -= 1
    hi = idx
    while hi + 1 < len(lines) and sibling(hi + 1):
        hi += 1
    if hi - lo < 1:
        return []
    out = []
    for j in range(lo, hi + 1):
        label = (mark.group('label') if j == idx else lines[j].strip()).strip()
        out.append({'line': j, 'number': None, 'label': label, 'selected': j == idx})
    return out


def detect_choice(pane, seat=''):
    """Return a pending-choice object for a pane sitting on a TUI selector, else None."""
    lines = [_norm(l) for l in (pane or '').splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    lines = lines[-CHOICE_SCAN_LINES:]
    if not lines:
        return None
    options, kind = _numbered(lines), 'numbered'
    if not options:
        options, kind = _marker(lines), 'select'
    if not options:
        return None
    question, j = [], options[0]['line'] - 1
    while j >= 0 and len(question) < 3:
        s = lines[j].strip().strip('─━╭╮╰╯└┘┌┐ ')
        if s and not _NUM.match(s) and not _MARK.match(s):
            question.append(s)
        elif question:
            break
        j -= 1
    selected = next((k for k, o in enumerate(options) if o['selected']), None)
    for o in options:
        o.pop('line', None)
    tail = [l for l in lines[-14:] if l.strip()]
    return {'seat': seat, 'kind': kind, 'options': options, 'selected': selected,
            'question': ' — '.join(reversed(question))[:400],
            'confidence': 'high' if selected is not None else 'low',
            'lines': tail, 'detected_at': int(time.time()),
            'notice': 'This seat is waiting on a choice in its terminal; the MCT turn cannot '
                      'advance until it is answered.'}
# --- end t266 detection -----------------------------------------------------


def install(app, state_home, locus, ts_call, frontier_enabled=lambda: True, collate=None, capture=None):
    async def lifecycle(app):
        broker = app['mct_broker'] = Broker(state_home, collate=collate, capture=capture)
        app['mct_archive_status'] = 'Pending central backup'

        async def follow(provider):
            while True:
                await broker.tick(provider, allow_send=frontier_enabled())
                await asyncio.sleep(1)

        async def archive():
            while True:
                try:
                    snapshot = await asyncio.to_thread(broker.store.snapshot)
                    if snapshot:
                        generation, raw = snapshot
                        station = locus()
                        if not station:
                            raise RuntimeError('Station locus is not registered')
                        result = await ts_call(app, 'exchange/archive', {
                            'locus': station, 'source': 'mct/renderer/ledger.sqlite3.gz',
                            'backend': 'mct', 'session_id': 'native-adapters',
                            'data': base64.b64encode(raw).decode('ascii'),
                            'version': time.time_ns()}, timeout=90)
                        if not isinstance(result, dict) or not result.get('stored'):
                            raise RuntimeError('Central archive did not acknowledge the snapshot')
                        await asyncio.to_thread(broker.store.archived, generation)
                        app['mct_archive_status'] = 'Backed up centrally'
                except Exception as exc:
                    app['mct_archive_status'] = 'Saved locally; central backup pending: ' + str(exc)[:160]
                await asyncio.sleep(30)

        tasks = [asyncio.create_task(follow(p)) for p in NATIVE_SEATS]
        tasks.append(asyncio.create_task(archive()))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def route(request, data):
        vm = str(data.get('vm') or '')
        if vm not in ('', '@keeper', 'keeper', locus()):
            raise web.HTTPBadRequest(text='The conversation renderer is available on this station’s locus. Open the remote station to use its MCT view.')
        provider = data.get('provider', 'codex')
        if provider not in NATIVE_SEATS:
            raise ValueError('Unknown frontier provider')
        return provider

    async def interaction(request):
        broker = request.app['mct_broker']
        try:
            data = await request.json() if request.method == 'POST' else request.query
            provider = route(request, data)
            if request.method == 'POST':
                text = data.get('text', '')
                if isinstance(text, str):
                    disposition, body = lane(text)
                    if disposition in ('append', 'capture') and not body.strip() and not data.get('files'):
                        raise ValueError('Write a message after the lane prefix')
                turn = await asyncio.to_thread(broker.store.submit, provider, data.get('request_id', ''), data.get('text', ''), data.get('files', []))
                return web.json_response({'id': turn['id'], 'status': turn['status']}, status=202)
            after = max(0, int(data.get('after', 0)))
            result = await asyncio.to_thread(broker.store.events, provider, after)
            state = await asyncio.to_thread(broker.store.state, provider)
            result.update(provider=provider, busy=bool(state['busy']), epoch=state['epoch'],
                          error=broker.maintenance(provider) or broker.errors.get(provider),
                          delivery=await asyncio.to_thread(broker.store.delivery_state, provider),
                          archive=request.app['mct_archive_status'])
            result['mediation'] = broker.b_state.get(provider, 'Messages pass through B before A')
            # t266: a seat parked on a TUI selector writes nothing more to its
            # transcript, so the turn would stall silently. Surface it instead.
            result['pending_choice'] = await choice_of(request.app, provider)
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def tmux(*args, timeout=8):
        proc = await asyncio.create_subprocess_exec(
            'tmux', '-L', TMUX_SOCK, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError('The seat terminal did not respond')
        if proc.returncode:
            raise RuntimeError((err or b'').decode('utf-8', 'replace').strip() or 'The seat terminal is not open')
        return (out or b'').decode('utf-8', 'replace')

    def target(app, provider):
        """The tmux pane the NativeAdapter drives, or '' when this seat is not tmux-backed."""
        broker = app['mct_broker']
        if getattr(broker, 'transport', 'tmux') != 'tmux':
            return ''
        return NATIVE_SEATS.get(provider, '')

    async def choice_of(app, provider, fresh=False):
        """Pending TUI choice for a seat, cached briefly because the live route polls fast."""
        seat = target(app, provider)
        if not seat:
            return None
        cache = app.setdefault('mct_choice_cache', {})
        hit = cache.get(provider)
        if hit and not fresh and time.monotonic() - hit[0] < CHOICE_TTL:
            return hit[1]
        try:
            pane = await tmux('capture-pane', '-p', '-t', '=' + seat + ':', '-S', '-' + str(CHOICE_SCAN_LINES))
            found = detect_choice(pane, seat)
        except Exception:
            found = None
        cache[provider] = (time.monotonic(), found)
        return found

    async def choice_answer(request):
        """Answer a pending TUI choice from the console, over the same tmux send-keys
        path the broker uses to deliver pointers (mct_gateway NativeAdapter.send)."""
        try:
            data = await request.json()
            provider = route(request, data)
            seat = target(request.app, provider)
            if not seat:
                raise ValueError('This frontier seat is not tmux-backed, so it has no terminal prompt to answer')
            pane = '=' + seat + ':'
            text, keys, index = data.get('text'), data.get('keys'), data.get('index')
            if isinstance(keys, list) and keys:
                for k in keys:
                    if k not in _KEYS:
                        raise ValueError('Unsupported key: ' + str(k)[:24])
                await tmux('send-keys', '-t', pane, *keys)
                sent = ' '.join(keys)
            elif index is not None:
                pending = await choice_of(request.app, provider, fresh=True)
                if not pending:
                    raise ValueError('That prompt is no longer on screen; re-read the seat before answering')
                options = pending['options']
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    raise ValueError('Pick one of the listed options')
                if not 0 <= index < len(options):
                    raise ValueError('Pick one of the listed options')
                want = str(data.get('label') or '')
                if want and want != options[index]['label']:
                    raise ValueError('The options changed while you were reading them; try again')
                here = pending['selected']
                if here is None:
                    number = options[index]['number']
                    if number is None:
                        raise ValueError('This prompt has no visible cursor, so it cannot be answered from here — '
                                         'use the respond box')
                    await tmux('send-keys', '-t', pane, '-l', str(number))
                    sent = 'key ' + str(number)
                else:
                    step = 'Down' if index > here else 'Up'
                    for _ in range(abs(index - here)):
                        await tmux('send-keys', '-t', pane, step)
                        await asyncio.sleep(0.05)
                    await asyncio.sleep(0.1)
                    await tmux('send-keys', '-t', pane, 'Enter')
                    sent = str(abs(index - here)) + '× ' + step + ' + Enter'
            elif isinstance(text, str) and text.strip():
                # Literal input then a distinct Enter event — identical to NativeAdapter.send.
                await tmux('send-keys', '-t', pane, '-l', text)
                await asyncio.sleep(0.15)
                await tmux('send-keys', '-t', pane, 'Enter')
                sent = 'text'
            else:
                raise ValueError('Send an option index, a key, or text')
            request.app.setdefault('mct_choice_cache', {}).pop(provider, None)
            await asyncio.sleep(0.35)
            return web.json_response({'ok': True, 'sent': sent,
                                      'pending_choice': await choice_of(request.app, provider, fresh=True)})
        except (ValueError, TypeError, RuntimeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def cancel(request):
        try:
            data = await request.json()
            provider = route(request, data)
            await request.app['mct_broker'].cancel(provider, data.get('turn', ''))
            return web.json_response({'ok': True})
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def event(request):
        try:
            provider = route(request, request.query)
            with request.app['mct_broker'].store.connect() as c:
                row = c.execute('SELECT detail FROM events WHERE seat=? AND id=?', (provider, int(request.query.get('id', 0)))).fetchone()
            if not row:
                raise web.HTTPNotFound()
            return web.json_response({'detail': json.loads(row['detail'])})
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def object_download(request):
        store = request.app['mct_broker'].store
        try:
            handle = request.query.get('handle', '')
            raw = await asyncio.to_thread(store.pull, handle)
            with store.connect() as c:
                row = c.execute('SELECT name FROM objects WHERE id=?', (handle.rsplit('/', 1)[-1],)).fetchone()
            return web.Response(body=raw, content_type='application/octet-stream', headers={
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['name'], safe=''),
                'Cache-Control': 'no-store'})
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=404)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/api/mct/interaction', interaction)
    app.router.add_post('/api/mct/interaction', interaction)
    app.router.add_post('/api/mct/interaction/cancel', cancel)
    app.router.add_post('/api/mct/choice', choice_answer)
    app.router.add_get('/api/mct/event', event)
    app.router.add_get('/api/mct/object', object_download)

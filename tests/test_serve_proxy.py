"""Exercise Station's actual proxy functions without booting unrelated services."""
import ast
import asyncio
import json
import re
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def test_generic_proxy(tmp_path):
    source = Path(__file__).with_name('server.py')
    if not source.exists():
        source = Path(__file__).parents[1] / 'resources/backend/server.py'
    tree = ast.parse(source.read_text())
    names = {'_ac_loci_env', '_ac_loci', '_ac_locus_key', '_ac_target', '_serve_headers', '_keeper_surface_now', 'ac_proxy', '_ac_rebase_body', '_ac_rebase_text'}
    import os
    ns = dict(os=os, re=re, Path=Path, web=web, aiohttp=aiohttp, asyncio=asyncio,
              AC_UPSTREAM='http://localhost:1', AC_SERVE_UNIT='legacy', AC_LOCI_PATH=tmp_path/'legacy.json',
              SERVE_LOCI_PATH=tmp_path/'serve.json', _AC_LOCI_CACHE={'mtime':None,'doc':{}},
              _PROXY_HOP={'host','connection','content-length'}, _AC_PROXY_TIMEOUT=aiohttp.ClientTimeout(total=5),
              _read_json=lambda p, default: json.loads(p.read_text()) if p.exists() else default)
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names],type_ignores=[]),str(source),'exec'), ns)
    async def exercise():
        seen=[]
        async def upstream(req):
            seen.append((req.path,dict(req.headers)))
            return web.json_response({'ok':True},status=201 if req.method=='POST' else 200)
        app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
        async with TestServer(app) as remote:
            token=tmp_path/'token';token.write_text('upstream-secret')
            ns['SERVE_LOCI_PATH'].write_text(json.dumps({'a-brain':{'url':str(remote.make_url('')).rstrip('/'),'token_file':str(token)}}))
            station=web.Application();station.router.add_route('*','/serve/{tail:.*}',ns['ac_proxy'])
            async with aiohttp.ClientSession() as session:
                station['proxy_sess']=session
                async with TestClient(TestServer(station)) as client:
                    response=await client.post('/serve/@a-brain/api/sessions',json={},headers={'Origin':'https://station.example','Authorization':'Bearer station-secret','Cookie':'private=value'})
                    assert response.status==201
                    assert await response.json()=={'ok':True}
        path,headers=seen[0]
        assert path=='/api/sessions'
        assert headers['Authorization']=='Bearer upstream-secret'
        assert 'Origin' not in headers and 'Cookie' not in headers
        assert ns['_ac_target']('a-brain')[2]=='/serve/@a-brain/'
        ns['SERVE_LOCI_PATH'].write_text(json.dumps({'host': {'url': 'http://127.0.0.1:9126'}}))
        host_url, host_unit, host_path, host_key = ns['_ac_target']('host')
        assert (host_url, host_unit, host_path, host_key) == (
            'http://127.0.0.1:9126', 'hugpy-agent-serve.service', '/serve/@host/', '')
        async def state(vm=''):
            return {'url': host_url}
        ns['_ac_serve_state'] = state
        ns['KEEPER_SURFACE'] = 'tmux'
        assert (await ns['_keeper_surface_now']('host'))[0] == 'serve'
    asyncio.run(exercise())

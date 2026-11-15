"""Exercise the anonymous browser path and explicit approval, without browser packages."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

import examples.company_ai_access.routers.assets as api
from examples.company_ai_access.routers.assets import assets_router


def test_login_link_preserves_request_and_escapes_html():
    async def check():
        app = FastAPI()
        app.include_router(
            assets_router(
                browser_api_prefix='/bff/ai_access/api/company-ai',
                login_path='/bff/auth/oidc/login?return_to=/company-ai/setup',
            ),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            target = (
                '/company-ai/setup?request_id=6459021a-323a-451a-aad3-a03e4818eab7&user_code=C764-B324&extra=%22%3E'
            )
            page = await client.get(target)
            href = re.search(r'<a id="login" href="([^"]+)"', page.text).group(1)
            login = urlsplit(unescape(href))
            assert login.path == '/bff/auth/oidc/login'
            assert parse_qs(login.query)['return_to'] == [target]
            assert page.headers['cache-control'] == 'no-store'
            assert '<option value="Both" selected disabled>' in page.text
            assert "script-src 'self'" in page.headers['content-security-policy']
            assert 'changdar-ai 電腦設定與綁定' in page.text
            assert 'href="/company-ai/downloads/changdar-ai-setup.exe"' in page.text

    asyncio.run(check())


def test_installer_download_exact_bytes_headers_and_filename_allowlist(tmp_path, monkeypatch):

    monkeypatch.setattr(api, '__file__', str(tmp_path / 'routers/assets.py'))
    (tmp_path / 'windows').mkdir()
    payload = b'MZ-test-installer-artifact'
    (tmp_path / 'windows/changdar-ai-setup.exe').write_bytes(payload)

    async def check():
        app = FastAPI()
        app.include_router(assets_router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            response = await client.get('/company-ai/downloads/changdar-ai-setup.exe')
            assert response.content == payload
            assert response.headers['content-disposition'] == 'attachment; filename="changdar-ai-setup.exe"'
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert (await client.get('/company-ai/downloads/unknown.exe')).status_code == 404

    asyncio.run(check())


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed to execute the browser script')
def test_browser_login_expiry_and_explicit_approval():
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const id = '6459021a-323a-451a-aad3-a03e4818eab7';
const reply = (status, data) => ({ok: status === 200, status, json: async () => data});
const flush = async () => { for (let i=0; i<20; i++) await Promise.resolve(); };
function browser(responses, saved = new Map()) {
  const elements = {};
  for (const name of ['status','approve','tool-choice','install-command','login','login-help','retry','device','code']) {
    elements[name] = {textContent:'', disabled:true, hidden:false, value:'Claude', options:[],
      events:{}, addEventListener(type, fn) { this.events[type] = fn; }};
  }
  const calls = [], redirects = [], timers = new Set();
  elements['tool-choice'].value = 'Both';
  const location = {origin:'https://changdar-server.mooo.com', pathname:'/company-ai/setup',
    search:'?request_id='+id+'&user_code=C764-B324', assign(url) { redirects.push(url); }};
  const context = {document:{getElementById: n => elements[n],
      querySelector: () => ({dataset:{apiPrefix:'/bff/ai_access/api/company-ai'}})},
    location, URLSearchParams, Date,
    sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    setInterval: fn=>{timers.add(fn);return fn;},clearInterval:fn=>timers.delete(fn),
    fetch: async (url, options) => {calls.push({url,options});assert.ok(responses.length,'Unexpected request');return responses.shift();}};
  vm.runInNewContext(source, context);
  return {elements,calls,redirects,timers,location,saved};
}
(async () => {
  const saved = new Map();
  let b = browser([reply(401)], saved); await flush();
  assert.equal(b.elements['install-command'].textContent,'.\\Setup-CompanyAI.ps1 -Portal '+b.location.origin);
  b.elements['tool-choice'].value = 'Claude'; b.elements['tool-choice'].events.change();
  assert.ok(b.elements['install-command'].textContent.endsWith(' -Tools Claude'));
  b.elements['tool-choice'].value = 'Codex'; b.elements['tool-choice'].events.change();
  assert.ok(b.elements['install-command'].textContent.endsWith(' -Tools Codex'));
  assert.equal(b.redirects.length,1);
  const login = new URL(b.redirects[0], b.location.origin);
  assert.equal(login.pathname,'/bff/auth/oidc/login');
  assert.equal(login.searchParams.get('return_to'),b.location.pathname+b.location.search);
  assert.equal(b.elements.approve.disabled,true);
  b = browser([reply(401)],saved); await flush();
  assert.equal(b.redirects.length,0,'SSO failure must not cause an infinite redirect loop');
  assert.equal(b.elements['login-help'].hidden,false);
  assert.match(b.elements.status.textContent,/尚未登入/);
  const policy = reply(200,{codex_enabled:true});
  const pending = () => reply(200,{label:'WIN ARM64',code:'C764-B324',status:'pending',expires_at:Date.now()/1000+600});
  b = browser([policy,pending(),reply(200,{csrf_token:'csrf-test'}),reply(200,{status:'approved'})],saved);
  await flush();
  assert.equal(b.elements.code.textContent,'C764-B324');
  assert.equal(b.elements.device.textContent,'WIN ARM64');
  assert.equal(b.elements.approve.disabled,false);
  assert.equal(b.calls.length,2,'Loading must never approve automatically');
  assert.equal(b.saved.size,0);
  await b.elements.approve.events.click();
  assert.equal(b.calls[2].url,'/bff/auth/csrf');
  const post = b.calls[3];
  assert.equal(post.options.headers['X-CSRF-Token'],'csrf-test');
  assert.equal(post.options.headers['X-Company-AI-Action'],'1');
  assert.deepEqual(JSON.parse(post.options.body),{user_code:'C764-B324',confirm:true});
  assert.equal(b.elements.approve.disabled,true);
  assert.equal(b.timers.size,0);
  assert.match(b.elements.status.textContent,/已核准/);
  b = browser([policy,reply(410)]); await flush();
  assert.equal(b.elements.approve.disabled,true);
  assert.match(b.elements.status.textContent,/重新執行/);
  assert.equal(b.redirects.length,0);
  b = browser([policy,reply(200,{label:'WIN',code:'C764-B324',status:'pending',expires_at:0})]);
  await flush();
  assert.equal(b.elements.approve.disabled,true);
  assert.match(b.elements.status.textContent,/10 分鐘/);
  b = browser([reply(401),policy,pending()],new Map([['company-ai-login:'+id,'1']]));
  await flush();
  await b.elements.retry.events.click();
  assert.equal(b.elements.approve.disabled,false,'Login in another tab can be retried');
  b = browser([policy,pending(),reply(401)]); await flush();
  await b.elements.approve.events.click();
  assert.equal(b.elements.approve.disabled,true);
  assert.equal(b.calls.length,3,'Expired session must not submit approval without CSRF');
  b = browser([reply(200,{codex_enabled:false}),pending()]); await flush();
  assert.equal(b.elements['tool-choice'].value,'Claude');
  assert.ok(b.elements['install-command'].textContent.endsWith(' -Tools Claude'));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(
        ['node', '-e', script, str(Path('examples/company_ai_access/ui/setup.js').resolve())],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr

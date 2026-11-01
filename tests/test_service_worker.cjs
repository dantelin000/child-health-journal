'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const handlers = {};
  let network = async () => new Response('current server response', {
    status: 200, headers: { 'Cache-Control': 'no-store' },
  });
  const scope = {
    location: { origin: 'https://health.test' },
    addEventListener: (name, fn) => { handlers[name] = fn; },
    skipWaiting: () => Promise.resolve(),
    clients: { claim: () => Promise.resolve() },
  };
  const context = { self: scope, URL, Response, fetch: (...args) => network(...args) };
  Object.defineProperty(context, 'caches', { get() { throw new Error('Cache Storage must not be used'); } });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/sw.js'), 'utf8'), context);
  let pending;
  handlers.install({ waitUntil: (promise) => { pending = promise; } });
  await pending;
  handlers.activate({ waitUntil: (promise) => { pending = promise; } });
  await pending;
  function trigger(request) {
    let response;
    handlers.fetch({ request, respondWith: (value) => { response = value; } });
    return response;
  }
  const navigation = { method: 'GET', mode: 'navigate', url: 'https://health.test/?sensitive=synthetic' };
  let response = await trigger(navigation);
  assert.equal(await response.text(), 'current server response');
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(trigger({ method: 'POST', mode: 'navigate', url: 'https://health.test/record/new' }), undefined);
  assert.equal(trigger({ method: 'GET', mode: 'cors', url: 'https://health.test/session-status' }), undefined);
  assert.equal(trigger({ method: 'GET', mode: 'navigate', url: 'https://other.test/' }), undefined);
  network = async () => { throw new TypeError('offline'); };
  response = await trigger(navigation);
  const body = await response.text();
  assert.equal(response.status, 200);
  assert.match(body, /暂时无法连接/);
  assert.match(body, /需要联网查看和保存/);
  assert.doesNotMatch(body, /sensitive=synthetic/);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.match(response.headers.get('Content-Security-Policy'), /default-src 'none'/);
  console.log('PASS: navigation is network-only; offline page contains no request data; POST/API bypassed; no Cache Storage');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });

'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {observeFetch, sessionFromBody, publishPrivate} = require('../ccc_claude_request_observer.cjs');
const sid = '12345678-1234-1234-1234-123456789012';
const body = JSON.stringify({metadata: {user_id: JSON.stringify({session_id: sid})}, messages: ['private prompt']});
const url = 'http://127.0.0.1:1234/v1/messages';

test('session formats and unknown body', () => {
  assert.equal(sessionFromBody(body), sid);
  assert.equal(sessionFromBody(JSON.stringify({metadata: {user_id: `user_abc_account__session_${sid}`}})), sid);
  for (const value of [null, '{}', '{', JSON.stringify({metadata: {user_id: 'bad'}})]) assert.equal(sessionFromBody(value), null);
});
test('preserves receiver, arguments and promise; captures headers not body', async () => {
  const records = [], receiver = {}, init = {headers: {'x-api-key': 'fake-one'}, body};
  const promise = Promise.resolve({redirected: false});
  const wrapped = observeFetch(function(...args) {
    assert.equal(this, receiver); assert.equal(args[0], url); assert.equal(args[1], init); return promise;
  }, r => records.push(r), {pid: 123});
  assert.equal(wrapped.call(receiver, url, init), promise);
  await promise;
  assert.equal(records[0].api_key, 'fake-one');
  assert.equal(records[0].session_id, sid);
  assert.ok(!JSON.stringify(records).includes('private prompt'));
});
test('separate clients and changing actual headers do not share keys', () => {
  const a = [], b = [], original = () => Promise.resolve({});
  const first = observeFetch(original, r => a.push(r), {pid: 1});
  const pinned = observeFetch(original, r => b.push(r), {pid: 2});
  for (const key of ['fake-old', 'fake-new']) {
    first(url, {headers: {authorization: `Bearer ${key}`}, body});
    pinned(url, {headers: {'x-api-key': 'fake-pinned'}, body});
  }
  assert.equal(a[1].authorization, 'Bearer fake-new'); assert.equal(b[1].api_key, 'fake-pinned');
});
test('unknown session and absent credentials replace previous association', () => {
  const records = [], wrapped = observeFetch(() => Promise.resolve({}), r => records.push(r), {});
  wrapped(url, {headers: {'x-api-key': 'fake-old'}, body});
  wrapped(url, {body: '{}'});
  assert.equal(records[1].session_id, null); assert.equal(records[1].api_key, null);
});
test('redirect only invalidates latest request, never overwrites newer key', async () => {
  const records = [], resolves = [];
  const wrapped = observeFetch(() => new Promise(resolve => resolves.push(resolve)), r => records.push(r), {});
  wrapped(url, {headers: {'x-api-key': 'fake-old'}, body});
  wrapped(url, {headers: {'x-api-key': 'fake-new'}, body});
  resolves[0]({redirected: true}); await Promise.resolve();
  assert.equal(records.length, 2);
  resolves[1]({redirected: true}); await Promise.resolve();
  assert.equal(records.at(-1).credential_scope, 'opaque_redirect'); assert.equal(records.at(-1).api_key, null);
});
test('observer errors do not alter synchronous errors or rejected promises', async () => {
  const failure = new Error('transport');
  const wrapped = observeFetch(() => {throw failure;}, () => {throw Error('disk');}, {});
  assert.throws(() => wrapped(url, {body}), error => error === failure);
  const pending = Promise.reject(failure);
  const asyncWrapped = observeFetch(() => pending, () => {}, {});
  assert.equal(asyncWrapped(url, {body}), pending);
  await assert.rejects(pending, error => error === failure);
});
test('Request headers respected; explicit init headers override', () => {
  const records = [], wrapped = observeFetch(() => Promise.resolve({}), r => records.push(r), {});
  const request = new Request(url, {headers: {'x-api-key': 'fake-request'}});
  wrapped(request, {body}); wrapped(request, {headers: {'x-api-key': 'fake-init'}, body});
  assert.equal(records[0].api_key, 'fake-request'); assert.equal(records[1].api_key, 'fake-init');
});
test('unrelated requests are not recorded', () => {
  const wrapped = observeFetch(() => Promise.resolve({}), () => assert.fail('unexpected record'), {});
  wrapped('http://127.0.0.1/v1/models');
});

test('failed newer publication clears old credential and still sends original request', () => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ccc-observation-'));
  const destination = path.join(directory, 'request.json');
  const write = fs.writeFileSync;
  try {
    fs.chmodSync(directory, 0o700);
    publishPrivate(directory, destination, {api_key: 'fake-old'});
    let sends = 0;
    const wrapped = observeFetch(() => { sends++; return Promise.resolve({}); },
      record => publishPrivate(directory, destination, record), {});
    fs.writeFileSync = () => { const error = new Error('disk full'); error.code = 'ENOSPC'; throw error; };
    wrapped(url, {headers: {'x-api-key': 'fake-new'}, body});
    assert.equal(sends, 1);
    assert.equal(fs.existsSync(destination), false);
  } finally {
    fs.writeFileSync = write;
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('real loopback transport receives exactly the observed headers', async () => {
  const http = require('node:http');
  const received = [], records = [];
  const server = http.createServer((request, response) => {
    received.push({token: request.headers.authorization, key: request.headers['x-api-key']});
    request.resume(); response.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const wrapped = observeFetch(globalThis.fetch, record => records.push(record), {pid: process.pid});
    const endpoint = `http://127.0.0.1:${server.address().port}/v1/messages`;
    for (const key of ['fake-global-old', 'fake-global-new', 'fake-profile-pinned']) {
      const response = await wrapped(endpoint, {method: 'POST', body,
        headers: {'content-type': 'application/json', 'x-api-key': key}});
      await response.text();
    }
    assert.deepEqual(received.map(value => value.key), records.map(value => value.api_key));
    assert.deepEqual(records.map(value => value.api_key), ['fake-global-old', 'fake-global-new', 'fake-profile-pinned']);
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

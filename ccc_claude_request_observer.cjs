/* Observe Claude's actual fetch headers. Never read credentials from settings,
 * change requests, or log prompt bodies. Explicit per-process opt-in only. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function sessionFromBody(body) {
  if (typeof body !== 'string' || body.length > 16 * 1024 * 1024) return null;
  try {
    const value = JSON.parse(body)?.metadata?.user_id;
    if (typeof value !== 'string') return null;
    let sid;
    try { sid = JSON.parse(value)?.session_id; } catch {
      sid = /^user_[^\s]+_account_[^\s]*_session_([0-9a-f-]{36})$/i.exec(value)?.[1];
    }
    return typeof sid === 'string' && UUID.test(sid) ? sid.toLowerCase() : null;
  } catch { return null; }
}

function observeFetch(original, publish, identity) {
  let sequence = 0;
  return function(input, init) {
    let record;
    try {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
      if (/\/messages\/?$/.test(url.pathname) && ['http:', 'https:'].includes(url.protocol)) {
        const headers = new Headers(init?.headers !== undefined ? init.headers : input?.headers);
        const body = init?.body;
        record = {schema: 1, purpose: 'claude_request_attempt', ...identity,
          sequence: ++sequence, observed_at_ms: Date.now(),
          session_id: sessionFromBody(body),
          endpoint: url.origin + url.pathname, credential_scope: 'first_hop_headers',
          authorization: headers.get('authorization'), api_key: headers.get('x-api-key')};
        publish(record);
      }
    } catch { /* Observation failure must not change Claude's request. */ }
    // Preserve fetch's receiver, original inputs, promise and error behaviour.
    const pending = Reflect.apply(original, this, arguments);
    if (record) {
      const captured = record;
      // Clear the first-hop credential if a newer redirect made it insufficient.
      Promise.resolve(pending).then(response => {
        if (response?.redirected && captured.sequence === sequence) {
          try { publish({...captured, credential_scope: 'opaque_redirect', authorization: null, api_key: null}); }
          catch { /* The request has already completed. */ }
        }
      }, () => {}).catch(() => {});
    }
    return pending;
  };
}

function privateDirectory(directory) {
  if (!directory || !path.isAbsolute(directory)) return false;
  const info = fs.lstatSync(directory);
  return info.isDirectory() && info.uid === process.getuid() && (info.mode & 0o077) === 0;
}

function publishPrivate(directory, destination, record) {
  if (!privateDirectory(directory)) return;
  // A failed newer write (e.g. a full disk) must not leave the older credential
  // looking like the newest request. Readers tolerate this brief absent state.
  try { fs.unlinkSync(destination); } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const temp = path.join(directory, `.claude-${process.pid}-${crypto.randomUUID()}.tmp`);
  try {
    fs.writeFileSync(temp, JSON.stringify(record), {mode: 0o600, flag: 'wx'});
    fs.renameSync(temp, destination);
  } finally { try { fs.unlinkSync(temp); } catch {} }
}

function install() {
  const directory = process.env.CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR;
  if (!privateDirectory(directory) || typeof globalThis.fetch !== 'function') return;
  const epoch = crypto.randomUUID();
  const identity = {pid: process.pid, observer_epoch: epoch,
    surface_id: process.env.CMUX_SURFACE_ID || null,
    workspace_id: process.env.CMUX_WORKSPACE_ID || null};
  const destination = path.join(directory, `claude-${process.pid}-request.json`);
  function publish(record) {
    publishPrivate(directory, destination, record);
  }
  globalThis.fetch = observeFetch(globalThis.fetch, publish, identity);
}

try { install(); } catch { /* No opt-in / unreadable directory: no instrumentation. */ }
module.exports = {sessionFromBody, observeFetch, publishPrivate};

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const workerPath = process.argv[2];
const listeners = new Map();
const cacheMatches = [];
const networkRequests = [];
const shellResponse = new Response('canonical shell', {status: 200});
const cache = {
  addAll: async () => {},
  match: async (request) => {
    const key = typeof request === 'string' ? request : request.url;
    cacheMatches.push(key);
    return key === '/' ? shellResponse.clone() : undefined;
  },
  put: async () => {},
};

const context = {
  URL,
  Request,
  Response,
  Set,
  Promise,
  JSON,
  caches: {
    open: async () => cache,
    match: async (request) => cache.match(request),
    keys: async () => [],
    delete: async () => true,
  },
  fetch: async (request) => {
    networkRequests.push(typeof request === 'string' ? request : request.url);
    throw new Error('offline');
  },
  self: {
    location: {origin: 'https://mail.example.com'},
    addEventListener: (type, listener) => listeners.set(type, listener),
    skipWaiting: async () => {},
    clients: {claim: async () => {}},
    registration: {showNotification: async () => {}},
  },
};
vm.runInNewContext(fs.readFileSync(workerPath, 'utf8'), context, {filename: workerPath});

async function dispatch(path) {
  let responsePromise;
  const event = {
    request: new Request(`https://mail.example.com${path}`),
    respondWith: (promise) => {
      responsePromise = Promise.resolve(promise);
    },
  };
  listeners.get('fetch')(event);
  assert.ok(responsePromise, `${path} must be handled`);
  return responsePromise;
}

(async () => {
  const response = await dispatch('/?source=pwa');
  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'canonical shell');
  assert.ok(cacheMatches.includes('/'), 'offline navigation must try canonical /');

  for (const path of [
    '/health',
    '/ready',
    '/sw.js',
    '/api/messages',
    '/sso/login',
    '/admin/sso/login',
    '/admin/sso/callback',
  ]) {
    cacheMatches.length = 0;
    const excludedResponse = await dispatch(path);
    assert.equal(excludedResponse.status, 503, `${path} must fail offline`);
    assert.equal(cacheMatches.length, 0, `${path} must not read Cache Storage`);
    assert.ok(
      networkRequests.some((request) => request.endsWith(path)),
      `${path} must attempt the network`
    );
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

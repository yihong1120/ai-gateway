// Execute the real dashboard script with an expired WebUI session.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../examples/official_document_workflow/frontend/assets/app.js'), 'utf8');

async function check(search, expected) {
  const nodes = new Map();
  const requests = [], redirects = [];
  const context = {
    URLSearchParams, Date,
    location: {search, replace: url => redirects.push(url)},
    localStorage: {getItem() {throw Error('Must use HttpOnly session cookie, not localStorage');}},
    document: {getElementById(id) {if (!nodes.has(id)) nodes.set(id, {});return nodes.get(id);}},
    setInterval() {},
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: false, status: 401, json: async () => ({detail: 'Not authenticated'})};
    },
  };
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(redirects[0], expected);
  assert.equal(requests[0].options.credentials, 'same-origin');
  assert.equal(requests[0].options.headers.Authorization, undefined);
}

(async () => {
  await check('?csv=7030cb9f-a897-4ee4-b79f-bce51a9d652e', '/auth?redirect=%2Feip%3Fcsv%3D7030cb9f-a897-4ee4-b79f-bce51a9d652e');
  await check('?redirect=https://evil.test&csv=//evil.test', '/auth?redirect=%2Feip');
  console.log('Dashboard session-expiry and safe-return tests passed');
})().catch(error => {console.error(error);process.exitCode = 1;});

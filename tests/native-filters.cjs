const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { performance } = require('node:perf_hooks');
const context = { document: { addEventListener() {}, querySelector() { return null; } },
  setTimeout, clearTimeout, URLSearchParams, AbortController, performance, console: { debug() {} },
  location: { search: '', pathname: '/' }, history: { replaceState() {} } };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('resources/js/app.js', 'utf8'), context);
const app = context.GoAccess;
const filters = app.Filters;
function dataset(count, site) {
  return { general: { valid_requests: count, total_requests: count, bandwidth: count * 10 },
    vhosts: { data: [{ data: site, hits: { count } }] },
    requests: { data: [{ data: '/example', hits: { count } }], metadata: { hits: { total: { value: count } } } } };
}
const all = dataset(10, 'all'), selected = dataset(2, 'one.example');
const fields = Object.fromEntries(Array.from(filters.fields, key => [key, { value: '' }]));
filters.form = { elements: { namedItem(key) { return fields[key]; } } };
filters.status = { textContent: '' };
filters.section = { setAttribute() {} };
filters.config = { endpoint: '/data', cacheSeconds: 30 };
filters.appliedQuery = new URLSearchParams();
filters.suggestions = () => {};
let renders = 0;
app.App.setInitSort = () => {};
app.App.renderPanels = () => { renders++; };
app.AppData = all;
app.AppState = { expanded: { old: true } };
filters.remember('', all);
assert.equal(filters.query(filters.form).toString(), '');
fields.site.value = 'one.example'; fields.ip.value = '192.0.2.1';
assert.equal(filters.query(filters.form).toString(), 'site=one.example&ip=192.0.2.1');
let requests = [];
context.fetch = async url => {
  requests.push(url);
  return { ok: true, json: async () => selected, headers: { get: () => 'goaccess;dur=18' } };
};
(async () => {
  await filters.load(false);
  assert.equal(app.AppData, selected);
  assert.equal(app.AppData.requests.metadata.hits.total.value, 2);
  assert.equal(Object.keys(app.AppState).length, 0);
  assert.equal(renders, 1);
  assert.equal(requests.length, 1);
  assert.ok(requests[0].startsWith('/data?'));
  assert.equal(requests[0].includes('/report'), false);
  // Clear uses the initial dataset without reparsing logs or fetching the page.
  fields.site.value = ''; fields.ip.value = '';
  await filters.load(false);
  assert.equal(app.AppData, all);
  assert.equal(requests.length, 1);
  // Force refresh bypasses both browser and server result caches.
  await filters.load(true);
  assert.equal(requests.length, 2);
  assert.ok(requests[1].includes('_refresh=1'));
  const before = app.AppData;
  fields.site.value = 'failure.example';
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Timeout' }) });
  await filters.load(false);
  assert.equal(app.AppData, before);
  assert.match(filters.status.textContent, /Timeout.*Still showing/);
  // A superseded response must not replace the latest selected dataset.
  let resolveOld;
  context.fetch = () => new Promise(resolve => { resolveOld = resolve; });
  fields.site.value = 'old.example';
  const oldRequest = filters.load(false);
  context.fetch = async () => ({ ok: true, json: async () => selected, headers: { get: () => '' } });
  fields.site.value = 'new.example';
  await filters.load(false);
  resolveOld({ ok: true, json: async () => all });
  await oldRequest;
  assert.equal(app.AppData, selected);
  assert.equal(filters.appliedQuery.get('site'), 'new.example');
  assert.throws(() => filters.applyData({}), /invalid report/);
  for (const file of ['resources/tpls.html', 'resources/css/app.css', 'resources/js/app.js']) {
    assert.equal(fs.readFileSync(file, 'utf8').includes('panel-filter'), false, file);
  }
  assert.equal(fs.readFileSync('dashboard/server.py', 'utf8').includes('<iframe'), false);
  console.log('Native report filter checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

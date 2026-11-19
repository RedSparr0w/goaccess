const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = { document: { addEventListener() {}, querySelector() { return null; } }, setTimeout, clearTimeout };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('resources/js/app.js', 'utf8'), context);
const app = context.GoAccess;
const tables = app.Tables;
app.AppPrefs = { perPage: 2 };
app.AppState = {};
const rows = [
  { data: 'Example.COM', items: [{ data: '/home', method: 'GET' }, { data: '/admin', protocol: 'HTTP/2' }] },
  { data: '192.0.2.1' }, { data: '200' },
];
const before = JSON.stringify(rows);
assert.equal(tables.filterRows(rows, ' EXAMPLE.com ')[0].items.length, 2);
assert.equal(tables.filterRows(rows, '192.0.2')[0].data, '192.0.2.1');
assert.equal(tables.filterRows(rows, '200')[0].data, '200');
assert.equal(tables.filterRows(rows, '/admin')[0].items.length, 1);
assert.equal(tables.filterRows(rows, 'get')[0].items[0].data, '/home');
assert.equal(tables.filterRows(rows, 'http/2')[0].items[0].data, '/admin');
assert.equal(tables.filterRows(rows, 'missing').length, 0);
assert.equal(tables.filterRows(rows, '   '), rows);
assert.equal(tables.filterRows(['Some agent', 'Other agent'], 'some').length, 1);
assert.equal(JSON.stringify(rows), before);
assert.equal(tables.getTotalPages([]), 1);
assert.equal(tables.getPage('hosts', [], 0).length, 0);
assert.equal(tables.getCurPage('hosts'), 1);
app.AppData = { hosts: { data: rows } };
app.AppState.hosts.filter = '192.0.2';
assert.equal(tables.getFilteredRows('hosts').length, 1);
app.AppData.hosts.data = rows.concat({ data: '192.0.2.2' });
assert.equal(tables.getFilteredRows('hosts').length, 2);
app.AppState.hosts.filter = '';
assert.equal(tables.getFilteredRows('hosts').length, 4);
console.log('Table filter checks passed');

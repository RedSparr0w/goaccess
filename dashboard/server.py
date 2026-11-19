"""Serve request-filtered GoAccess reports from read-only log files."""
import gzip
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FIELDS = ('site', 'ip', 'url', 'method', 'status', 'referrer', 'agent', 'protocol', 'user')
NPM_FORMAT = '[%d:%t %^] %^ %^ %s - %m %^ %v "%U" [Client %h] [Length %b] [Gzip %^] [Sent-to %^] "%u" "%R"'


class Reports:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.ttl = max(1, int(os.getenv('REPORT_CACHE_SECONDS', '30')))

    def command(self, filters, directory):
        command = [os.getenv('GOACCESS_BIN', '/usr/bin/goaccess'), '-',
                   '--no-global-config', '--no-progress',
                   '--log-format=' + os.getenv('LOG_FORMAT', NPM_FORMAT),
                   '--date-format=' + os.getenv('DATE_FORMAT', '%d/%b/%Y'),
                   '--time-format=' + os.getenv('TIME_FORMAT', '%T'),
                   '--http-method=yes', '--http-protocol=yes',
                   '--max-items=100000',
                   '--output=' + str(Path(directory) / 'report.html'),
                   '--output=' + str(Path(directory) / 'report.json')]
        # Each subprocess starts with fresh storage; filtered reports never restore
        # the unfiltered persisted database.
        for field, value in filters:
            command.append('--filter=' + field + '=' + value)
        keep_last = os.getenv('KEEP_LAST', '')
        if keep_last:
            command.append('--keep-last=' + str(int(keep_last)))
        return command

    def files(self):
        root = Path(os.getenv('LOG_PATH', '/opt/log'))
        pattern = os.getenv('LOG_PATTERN', 'proxy-host-*_access.log*')
        archived = os.getenv('SKIP_ARCHIVED_LOGS', 'False').lower() != 'true'
        return sorted(p for p in root.glob(pattern) if p.is_file()
                      and (archived or p.name.endswith('.log')))

    def get(self, filters):
        key = tuple(filters)
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1:]
            files = self.files()
            if not files:
                raise ValueError('No access logs found. Check LOG_PATH and LOG_PATTERN.')
            with tempfile.TemporaryDirectory(prefix='goaccess-report-') as directory:
                # Disk-backed input handles compressed archives without retaining
                # the entire log history in RAM or passing paths from HTTP input.
                with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
                    for path in files:
                        opener = gzip.open if path.suffix == '.gz' else open
                        with opener(path, 'rb') as stream:
                            shutil.copyfileobj(stream, source)
                        source.write(b'\n')
                    source.seek(0)
                    result = subprocess.run(self.command(filters, directory), stdin=source,
                                            stdout=subprocess.DEVNULL, stderr=errors,
                                            timeout=int(os.getenv('REPORT_TIMEOUT', '180')))
                    if result.returncode:
                        errors.seek(0)
                        raise ValueError('Report generation failed:\n' + errors.read(8192).decode('utf-8', 'replace'))
                report = (Path(directory) / 'report.html').read_bytes()
                data = json.loads((Path(directory) / 'report.json').read_text(encoding='utf-8'))
            if len(self.cache) >= 8:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), report, data)
            return report, data


def parse_filters(query):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=16)
    if set(values) - set(FIELDS):
        raise ValueError('Unknown filter')
    filters = []
    for field in FIELDS:
        entries = values.get(field, [''])
        if len(entries) != 1 or len(entries[0]) > 2048 or '\x00' in entries[0]:
            raise ValueError('Invalid filter value')
        if entries[0]:
            filters.append((field, entries[0]))
    return filters


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GoAccess — Filter traffic</title>
<style>
body{margin:0;font:14px system-ui;background:#f3f4f6;color:#172033}
header{padding:14px 20px;background:white}h1{font-size:20px;margin:0 0 10px}
form{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{display:grid;gap:4px}input,button{padding:8px;border:1px solid #9ca3af;border-radius:4px}
button{background:#253d85;color:white;cursor:pointer}p{margin:10px 0 0}
iframe{width:100%;height:calc(100vh - 240px);min-height:550px;border:0}
</style><header><h1>Filter traffic across the entire report</h1>
<form id="filters">FIELDS<button>Apply filters</button><button type="reset">Clear all</button></form>
<p id="scope" role="status">All traffic</p>
<p>Filters combine with AND. Site, IP, method and status match exactly; URL, referrer and user agent match substrings.
Reports are rebuilt from available logs. Refresh to include new traffic.</p>
<button id="refresh" type="button">Refresh report</button></header>
<iframe id="report" title="Filtered GoAccess report" sandbox="allow-scripts allow-same-origin"></iframe>
<script>
const form = document.querySelector('form'), frame = document.querySelector('iframe');
const params = new URLSearchParams(location.search);
for (const [key,value] of params) if (form.elements[key]) form.elements[key].value = value;
function apply() {
 const query = new URLSearchParams();
 for (const [key,value] of new FormData(form)) if (value) query.set(key,value);
 frame.src = '/report?' + query;
 history.replaceState(null, '', '/?' + query);
 document.querySelector('#scope').textContent = query.size ? 'Showing traffic matching: ' + [...query].map(([k,v]) => k + ' = ' + v).join(' AND ') : 'All traffic';
}
form.onsubmit = event => {event.preventDefault();apply()};
form.onreset = () => {setTimeout(() => {for(const input of form.querySelectorAll('input')) input.value='';apply()},0)};
document.querySelector('#refresh').onclick = apply;
apply();
fetch('/options').then(r => {if (!r.ok) throw Error();return r.json()}).then(data => {
 for (const key of ['site','ip']) for (const value of data[key]) {
  const option = document.createElement('option');option.value=value;
  document.querySelector('#' + key + '-options').append(option);
 }
}).catch(() => {});
</script></html>'''


def page():
    labels = {'site': 'Website', 'ip': 'Client IP', 'url': 'URL contains', 'method': 'Method',
              'status': 'HTTP status', 'referrer': 'Referrer contains', 'agent': 'User agent contains',
              'protocol': 'Protocol', 'user': 'Remote user'}
    controls = []
    for field in FIELDS:
        options = f' list="{field}-options"' if field in ('site', 'ip') else ''
        controls.append(f'<label>{labels[field]}<input name="{field}"{options} maxlength="2048"></label>')
        if options:
            controls.append(f'<datalist id="{field}-options"></datalist>')
    return PAGE.replace('FIELDS', ''.join(controls)).encode()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, kind):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if url.path == '/':
                parse_filters(url.query)
                self.send(200, page(), 'text/html; charset=utf-8')
            elif url.path == '/report':
                report, _ = self.server.reports.get(parse_filters(url.query))
                self.send(200, report, 'text/html; charset=utf-8')
            elif url.path == '/options':
                _, data = self.server.reports.get([])
                options = {field: [str(row['data']) for row in data.get(panel, {}).get('data', [])]
                           for field, panel in [('site', 'vhosts'), ('ip', 'hosts')]}
                self.send(200, json.dumps(options).encode(), 'application/json')
            else:
                self.send(404, b'Not found', 'text/plain')
        except (ValueError, OSError, EOFError, subprocess.TimeoutExpired) as error:
            body = ('<!doctype html><h2>Unable to generate report</h2><pre>'
                    + html.escape(str(error)) + '</pre>').encode()
            self.send(400 if isinstance(error, ValueError) else 503, body, 'text/html; charset=utf-8')


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '7880'))), Handler)
    server.reports = Reports()
    server.serve_forever()

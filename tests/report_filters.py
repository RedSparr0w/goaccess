"""Run against a built Linux/macOS GoAccess: python3 tests/report_filters.py ./goaccess."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import os
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
from server import Reports

binary = str(Path(sys.argv[1]).resolve())
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    log = root / 'access.log'
    log.write_text('\n'.join([
        'one.example 192.0.2.1 - - [01/Jul/2026:06:20:38 +0000] "GET /one HTTP/1.1" 200 100 "https://ref.one/" "AgentOne/1.0"',
        'one.example 192.0.2.2 - - [01/Jul/2026:07:20:38 +0000] "POST /two HTTP/1.1" 404 200 "https://ref.two/" "AgentTwo/1.0"',
        'two.example 192.0.2.1 - - [02/Jul/2026:08:20:38 +0000] "GET /three HTTP/1.1" 500 400 "https://ref.three/" "AgentThree/1.0"',
    ]) + '\n')

    def report(*filters):
        target = root / 'report.json'
        subprocess.run([binary, str(log), '--no-global-config', '--log-format=VCOMBINED',
                        '--http-method=yes', '--http-protocol=yes', '-j', '2',
                        '--output=' + str(target)] + ['--filter=' + rule for rule in filters],
                       check=True, capture_output=True)
        return json.loads(target.read_text())

    baseline = report()
    assert baseline['general']['valid_requests'] == 3
    site = report('site=one.example')
    assert site['general']['valid_requests'] == 2
    assert {row['data'] for row in site['hosts']['data']} == {'192.0.2.1', '192.0.2.2'}
    assert {row['data'] for row in site['vhosts']['data']} == {'one.example'}
    combined = report('site=one.example', 'ip=192.0.2.1')
    assert combined['general']['valid_requests'] == 1
    assert combined['general']['total_requests'] == 1
    assert combined['general']['bandwidth'] == 100
    text = json.dumps(combined)
    for excluded in ['/two', '/three', 'two.example', '192.0.2.2', 'ref.two', 'ref.three']:
        assert excluded not in text, excluded
    assert report('method=POST')['general']['valid_requests'] == 1
    assert report('status=500')['general']['bandwidth'] == 400
    assert report('url=/two')['general']['valid_requests'] == 1
    assert report('referrer=ref.three')['general']['valid_requests'] == 1
    assert report('agent=AgentTwo')['general']['valid_requests'] == 1
    assert report('site=missing.example')['general']['valid_requests'] == 0
    # A cleared filter must restore all panels, independent of previous queries.
    assert report()['general']['valid_requests'] == 3
    invalid = subprocess.run([binary, '--filter=unknown=value', '--help'], capture_output=True)
    assert invalid.returncode != 0

    npm = root / 'proxy-host-1_access.log'
    npm.write_text('\n'.join([
        '[01/Jul/2026:06:20:38 +0000] - - 200 - GET https one.example "/one" [Client 192.0.2.1] [Length 100] [Gzip -] [Sent-to 127.0.0.1] "AgentOne/1.0" "-"',
        '[01/Jul/2026:07:20:38 +0000] - - 404 - POST https two.example "/two" [Client 192.0.2.2] [Length 200] [Gzip -] [Sent-to 127.0.0.1] "AgentTwo/1.0" "-"',
    ]) + '\n')
    with patch.dict(os.environ, {'GOACCESS_BIN': binary, 'LOG_PATH': str(root)}, clear=True):
        reports = Reports()
        document, data = reports.get([('site', 'one.example')])
        assert b'<html' in document.lower()
        assert data['general']['valid_requests'] == 1
        assert data['general']['bandwidth'] == 100
        assert {row['data'] for row in data['hosts']['data']} == {'192.0.2.1'}
print('Report-wide filter integration checks passed')

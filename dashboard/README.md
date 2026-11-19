# Report-wide filters in the native GoAccess report

Website, client IP, status and method controls are part of the GoAccess report,
above the overall metrics. More filters contains URL, referrer, user agent,
protocol and remote user. Apply updates every table, chart, percentage and total
in place from the matching request dataset. There is no iframe or separate UI.
The previous individual table searches and their code have been removed.

Filters combine with AND. Site, IP, method, protocol, user and status match
exactly; URL, referrer and user agent use case-sensitive substring matches.
Only fields present in the log format can match. NPM proxy access logs are the
default format. Site and IP suggestions come from data already loaded in the
report, with suggestions retained from previous selections. Type any value to
filter it even when it does not appear in the suggestions. The URL preserves the
selection for bookmarks.

## Deploy without cloning

Push the changes to `master`, then use `dashboard/compose.yml` as your server's
Compose file. It keeps the existing port 7880 and NPM log mount. Run:

```sh
docker compose config --quiet
docker compose build --pull
docker compose up -d
```

The `dashboard` image target serves the native GoAccess HTML and its `/data`
endpoint. Refresh the browser after rebuilding so the new controls load. The
refresh button bypasses both browser and server result caches. There is no
WebSocket connection or automatic refresh.

The earlier NPM image's `LOG_TYPE`, `HTML_REFRESH`, `PERSIST`, `RESTORE` and
`DB_PATH` variables do not apply to this backend. The old cache volume is not
used or removed. Historical traffic must be available in the mounted raw logs,
including `.gz` archives. `KEEP_LAST` limits report dates; it cannot recreate
removed logs. NPM redirection and error reports are not included by default.

## Performance and timing

The initial page requires one GoAccess invocation and emits only HTML. A filter
change emits only JSON; scripts, templates, styles and the page remain loaded.
Suggestions never start another report build. Browser and server caches keep up
to eight report selections for 30 seconds by default. Reusing a selection within
that window avoids aggregation altogether.

Unchanged logs reuse a prepared disk-backed input snapshot. Changed files,
rotation, truncation or added archives invalidate that snapshot on the next
uncached request. Decompression and copying are timed separately from GoAccess.
`REPORT_TIMEOUT` covers both preparation and the GoAccess subprocess (after the
request acquires the build lock).

The default display limit is GoAccess's normal 366 rows per panel, replacing the
previous 100,000-row override. This limits report payload and chart work, not
which requests are filtered or counted in totals. You can increase it with
`REPORT_MAX_ITEMS`; high values increase browser work and response sizes.

Responses support gzip. `Server-Timing` and container logs show `queue`, `source`,
`goaccess`, `output` and `compress` durations in milliseconds. A cache hit reports
`cache` instead of rebuilding. `goaccess` includes the entire subprocess,
including report serialization; it is not just the report's parsing-time metric.
The browser console's `GoAccess filter timing` entry separates fetch/update time
from native panel rendering.

An uncached selection still runs a fresh GoAccess aggregation over the prepared
input. This is not an indexed query engine: an 18-second parse can still take
roughly that long plus aggregation/output/rendering. The changes remove duplicate
builds, repeated decompression for unchanged logs, oversized payloads and full-page
reloads. Actual response time depends on log size and hardware.

## Settings

- `LOG_PATH`: directory containing logs (default `/opt/log`).
- `LOG_PATTERN`: glob below it (default `proxy-host-*_access.log*`).
- `SKIP_ARCHIVED_LOGS=True`: only read files ending in `.log`.
- `LOG_FORMAT`, `DATE_FORMAT`, `TIME_FORMAT`: override the NPM parser format.
- `KEEP_LAST`: optional number of days retained in each report.
- `REPORT_CACHE_SECONDS`: result cache lifetime (default 30).
- `REPORT_MAX_ITEMS`: maximum displayed rows per panel (default 366).
- `REPORT_TIMEOUT`: preparation plus subprocess timeout in seconds (default 180).
- `PORT`: listening port (default 7880).

Native CLI filters are also available:

```sh
goaccess access.log --log-format=VCOMBINED \
  --filter=site=example.com --filter=ip=192.0.2.1 -o report.html
```

Filtered reports require raw logs and fresh aggregation storage. An existing
persisted aggregate database cannot be filtered retroactively. Static HTML on its
own cannot recompute report-wide traffic; the integrated controls are enabled
when the report is served with its data endpoint.

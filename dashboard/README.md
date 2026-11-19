# Report-wide dashboard filters

This service rebuilds GoAccess reports from matching requests. Selecting a site
or IP changes every traffic panel, chart, percentage, visitor count and bandwidth
metric. Filters combine with AND. Site, IP, method, protocol, user and status use
exact matches; URL, referrer and agent use case-sensitive substring matches.
Only fields present in the log format can match. NPM proxy access logs are the
default format. Site/IP fields offer suggestions from the full report; you can
also type values directly. Suggestions are limited to the report's top 100,000
rows. Expand the dropdown by typing in the field.

## Deploy without cloning

Push the changes to `master`, then replace the server's `compose.yml` with the
contents of `dashboard/compose.yml` from this repository. From the existing
`~/docker/goaccess` directory run:

```sh
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose logs --tail=100 goaccess
```

Open the existing dashboard on port 7880. Apply filters in the toolbar above the
report, or clear them to return to all traffic. The URL preserves your selection
and can be bookmarked. Use Refresh report to request another snapshot. Reports
are cached for 30 seconds by default; refresh within that window can reuse the
previous snapshot. There is no WebSocket connection or automatic refresh.

This replaces the previous NPM wrapper service. Its `LOG_TYPE`, `HTML_REFRESH`,
`PERSIST`, `RESTORE` and `DB_PATH` variables do not apply. The existing named cache
volume can be kept in Docker, but must not be restored into filtered reports.
Historical traffic must be available in the mounted logs, including `.gz`
archives. `KEEP_LAST` limits dates in the report; it does not recreate removed
logs. NPM redirection and error reports are not included by default.

Each uncached selection scans the available logs, expands compressed archives to
a temporary disk-backed input and runs one fresh GoAccess process. Large histories
need sufficient temporary disk space and processing time. Builds are serialized,
with up to eight results cached in memory. `REPORT_TIMEOUT` limits the GoAccess
process, not the initial archive expansion. Set the timeout and cache duration to
suit your log volume. Keep the service behind your existing authenticated proxy
when sharing it.

## Other logs and settings

- `LOG_PATH`: directory containing logs (default `/opt/log`).
- `LOG_PATTERN`: glob below that directory (default `proxy-host-*_access.log*`).
- `SKIP_ARCHIVED_LOGS=True`: only read files ending in `.log`.
- `LOG_FORMAT`, `DATE_FORMAT`, `TIME_FORMAT`: override the NPM parser format.
- `KEEP_LAST`: optional number of days retained during each report generation.
- `REPORT_CACHE_SECONDS`: cache lifetime (default 30).
- `REPORT_TIMEOUT`: subprocess timeout in seconds (default 180).
- `PORT`: listening port (default 7880).

The native CLI also supports the filters independently:

```sh
goaccess access.log --log-format=VCOMBINED \
  --filter=site=example.com --filter=ip=192.0.2.1 -o report.html
```

Always generate filtered reports from raw logs with fresh storage. An existing
persisted aggregate database cannot be filtered retroactively. Static HTML files
on their own do not provide report-wide interactive filtering; use this service.

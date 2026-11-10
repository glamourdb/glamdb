# GlamDB alerting

The Alerting checks workflow runs the full loopback checker on Ubuntu. It
extracts native Prometheus/Alertmanager tools from digest-pinned official images
and sends no messages to a real chat. Hosted checks remain separate from local
evidence below.

The alerting layer evaluates **OK, WARN, ALARM, and NODATA**, routes notifications
through Alertmanager, and sends plain-text Telegram messages through a bounded
SQLite outbox. It requires the Prometheus metric contracts and recording rules
in the monitoring deployment. It never treats an Alertmanager `resolved` event
as evidence of recovery.

This is an operational starting configuration, not a production SLO or an engine
durability qualification. The engine is still being developed; missing native
instrumentation and unconfigured exporters remain visible as NODATA.

## Components and open-source choices

Prometheus owns scraping, recording rules, freshness checks, and threshold
evaluation. Alertmanager owns grouping, routing, deduplication, silences, and
inhibition. The Python standard-library transport only delivers authenticated
webhooks to one configured Telegram chat. It has no storage-engine write access.

The choices follow these primary sources:

- [Prometheus alerting overview](https://prometheus.io/docs/alerting/latest/overview/)
  describes the rule/notification boundary.
- [Prometheus rule syntax](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
  and [rule unit tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)
  provide `for` and executable transition fixtures.
- [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/)
  supplies native Telegram delivery, grouping, inhibition, and receivers.
- [Grafana missing-series behavior](https://grafana.com/docs/grafana/latest/alerting/guides/missing-data/)
  explains why a resolved notification can represent disappearing data rather
  than recovery. Grafana-managed rules are an alternative, but this deployment
  keeps evaluation in Prometheus and uses Grafana for viewing.
- [Telegram Bot API](https://core.telegram.org/bots/api) specifies the send API
  and `retry_after`; the [Bots FAQ](https://core.telegram.org/bots/faq) describes
  update delivery and chat rate limits.

The native Alertmanager Telegram receiver is sufficient for many installations.
This transport adds a disk outbox, bounded history, chat-wide persisted flood
backoff, and a fresh Prometheus check immediately before sending any queued OK.
The [pinned Telegram notifier implementation](https://github.com/prometheus/alertmanager/blob/v0.35.0/notify/telegram/telegram.go)
uses a generic retry verdict for Telegram failures; this configuration handles
the server's `retry_after` explicitly.

Runtime images are pinned by manifest digest in
[versions.lock.json](deploy/alerting/versions.lock.json). Python has no third-party
runtime packages. Alertmanager remains a separate Apache-2.0 upstream component;
it is not vendored into GlamDB.

## The state contract

Every configured check has exactly one numeric
`glamdb_alert_state{check,cluster,service,...}` sample:

- `0` = **OK**: fresh finite input, below the warning threshold.
- `1` = **WARN**: input is at or above warning and below alarm.
- `2` = **ALARM**: input is at or above alarm.
- `3` = **NODATA**: required inputs are absent, stale, non-finite, or fail a
  collector/snapshot-validity guard.

Each check is a fixed inventory entry in
[checks.json](deploy/alerting/checks.json). Missing source series therefore
produce NODATA rather than deleting the state series. A check aggregates the
selected scope to one value; copy a check with a distinct ID and narrower
selectors when individual hosts, pods, or services need separate notifications.
The initial HTTP scope is `cluster="local",service="glamdb"`; the initial pod
scope is `namespace="default",pod="glamdb"`. These are configuration examples,
not discovery of an existing deployment. Monitoring target inventories start
empty and must be populated deliberately.
Native checks additionally select `service_instance="core"`, the producer's
default stable identity. Configure a distinct `GLAMDB_METRICS_INSTANCE` per
publisher and clone/narrow selectors when monitoring multiple processes.

Raw sample timestamps must be less than 90 seconds old by default, and no more
than five seconds in the future. Recording-rule timestamps alone are insufficient
because a rule can keep evaluating while its underlying data ages. Native engine
and resource textfile snapshots also check the **value** of their producer's
timestamp metric; rereading an old file does not make it fresh. Pod CPU/memory
checks require their collector's success flag.

HTTP error and overload ratios require at least 100 requests over five minutes
to cross a threshold. A fresh, initialized idle request counter yields an OK
error ratio; missing counters do not. Latency needs at least 100 observed requests
and a finite histogram estimate, so idle latency is NODATA. A missing 5xx status
series is treated as zero errors only behind a valid request-counter/scrape guard.
HTTP 502/504 statuses are proxy symptoms, not native storage outcome metrics.
The packaged Caddy gateway exports the outer route as
`handler="subroute",server="workbench"`. Its counters, ratio denominators and
latency histogram cover **all gateway requests**, including UI, authentication,
helpers and API traffic. Nested proxy handlers have no separate histogram in
this configuration. Use native `glamdb_operations_total` for actual core-operation
counts; gateway HTTP measurements do not identify database-only requests.

The numeric state changes on evaluation. Notification holds suppress short
spikes: WARN 2 minutes, ALARM 1 minute, NODATA 2 minutes, OK 2 minutes. These are
time holds rather than separate recovery thresholds. Prometheus's internal
pending/inactive alert lifecycle does not add public state values. Freshness,
holds, evaluation cadence, and Alertmanager grouping delays all contribute to
notification latency. The generated group evaluates every 15 seconds.

There is no `keep_firing_for`: retaining an old state after inputs disappear
would violate exclusivity. All webhook receivers use `send_resolved: false`.
Recovery is a firing **OK** state after the healthy hold, and the transport
rechecks the exact state identity, value zero, and a state-sample age below 90
seconds before sending it. Freshness uses `timestamp()` inside PromQL; an instant
query's JSON timestamp alone can represent query time. Missing, mismatched, stale, or unreachable Prometheus
data discards that queued recovery. A delayed message states its observation
time; it is never a new measurement made by Telegram.

## Checks and configurable starting points

Thresholds below are deliberately conservative examples. The full PromQL,
freshness guards, labels, explanations, and NODATA routing switches live in
`checks.json`; review that file before enabling notifications.

- Gateway scrape failure: ALARM for a measured `up=0`; disappearance is NODATA.
  This checks scrape reachability, not production readiness or a durable write.
- HTTP 5xx and 502/504 ratios: WARN 1%, ALARM 5%, with the minimum traffic above.
  HTTP p95 latency: WARN 0.5 seconds, ALARM 2 seconds. Overload 429/503 uses 1%/5%.
- HTTP 401/403 responses: WARN 1 request/second, ALARM 5 requests/second over five
  minutes. Expected denials and attacks cannot be distinguished from statuses.
- Host CPU: WARN 80%, ALARM 95% busy on the busiest selected host. Host memory:
  WARN 85%, ALARM 95% unavailable according to Linux `MemAvailable`.
- Filesystem bytes and inodes: WARN below 20% available, ALARM below 10%.
  Select the real store mount and inspect excluded pseudo/container filesystems.
  These are host filesystem checks, not proof of engine capacity admission.
- Network errors: 1/10 per second; drops: 10/100 per second, excluding loopback.
  Major page faults: 10/100 per second. Any host OOM kill in five minutes alarms.
- Pod CPU: 1.6/1.9 cores for an example two-core budget. Throttling: 0.1/0.5
  throttled seconds/second. Total memory: 80%/95% of an example 1 GiB budget;
  anonymous memory: 70%/90% of that budget. Adjust absolute thresholds to actual
  quotas; anonymous memory is kept separate from reclaimable file cache.
- Native admission: 4/16 pending synchronous calls, and 64/256 MiB owned input.
  These are not an asynchronous durable job queue. Any `commit_unknown` outcome
  in five minutes alarms; native operation errors use 0.1/1 per second.
- Alloy Loki-write drops: 0.1/1 entry per second. Exporter configuration must
  scrape the real collector; an absent drop counter is not fabricated zero.
- Loki WAL filesystem usage: WARN 80%, ALARM 90%. Despite its `_percent` suffix,
  `loki_ingester_wal_disk_usage_percent` is a fraction from zero to one. Any
  `loki_ingester_wal_disk_full_failures_total` increase in five minutes alarms;
  it counts guard events, not every rejected write. Keep the production WAL
  guard enabled; see the [Loki WAL guide](https://grafana.com/docs/loki/latest/operations/storage/wal/).
- Alertmanager and transport scrape availability, outbox occupancy at 50%/80%,
  and rejected/exhausted/expired deliveries cover the notification pipeline.
- Administrative anomalies reserve `glamdb_admin_anomalies_total`. No current
  administrative-audit producer supplies this metric. The state stays NODATA;
  HTTP authentication statuses do not fabricate an administrator audit trail.

Host/pod/native/log/admin NODATA notifications are muted by default. They remain
queryable in Prometheus and visible as active alerts in Alertmanager. Gateway,
HTTP error-series, and notification-component disappearance are enabled. Turn
`notify_nodata` on per check after its exporter and selectors are verified.
WARN/ALARM notifications become active when those inputs actually exist.

Edit the source configuration, regenerate, then validate before reloading:

```sh
python3 tools/alerting/generate.py
promtool check rules deploy/alerting/generated/states.yml
promtool test rules deploy/alerting/tests/states.test.yml
amtool check-config deploy/alerting/alertmanager.yml
```

Do not edit the generated JSON-compatible YAML directly. If adding/removing
checks, update the inventory-count assertions in the unit fixtures too.

## Install Telegram and local secrets

All commands below run from the GlamDB checkout root. Docker Engine/Desktop
with Compose v2, or a compatible Linux container runtime, is required for the
Compose deployment. The separate monitoring deployment must already provide
Prometheus, its canonical recording rules, and the `glamdb-observability` network.
An existing Grafana instance can view these state metrics through Prometheus;
its alerting engine does not need to run these rules a second time.

1. Open the official [BotFather](https://t.me/BotFather). Send `/newbot`, choose
   a display name and a unique username ending in `bot`, and save the bot token
   in a password manager. The official [bot tutorial](https://core.telegram.org/bots/tutorial)
   explains this flow. Create a dedicated notification bot so another
   application's webhook or long poller does not consume setup updates.
2. Choose the destination. For a private chat, open the new bot and press
   **Start** or send `/start`; a bot cannot initiate a private conversation.
   For a group, add it through the group's member controls and allow sending
   messages. Normal group membership is sufficient; administrator rights and
   disabled privacy mode are unnecessary. Send `/start@YourBotUsername` in that
   group so an addressed command reaches the bot with privacy mode enabled.
   For a channel, add it as an administrator with **Post messages** permission.
   This transport uses the chat's general destination, not a selected forum topic.
3. Check the token without printing it or placing it in a command URL:

   ```sh
   python3 tools/alerting/telegram.py getme
   python3 tools/alerting/telegram.py chats
   ```

   Until files exist, these commands prompt for the token with input hidden.
   `chats` prints only numeric chat IDs and chat types, not message contents or
   participants. Keep the negative sign for group/supergroup/channel IDs. Do not
   substitute the bot's user ID for the destination chat ID. If no update appears,
   send a new addressed command and retry. If Telegram reports a conflict, stop
   another poller or use a dedicated bot; do not remove an unrelated webhook.
   [`getUpdates`](https://core.telegram.org/bots/api#getupdates) and the
   [FAQ](https://core.telegram.org/bots/faq#getting-updates) explain that webhooks
   and polling cannot consume updates simultaneously.
4. Create the local files:

   ```sh
   python3 tools/alerting/configure.py
   ```

   Enter the token at the hidden prompt and the verified numeric chat ID.
   The command creates `artifacts/alerting/secrets/telegram_bot_token`,
   `telegram_chat_id`, and a random `alert_webhook_bearer`. `artifacts/` is ignored.
   Keep the directory outside version control and backups shared with others.
   Its mode is 0700. The files are 0444 inside that private directory because
   Compose file-backed secrets do not remap host ownership/mode and both
   containers run as non-root users. The mounts are read-only. Anyone granted
   access to the container runtime can read mounted secrets; protect that access.
   Compose's [secrets reference](https://docs.docker.com/reference/compose-file/secrets/)
   and [service secret behavior](https://docs.docker.com/reference/compose-file/services/#secrets)
   describe these semantics. A managed secret store may provide the same files.
5. Make one explicit manual delivery test after verifying the destination:

   ```sh
   python3 tools/alerting/telegram.py send-test
   ```

   This command sends a clearly labeled setup-test message to the configured
   chat. Automated tests never run this command or contact Telegram. If it fails,
   check the bot's membership, message permission, numeric ID and token; errors
   suppress URLs because Telegram embeds the token in the request path.

Do not put tokens in `.env`, Compose arguments, pasted curl URLs, issue bodies,
screenshots, or shell history. The runtime accepts mounted files, including
paths selected through `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_CHAT_ID_FILE`, and
`ALERT_WEBHOOK_BEARER_FILE`; raw secret values are not required in environment
variables. For an alternate directory, use an absolute `ALERTING_SECRETS_DIR`
and pass the same directory to the setup helpers.

## Start, connect, and verify

First configure actual scrape targets and their cluster/service labels in the
monitoring inventories. Ensure the native/resource snapshot producers keep
running; node-exporter textfile scraping alone cannot refresh a stopped producer.
Do not point the disposable mock adapter at a production store to generate data.

```sh
docker network inspect glamdb-observability
docker compose -f deploy/alerting/compose.yml config --quiet
docker compose -f deploy/alerting/compose.yml up -d --build
docker compose -f deploy/monitoring/compose.yml \
  -f deploy/alerting/monitoring.override.yml config --quiet
docker compose -f deploy/monitoring/compose.yml \
  -f deploy/alerting/monitoring.override.yml up -d prometheus
```

If installing monitoring for the first time, create the network through its
documented setup before starting either project. The override retains the
monitoring project's seven-day/5 GB retention and persistent data volume, selects
`prometheus-alerting.yml`, and mounts the generated rules. This config adds the
Alertmanager endpoint and scrapes both notification components. It does not add
or discover workload targets automatically.

The default local endpoints are [Alertmanager](http://localhost:9093),
[transport health](http://localhost:9087/healthz),
[transport metrics](http://localhost:9087/metrics), and
[Prometheus](http://localhost:9090). Published ports bind to loopback. The Docker
network is private operational infrastructure; do not expose these administrative
APIs directly to the internet.

Check all of the following:

1. Prometheus `/targets` shows the configured gateway/exporters, Alertmanager,
   and transport scraping successfully. `/rules` shows the four-state group
   evaluating without errors.
2. Query `glamdb_alert_state` and `glamdb_alert_value`. Confirm each expected
   check has one state, with the intended scope labels. NODATA on an unconfigured
   native/host check is expected. A healthy `up` target does not prove a workload
   snapshot is current.
3. In Alertmanager, confirm the appropriate state after its hold and grouping
   delay. Initial healthy checks can send an OK message; this is a verified
   starting state, not a recovery from a previous incident.
4. Inspect `glamdb_alert_transport_pending` and
   `glamdb_alert_transport_events_total`. A webhook 202 means persisted acceptance,
   not Telegram delivery. `outcome="delivered"` means Telegram accepted the API
   call, not that a human read it.
5. Run an intentional failure only on a disposable test endpoint. Observe ALARM,
   remove its scrape target to observe NODATA, then restore it for a fresh OK.
   The automated stack test exercises this sequence entirely on loopback.

## Notification behavior, persistence, and silences

Alertmanager groups by cluster, service, check, namespace, pod, and node. It waits
30 seconds initially, checks group changes every minute, and repeats problem
notifications every four hours. OK repeats every 24 hours. NODATA inhibits other
states of the same identity; ALARM inhibits lower severities during transitions.
Silences match these same stable labels. A muted NODATA is still visible.

To perform maintenance, create a time-bounded silence in the Alertmanager UI
with `check`, `cluster`, and `service` matchers; narrow namespace/pod/node when
needed. Set an owner, explanation, and end time. Verify it matches the intended
alerts. The [Alertmanager API](https://prometheus.io/docs/alerting/latest/clients/)
and `amtool silence add --help` support the same workflow. Silence creation
cannot retract a notification already persisted in the delivery outbox. For
immediate maintenance quiet, stop `alert-transport`, apply the silence, and let
older queued messages expire or inspect the outbox before restarting it.

Alertmanager's named volume retains silences and its notification log; it is
not a durable archive of active alerts. Prometheus continuously resends active
states after either process restarts. The transport's own named volume retains
pending messages, event identities, attempts, retry deadlines, and chat-wide
throttling. The default queue holds at most 1,000 pending identities and at most
2,000 total identities including bounded recent history, with one latest queued
state per identity. History is pruned as new webhooks are accepted.
Newer states replace unsent older ones; late older states are ignored. A duplicate
within four hours is suppressed, allowing later Alertmanager problem reminders.

A full queue returns HTTP 503 to Alertmanager and rolls back the entire webhook
batch. Request bodies are limited to 64 KiB and 100 alerts. A single delivery
worker and one HTTP request handler bound concurrent work. Each Telegram request
has a five-second timeout. Successful sends are paced three seconds apart for
the single destination. Flood responses persist a chat-wide `retry_after` pause;
other queued alerts cannot bypass it. Other transient errors use exponential
backoff with jitter, capped at 300 seconds between attempts, up to 20 attempts.
Server-requested longer pauses take priority. Problem messages expire after 24
hours and OK after five minutes. Rejected 4xx requests, exhausted attempts, expired
messages and unverified recoveries have separate metrics. Redirects are refused.

Telegram does not provide an idempotency key for `sendMessage`. If Telegram
accepts a request and its response is lost before the SQLite acknowledgement,
retry can duplicate that message. Delivery is therefore at least once across
that boundary; it is not exactly once. The event ID printed in each message
helps identify duplicates. Preserve both named volumes across normal restarts.

All delivery secrets come from local configuration, never webhook labels or
annotations. An incoming `chat_id` is ignored. Generated summaries contain no raw
logs, request bodies, or tokens, and messages use no Telegram parse mode. Keep
custom annotations free of sensitive data. Error output never renders request
URLs or exception text that could contain a token.

## Reload, rotate, and rollback

After changing thresholds or selectors, regenerate and run the checks, then:

```sh
docker compose -f deploy/monitoring/compose.yml \
  -f deploy/alerting/monitoring.override.yml kill -s HUP prometheus
docker compose -f deploy/alerting/compose.yml kill -s HUP alertmanager
```

Prometheus and Alertmanager reject invalid replacement configurations. Inspect
their rule/config health after reloading. Changing a check ID or its scope creates
a different identity; expect a fresh hold and initial-state notification.

For token compromise, use BotFather's `/revoke` for this bot and obtain its new
token. Revoke membership/access as needed. Replace local secrets with
`python3 tools/alerting/configure.py --rotate`, then recreate both alerting
containers using `up -d --force-recreate`. Rotation also replaces the shared
webhook bearer; both services must see the same value. Atomic replacement of a
host secret file does not update an existing file bind mount, so container
recreation is required. Never test or publish a revoked token.

To disable notifications while preserving pending work:

```sh
docker compose -f deploy/alerting/compose.yml stop alert-transport
```

To return monitoring to its base configuration and stop this stack:

```sh
docker compose -f deploy/monitoring/compose.yml up -d prometheus
docker compose -f deploy/alerting/compose.yml down
```

These commands preserve named volumes. Do not add `-v`: it destroys silences,
notification history, and the durable outbox. Back up stopped-volume data before
deliberate deletion. Pausing Telegram delivery or silencing a check does not
change metric collection or its recorded state.

## Executable validation and limits

Download official Prometheus and Alertmanager archives for the host platform
from [Prometheus downloads](https://prometheus.io/download/) and verify their
published SHA-256 checksums. Put `promtool`, `prometheus`, `amtool`, and
`alertmanager` on PATH, or pass all four executable paths to the checker:

```sh
python3 tools/alerting/check.py
```

The checker verifies deterministic generation, syntax, ten PromQL scenario
groups, Alertmanager configuration, Python unit tests, real webhook/fake Telegram
HTTP, and actual Prometheus/Alertmanager subprocess tests. Local sockets are
required. Direct unittest discovery skips the five subprocess tests unless
`PROMETHEUS_BIN` and `ALERTMANAGER_BIN` are set; the full checker requires them.
There are no live Telegram calls in any test.

Executed validation used macOS arm64, Python 3.14.3, Prometheus 3.13.5 LTS and
3.16.0, and Alertmanager 0.35.0. Tests cover threshold boundaries, minimum traffic,
idle counters versus missing histograms, stale underlying samples, an old OK
sample hidden by an instant query's recent timestamp, a replay of the captured
Caddy 2.11.9 outer-route histogram, absent
inventory, stale native snapshots, short spikes, ALARM-to-NODATA without false
recovery, fixed destination routing, bearer authentication, payload/queue bounds,
coalescing, out-of-order events, persistent dedup/backoff, 429/5xx/4xx, redirects,
actual silences across restart, and the complete healthy/down/removed-target flow.
Official image-manifest digests and Compose configuration were checked. Container
image build/runtime and live Telegram delivery were not exercised in that run.

No in-process component can independently page when the host, Prometheus,
Alertmanager, the transport, and its network all fail together. Install an
independent external dead-man/blackbox observer and another notification path
for that failure domain. A process or scrape check does not establish durable
write availability. This deployment does not invent administrative audit events,
unobserved storage phases, or hardware health data from missing instrumentation.

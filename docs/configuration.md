# Configuration

Environment variables are the primary interface, because that is all the Unraid
template UI can offer. A YAML file is also accepted. Environment variables win.

Nested settings use a double underscore: `TETHERD_PROBE__ENABLED=true`.

Lists accept `a,b`, `a b`, `a, b`, or a JSON array. You do not have to type
JSON into a web form.

| Setting | Env | Default | Notes |
| --- | --- | --- | --- |
| provider | `TETHERD_PROVIDER` | required | Name or ID of the container whose network the others borrow. |
| include | `TETHERD_INCLUDE` | empty | If set, *only* these dependents are managed. Exact names. |
| exclude | `TETHERD_EXCLUDE` | empty | Dependents to leave alone. |
| require_label | `TETHERD_REQUIRE_LABEL` | false | Only manage containers labelled `tetherd.enable=true`. |
| adopt_orphans | `TETHERD_ADOPT_ORPHANS` | true | Claim dependents whose provider ID no longer exists. Turn off if you run more than one provider. |
| reconcile_interval_seconds | `TETHERD_RECONCILE_INTERVAL_SECONDS` | 300 | Full pass cadence, behind the event stream. |
| event_debounce_seconds | `TETHERD_EVENT_DEBOUNCE_SECONDS` | 5 | Quiet period after a provider event so a recreate is one pass, not five. |
| state_dir | `TETHERD_STATE_DIR` | `/config` | Snapshots and provider ID history. |
| snapshot_retention | `TETHERD_SNAPSHOT_RETENTION` | 5 | Known-good configs kept per container. |
| dry_run | `TETHERD_DRY_RUN` | false | Report without changing anything. Also `tetherd run --dry-run`. |
| restart_grace_seconds | `TETHERD_RESTART_GRACE_SECONDS` | 15 | How long a repaired container has to come up before escalating. |
| docker_host | `TETHERD_DOCKER_HOST` | default socket | Override the Docker endpoint. |
| log_level | `TETHERD_LOG_LEVEL` | INFO | DEBUG, INFO, WARNING, ERROR. |
| log_format | `TETHERD_LOG_FORMAT` | console | `console` or `json`. |
| probe.enabled | `TETHERD_PROBE__ENABLED` | false | Actively test that the provider can route. Prefer a Docker healthcheck on the provider if it has one. |
| probe.targets | `TETHERD_PROBE__TARGETS` | `1.1.1.1 8.8.8.8` | Host, or `host:port` for a TCP connect. |
| probe.timeout_seconds | `TETHERD_PROBE__TIMEOUT_SECONDS` | 5 | Per probe command. |
| probe.failures_before_restart | `TETHERD_PROBE__FAILURES_BEFORE_RESTART` | 3 | Consecutive failed *rounds*, not packets. |
| probe.restart_provider_on_failure | `TETHERD_PROBE__RESTART_PROVIDER_ON_FAILURE` | true | Set false to be told without Tetherd restarting the VPN container. |
| probe.settle_seconds | `TETHERD_PROBE__SETTLE_SECONDS` | 10 | Wait after restarting the provider before repairing dependents. |
| probe.min_restart_interval_seconds | `TETHERD_PROBE__MIN_RESTART_INTERVAL_SECONDS` | 300 | Floor so an ISP outage does not restart the VPN container in a loop. |
| notify.urls | `TETHERD_NOTIFY__URLS` | empty | [Apprise](https://github.com/caronc/apprise) URLs. |
| notify.unraid | `TETHERD_NOTIFY__UNRAID` | true | Use Unraid's notifier when `/usr/local/emhttp/webGui/scripts/notify` is present. Harmless elsewhere. |
| notify.hook | `TETHERD_NOTIFY__HOOK` | empty | Executable run after a remediation, with `TETHERD_*` in its environment. |
| notify.notify_on_healthy_runs | `TETHERD_NOTIFY__NOTIFY_ON_HEALTHY_RUNS` | false | Otherwise a quiet pass is silent, on purpose. |

A YAML file at `/config/tetherd.yaml`, or at the path in `TETHERD_CONFIG_FILE`,
looks like this:

```yaml
provider: gluetun
exclude:
  - plex
probe:
  enabled: true
  targets:
    - 1.1.1.1
    - 8.8.8.8
notify:
  urls:
    - discord://id/token
```

`include` and `exclude` cannot name the same container. The provider cannot be
in `include`. Names are matched in full; `radarr` will never select `radarr-4k`.

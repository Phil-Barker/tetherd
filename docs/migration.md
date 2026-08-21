# Migrating from Rebuild-DNDC

Stop Rebuild-DNDC before starting Tetherd. Two processes repairing the same
containers is how a rename-aside becomes a name collision, and it is how the
original's stop/rm/recreate could race Tetherd's own rebuild.

Tetherd is not a fork and does not read Rebuild-DNDC's state files. What you
bring across is the *intent*: which container is the provider, which
dependents exist, whether to probe the tunnel, and where to send
notifications.

## Generate a starting config

Dump the old container's environment to a file on the host, then feed it to
`tetherd import-rdndc`. The importer ignores everything that is not a
Rebuild-DNDC key, so a full `env` dump is fine.

```bash
docker exec Rebuild-DNDC env > rdndc.env

docker run --rm -v "$PWD/rdndc.env:/rdndc.env:ro" tetherd:local \
  tetherd import-rdndc --env-file /rdndc.env
```

Or pass `--format env` for Unraid template fields instead of YAML. From a
checkout, `uv run tetherd import-rdndc --env-file rdndc.env` does the same
without Docker.

The command prints what it mapped, and comments for everything it refused to
guess. Read the comments. A few of them are load-bearing.

## What maps

| Rebuild-DNDC | Tetherd |
| --- | --- |
| `mastercontname` | `TETHERD_PROVIDER` |
| `mastercontconcheck=yes` | `TETHERD_PROBE__ENABLED=true` |
| `ping_ip`, `ping_ip_alt` | `TETHERD_PROBE__TARGETS` |
| `sleep_secs` | `TETHERD_PROBE__SETTLE_SECONDS` |
| `cron` of the form `*/N * * * *` | `TETHERD_RECONCILE_INTERVAL_SECONDS=N*60` |
| `discord_url`, `gotify_url` | `TETHERD_NOTIFY__URLS` |
| `cont_list` | `TETHERD_INCLUDE` — see below |

## What does not map, and why

**`ping_count`.** Rebuild-DNDC documented this as a retry count. It was the
packet count of a single `ping`. Tetherd counts consecutive failed probe
*rounds* instead (`TETHERD_PROBE__FAILURES_BEFORE_RESTART`, default 3). Copying
`4` across would not have meant what you thought.

**`run_startup`.** Tetherd always reconciles once at start.

**`save_no_mcontids`.** Tetherd keeps the last 10 provider IDs. That is enough
to recognise orphans across a reboot.

**`cont_list`.** Rebuild-DNDC used this only for the manual `rebuildm`
command. Tetherd's `include` *restricts* automatic management to those names.
If you want every dependent of the provider managed — Rebuild-DNDC's default
— delete `include` after the import.

**`rutorrent_pf` and friends.** Port forwarding into a torrent client is
intentionally out of scope. gluetun can do that itself.

**A richer cron than "every N minutes".** Tetherd is event-driven with a
periodic safety net, not a cron wrapper.

## Templates and labels

You do not need to change how Unraid presents the containers. Tetherd rebuilds
from a live Docker inspect, so `net.unraid.docker.managed`, `.icon`, `.webui`
and `.shell` survive. That is the opposite of Rebuild-DNDC, which dropped the
icon and WebUI link on every rebuild and could make a container show as
"3rd Party".

Do fix the templates if `tetherd doctor` flags them. Tetherd's own rebuilds
do not use the XML, but **Apply** in the Docker tab still does. A template
that publishes ports while routing through gluetun will fail in Unraid's
hands the same way it failed in Rebuild-DNDC's.

## After switching

```bash
docker exec tetherd tetherd doctor
docker exec tetherd tetherd run --once --dry-run
```

If that looks right, leave the daemon running. The first real pass will
snapshot every healthy dependent. Those snapshots are what a later rebuild
replays.

# Troubleshooting

Start with `tetherd doctor`. It exists because "nothing happened and I cannot
tell why" was the dominant support burden on Rebuild-DNDC, and most of those
threads were a wiring mistake that the tool never mentioned.

```bash
docker exec tetherd tetherd doctor
docker exec tetherd tetherd status
```

## doctor cannot reach Docker

The socket is not mounted, or it is mounted read-only. Tetherd needs
`/var/run/docker.sock` read-write. On Unraid that path is the same as on
plain Linux.

## the provider does not exist

`TETHERD_PROVIDER` is a container **name or ID**, matched in full, case
sensitive. `gluetun` will not find `GluetunVPN`. `docker ps -a --format '{{.Names}}'`
is the source of truth.

## a container I named in include is "missing"

It is not borrowing the provider's network, so Tetherd cannot see it. On
Unraid this is almost always Network Type left at `bridge` while Extra
Parameters set `--net=container:…`. Set Network Type to None, or use the
Network Type dropdown itself. See [network-wiring.md](network-wiring.md).

## nothing happens when the VPN restarts

Two different failures look like "the VPN restarted" from the outside.

If the VPN container **kept its ID** (a `docker restart`, or Unraid's Appdata
Backup stopping and starting it), Tetherd should restart the dependents. Check
`status` for `stale_namespace`. If you do not see that, the dependents may not
be using `container:` at all — confirm with `docker inspect`.

If the VPN container was **recreated** (a new ID), Tetherd rebuilds
dependents from snapshots. A rebuild needs a snapshot, which is only taken
when a container is observed healthy. A brand-new install has no snapshots
yet; the first healthy pass records them. `status` shows `no snapshot yet`
until then.

## a container shows as 3rd Party in the Unraid Docker tab after a rebuild

That label was already missing before Tetherd touched it. Tetherd preserves
`net.unraid.docker.managed`, `.icon`, `.webui` and `.shell`; it does not
invent them. `doctor` reports how Unraid will present each managed container.

## doctor says the template publishes ports

Tetherd's own rebuild will strip those and succeed. Pressing Apply in the
Docker tab will use the template, and the daemon will refuse the create.
Remove the port mappings from the template; they belong on the provider.

## the VPN is up but nothing can reach the internet

That is the failure Unraid does not notice. If the provider image ships a
healthcheck (gluetun does), Tetherd uses it. Otherwise enable
`TETHERD_PROBE__ENABLED=true`. If probing reports it cannot find `ping`/`nc`/
`wget` in the image, add a healthcheck to the provider instead — Tetherd will
not restart a VPN container on the strength of a missing binary.

A WAN outage looks identical to a dead tunnel from inside the namespace.
Restarts are therefore rate-limited (`min_restart_interval_seconds`, default
five minutes) so an ISP blip does not bounce every dependent on a loop.

## two Tetherd containers, or Tetherd plus Rebuild-DNDC

The second process will refuse to start: `another Tetherd process (pid …)
already holds …/tetherd.lock`. If you see repairs you did not expect, check
you do not still have Rebuild-DNDC running.

## I run two VPN containers

One Tetherd per provider, or set `TETHERD_ADOPT_ORPHANS=false` so a dead
reference that might belong to the other VPN is left alone. A *live*
container belonging to the other provider is never claimed.

## a rebuild failed and the original is named `something.tetherd-old`

Tetherd was killed between renaming the original aside and verifying the
replacement — a reboot, an OOM. The next pass puts it back. If you need to
do it yourself: `docker rename qbittorrent.tetherd-old qbittorrent`.

## logs are quiet

A healthy pass is silent on purpose. Reconciling every five minutes and
notifying on success would train you to ignore it. Set
`TETHERD_NOTIFY__NOTIFY_ON_HEALTHY_RUNS=true` or `TETHERD_LOG_LEVEL=DEBUG`
if you want the chatter.

## still stuck

`tetherd status` prints a reason for every container it examined and set
aside. That sentence is the bug report. Empirical Docker behaviour the code
depends on is in [design-notes.md](design-notes.md); if a claim there is
false on your daemon, that is a real defect.

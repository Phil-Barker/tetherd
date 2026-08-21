# Quick start

Tetherd keeps containers attached to the network of the container they route
through. The usual case is apps sharing a VPN sidecar such as gluetun via
`--network container:gluetun`.

This page gets you from nothing to `tetherd doctor` reporting a healthy layout.
Wiring pitfalls live in [network-wiring.md](network-wiring.md). Unraid-specific
notes, including why this is still useful on 7.x, are in
[unraid.md](unraid.md).

## 1. Wire the dependents

Every container Tetherd should manage must borrow the provider's network stack.
In Compose that is:

```yaml
network_mode: "container:gluetun"
```

On Unraid, set Network Type to `container:gluetun` (or the Extra Parameters
form `--net=container:gluetun` with Network Type **None** — never both). Do
not publish ports on the dependent. Ports belong on the container that owns
the network.

Confirm with:

```bash
docker inspect -f '{{.HostConfig.NetworkMode}}' qbittorrent
```

You should see `container:` followed by a 64-character ID. The daemon rewrites
the name to an ID at create time.

## 2. Run Tetherd

The only required setting is the provider's name.

```bash
docker run -d \
  --name tetherd \
  --restart unless-stopped \
  -e TZ=Europe/London \
  -e TETHERD_PROVIDER=gluetun \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /path/to/appdata/tetherd:/config \
  philbarker79/tetherd
```

Use `tetherd:local` if you built the image yourself.

On Unraid, add the two extra mounts in [unraid.md](unraid.md) if you want the
notification bell and a template audit.

`compose.yaml` in the repo is the same thing for a generic Docker host.

Coming from Rebuild-DNDC, stop that container first — two remediations racing
is worse than none — then see [migration.md](migration.md).

## 3. Check it can see what you think it can see

```bash
docker exec tetherd tetherd doctor
docker exec tetherd tetherd status
```

`doctor` is the one to run after install. It will tell you if the provider is
missing, if an include list names a container that is not borrowing the
network, if the state directory cannot be written, and (on Unraid) if a
template would fail to recreate a container.

`status` is the same view without the pass/fail framing: every managed
container, why anything was skipped, and how old the last known-good snapshot
is.

## 4. Optional: dry-run a pass

```bash
docker exec tetherd tetherd run --once --dry-run
```

That reports what a reconcile would do, and changes nothing. Useful the first
time, or after changing `include` / `exclude`.

## What happens next

Tetherd reconciles once at start, then watches Docker events and falls back to
a full pass every five minutes. A VPN container restart becomes a restart of
each dependent. A VPN container recreation becomes a rebuild from a snapshot
of what was actually running, not from an Unraid template.

Configuration is listed in [configuration.md](configuration.md). When something
looks wrong, [troubleshooting.md](troubleshooting.md).

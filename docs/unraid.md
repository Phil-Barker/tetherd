# Unraid

Tetherd is a generic Docker tool. Unraid is the flagship platform because
that is where `--network container:gluetun` is common, and where the previous
attempt at this job accumulated most of its scars.

The notes below were checked against Unraid 7.3.2 (kernel 6.18.38-Unraid,
Docker Engine 29.5.3) with 82 user templates.

Install from Community Applications once the template is listed (search
**Tetherd**). Until then the XML is
[templates/tetherd.xml](../templates/tetherd.xml). How that listing is
submitted is in [publishing.md](publishing.md).

## Why this is still needed on Unraid 7.x

Unraid 7.x can recreate a container that used `container:` networking, which
7.x users reasonably hoped would make a helper like this obsolete. Two gaps
remain.

The common failure is a **restart**, not a recreate. Appdata Backup, a VPN
client reconnecting, or `docker restart gluetun` keeps the container ID and
replaces the network namespace. Every dependent still *looks* correctly
attached and is silently offline. Unraid does not detect that. Neither did
Rebuild-DNDC, which only compared IDs. That is upstream issue #72, and it is
why the tool still has a job.

The second gap is a VPN container that is **running but not routing**.
Unraid's Docker tab shows it as up. Tetherd can use the image's own
healthcheck (gluetun ships one) or an exec probe.

## Extra mounts

The generic run in the README is enough to repair containers. These extra
mounts unlock Unraid-native behaviour:

```
-v /tmp/notifications:/tmp/notifications
-v /usr/local/emhttp:/usr/local/emhttp:ro
-v /boot/config/plugins/dockerMan/templates-user:/config/docker-templates:ro
```

`/tmp/notifications` is where Unraid 7 stores `.notify` files. Tetherd writes
those directly: Unraid's GraphQL API watches the directory, which is how the
bell updates. The host `notify` script is PHP and cannot run in this image —
Unraid's own API falls back to the same files when that script fails. Email
and other *agents* still need [Apprise](configuration.md) or a hook.

`/usr/local/emhttp` is the Unraid marker and is used by `tetherd doctor` for
the template audit, not for notifications.

The templates directory is **read-only**. Tetherd never rebuilds from those
files.

## Labels

Unraid decides how to present a container from four labels read off it:

- `net.unraid.docker.managed`
- `net.unraid.docker.icon`
- `net.unraid.docker.webui`
- `net.unraid.docker.shell`

Losing them is what makes a container show as "3rd Party", with no icon and
no WebUI link. Rebuild-DNDC dropped the last three on every rebuild.
Tetherd replays a live inspect, so they survive.

A fifth label, `net.unraid.docker.template`, is **not in use** on 7.3.2. The
community fix proposed for Rebuild-DNDC issue #77 was to read the template
path from that label. It would not have worked. Tetherd matches templates
by the `<Name>` element inside the file.

## Templates outlive the containers they describe

Unraid keeps XML for apps you have removed, so Previous Apps can reinstall
them. A host with a few dozen running containers can easily have eighty
templates. Some of those will still name a VPN container you replaced years
ago.

`tetherd doctor` therefore audits templates for containers that **exist**,
never by scanning the directory. A stale file for an uninstalled app is
history you chose to keep, not a misconfiguration.

That directory-as-history is also why Rebuild-DNDC's filename substring
match was dangerous rather than merely sloppy. Searching `qbittorrent`
against filenames can land on `my-nordvpn-qbittorrent.xml`, or on an orphan
still wired to `binhex-qbittorrentvpn`.

## Network Type

On current Unraid, set Network Type to `container:<provider>`. That is what
ends up in `<Network>`. The Extra Parameters form `--net=container:…` still
works if Network Type is **None**. Using both is issue #57: Unraid records
Network Type, the container never borrows the namespace, and nothing
detects it as a dependent.

Do not put port mappings on a dependent's template. `doctor` will flag it.
Apply in the Docker tab uses the XML; Tetherd's rebuilds do not.

## Native Docker handling vs Tetherd

Let Unraid recreate a container when you press Apply. That is its job, and
Tetherd is not trying to be the Unraid UI. Tetherd's job is the cases Unraid
does not see: a provider restart that does not change the ID, a dead tunnel
behind a running process, and a host whose VPN was already replaced before
you installed the helper.

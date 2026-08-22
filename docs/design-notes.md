# Design notes

Empirical findings that the implementation depends on. Everything here was
verified against a real daemon by `scripts/spike-netns.sh`, which exits non-zero
if any claim stops holding. Re-run it when changing the supported Docker range.

Environment for the run recorded below: Docker Engine 29.6.1, API v1.55, Linux
6.8, `alpine:3.22` test containers. 17 of 17 checks passed.

Independently confirmed on a live Unraid server by `scripts/unraid-recon.sh`:
Unraid 7.3.2, kernel 6.18.38-Unraid, Docker Engine 29.5.3, API v1.54, with 82
user templates and containers routed through a gluetun VPN container. Findings
1, 2 and 3 below all hold identically there.

Note that Unraid 7.3.2 ships a Docker version close to the development machine's,
so the behaviour below is verified on current Unraid but *not* on the older
daemons that Unraid 6.x shipped. Reference handling stays deliberately tolerant
of name-form values for that reason.

## 1. A dependent container owns no network metadata

For a container created with `--network container:<ref>`, the daemon reports:

- `NetworkSettings.SandboxKey` — empty string
- `NetworkSettings.Networks` — `{}`
- `NetworkSettings.EndpointID` — **the key is absent entirely**, so
  `docker inspect --format '{{.NetworkSettings.EndpointID}}'` fails with
  `map has no entry for key "EndpointID"` rather than returning empty

This is the single most important finding, because it explains why Rebuild-DNDC
could never detect reliably. Its whole scheme is built on comparing
`EndpointID` values, a field that is structurally unavailable for exactly the
containers it manages. Upstream reads it from the *provider* instead, which does
expose it while on a default bridge network but returns empty on a custom
network — that is upstream issue #61, and the empty `ENDPOINT-ID:` line users
keep pasting into issues #57 and #71.

Tetherd therefore never reads `EndpointID`.

## 2. The daemon normalises name references to container IDs

Creating a dependent with `container:tetherd-spike-provider` results in
`HostConfig.NetworkMode` being stored as
`container:fa0e29526f22...` — the provider's full 64-character ID.

This normalisation happens in the daemon, not the CLI: issuing the same request
directly against `POST /containers/create` on the unix socket with a name-form
`NetworkMode` also yields an ID-form value on inspect.

So discovery can compare IDs and never has to reconcile a name against an ID.
The code still handles a name-form reference defensively, because this has only
been verified on 29.6.1 and Unraid 6.x ships considerably older daemons; the
cost of tolerating both forms is a few lines.

## 3. Two distinct failure modes, needing two distinct repairs

### Provider restarted in place

`docker restart <provider>` keeps the container ID but replaces the network
namespace (`SandboxKey` moved from `/run/.../5d5f41e60471` to
`/run/.../3d4a5763d155`). Afterwards:

- the dependent still reports `State.Running == true`
- its `NetworkMode` still references a live, current, correct provider ID
- its network is dead — an outbound ping from inside it fails

Every field upstream compares still looks correct, which is why this mode goes
undetected indefinitely. It is the common case in practice: a VPN container
reconnecting, or Appdata Backup stopping and starting it (issue #67), and it is
the residual gap that keeps the tool necessary on Unraid 7.x (issue #72).

The detection signal is timestamp ordering: `provider.State.StartedAt >
dependent.State.StartedAt` means the dependent is holding a namespace that no
longer exists. `State.StartedAt` is RFC3339 with nanosecond precision, so it
compares correctly as a plain string as well as a parsed datetime.

The repair is simply `docker restart <dependent>`, which re-resolves the
provider's new sandbox. Connectivity is restored with no teardown and no config
reconstruction — this is what makes the non-destructive Tier 1 path possible,
and what issues #78 and #68 were asking for.

### Provider recreated

Removing and recreating the provider yields a new container ID. The dependent
still points at the dead original ID, and `docker start` on it **fails**. A
restart cannot recover this, so it must escalate to Tier 2: recreate the
dependent from a config snapshot.

## 4. Corroborating signal for provider namespace changes

The provider's `SandboxKey` changes on every restart. Persisting the last-seen
value gives a second, clock-independent signal that the namespace was replaced,
useful as a cross-check when container clocks or timestamps look implausible.

## 5. Fields that cannot coexist with a `container:` network mode

Docker rejects a container that combines `--network container:<ref>` with any
of `--publish`, `--publish-all`, `--expose`, `--hostname`, `--dns`,
`--dns-search`, `--dns-option`, `--mac-address` or `--add-host`. The resulting
error is:

```
conflicting options: port publishing and the container type network mode
```

This is the cause of upstream issues #80, #69 and #65, where a container is
destroyed and then cannot be recreated because its Unraid template still
carries port mappings.

Testing each field individually against the daemon shows the documentation
overstates the restriction. Rejected on Docker 29.6.1:

- `Config.Hostname` — "conflicting options: hostname and the network mode"
- `Config.ExposedPorts` — "port exposing and the container type network mode"
- `HostConfig.PortBindings` — "port publishing and the container type network mode"
- `HostConfig.PublishAllPorts` — same
- `HostConfig.Dns` — "dns and the network mode"
- `HostConfig.ExtraHosts` — "custom host-to-IP mapping and the network mode"

Accepted despite being documented as unsupported: `Config.Domainname`,
`Config.MacAddress`, `HostConfig.DnsSearch`, `HostConfig.DnsOptions`,
`HostConfig.MacAddress`.

Tetherd strips the union of both groups. The accepted-but-documented-as-
unsupported fields are stripped anyway for two reasons: enforcement may differ
on the older daemons Unraid ships, and the fields are meaningless in a borrowed
namespace regardless — DNS resolution, hostname and MAC address all belong to
the container that owns the namespace, not to the one borrowing it.

## 6. Unraid specifics, verified on 7.3.2

Exactly four integration labels are in use across the whole host:
`net.unraid.docker.managed`, `.icon`, `.webui` and `.shell`. Unraid reads them
straight off the container, so preserving them is what keeps a container from
appearing as "3rd Party" in the Docker tab, with its icon and WebUI link intact.

**`net.unraid.docker.template` does not exist.** No container on the server
carries it. This matters because the fix proposed by the community for upstream
issue #77 was to read the template path out of that label instead of guessing a
filename; it would not have worked. Tetherd identifies templates by the `<Name>`
element inside the file, which needs no label.

Template structure, from 82 real templates: root element `Container`, with
`Name`, `Repository`, `Registry`, `Network`, `MyIP`, `MyMAC`, `Shell`, `WebUI`,
`Icon`, `ExtraParams`, `PostArgs`, `Privileged`, `CPUset`, `Category`, `Project`,
`Support`, `Overview`, `TemplateURL`, `DonateLink`, `DonateText`,
`DateInstalled`, `Requires` and `ReadMe`, plus repeated `Config` elements typed
`Variable` (472), `Path` (176), `Port` (85), `Label` (24) and `Device` (5).

Crucially, a borrowed network is expressed as `<Network>container:GluetunVPN</Network>`
— in the `Network` element itself, not as `--net=container:` in `ExtraParams`.
Both forms are recognised, since older templates and the upstream README's
"alternate steps" use the `ExtraParams` form, and it is the mismatch between the
two that produces issue #57.

Because the `Network` element names the provider, a template can be checked
against the provider Tetherd is actually configured for, catching an installed
container whose template would reattach it somewhere unwatched.

### Templates outlive the containers they describe

The surveyed server held 82 templates against far fewer installed containers.
Unraid keeps the XML for apps you remove so they can be reinstalled from Previous
Apps, which means the directory is a record of everything ever installed, not a
description of what is running. Four of the surveyed templates declared
`container:GluetunVPN`; a fifth, for an app uninstalled some time ago, still
declared `container:binhex-qbittorrentvpn` — an all-in-one qBittorrent/VPN image
since replaced by gluetun plus a standalone qBittorrent.

Two consequences follow.

The audit is driven by the list of containers that exist, and never by the
contents of the template directory. Scanning the directory would turn every
orphan into a warning about a file the user intentionally kept.

More importantly, this is what makes upstream's matching bug severe rather than
merely untidy. Issues #77 and #75 describe the wrong template being picked by
substring-matching a container name against filenames, and the pool being
searched contains abandoned templates carrying network wiring that was correct
years ago. A stale `binhex-qbittorrentvpn` provider in a matched orphan is
exactly the kind of value that gets applied to a live container and looks, from
the outside, like an unexplained network failure. Matching on the `<Name>`
element of a template belonging to a container that actually exists closes both
halves of that.

Also confirmed: `/usr/local/emhttp` and `/usr/local/emhttp/webGui/scripts/notify`
both exist. The script is PHP (`#!/usr/bin/php -q`) and cannot run inside
Tetherd's image; notifications are written as `.notify` files under
`/tmp/notifications`, which Unraid 7's GraphQL API watches. The
update-status cache lives at
`/var/lib/docker/unraid-update-status.json` rather than the dynamix path some
documentation cites.


# Design notes

Empirical findings that the implementation depends on. Everything here was
verified against a real daemon by `scripts/spike-netns.sh`, which exits non-zero
if any claim stops holding. Re-run it when changing the supported Docker range.

Environment for the run recorded below: Docker Engine 29.6.1, API v1.55, Linux
6.8, `alpine:3.22` test containers. 17 of 17 checks passed.

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
carries port mappings. Any payload Tetherd builds must strip the corresponding
inspect fields (`PortBindings`, `ExposedPorts`, `Hostname`, `Dns`, `DnsSearch`,
`DnsOptions`, `MacAddress`, `ExtraHosts`) and say so in the logs.

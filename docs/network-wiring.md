# Network wiring

`--network container:<other>` does not mean "put this container on the same
Docker network as `<other>`". It means "this container has no network of its
own; it uses `<other>`'s namespace". Ports, hostname, DNS and MAC address all
belong to `<other>`. Getting that wrong is how containers end up offline, or
destroyed and un-recreatable.

Tetherd will not fix a container that was never wired this way. It will tell
you, if you named it in `include`.

## The form that works

Create the dependent with the provider's **name**. The daemon stores the
provider's **full ID**.

```bash
docker run -d --name qbittorrent --network container:gluetun lscr.io/linuxserver/qbittorrent
```

```yaml
# Compose
network_mode: "container:gluetun"
```

On current Unraid, the Network Type dropdown itself accepts
`container:GluetunVPN`. That is what a live 7.3.2 server writes into the
template's `<Network>` element.

The older form still works, and is what Rebuild-DNDC's README called its
"alternate steps":

```
--net=container:gluetun
```

in Extra Parameters, with Network Type set to **None**.

## Do not combine the two forms

If Extra Parameters request `container:gluetun` and Network Type is `bridge`
(or anything other than None), Unraid records the Network Type. The container
never borrows the namespace, Tetherd never sees it, and the user is left
wondering why nothing happened. That is Rebuild-DNDC issue #57, open for years,
and the thing `tetherd doctor` is looking for when an include name does not
appear as a dependent.

## Do not publish ports on the dependent

Docker rejects the combination:

```
conflicting options: port publishing and the container type network mode
```

The same class of error fires for `--expose`, `--hostname`, `--dns` and
`--add-host`. Publish 8080 on gluetun, not on qbittorrent. WebUI links on
Unraid should use the provider's IP; `http://[IP]:[PORT:8080]` still works
because Unraid substitutes the host IP, not the container's.

Rebuild-DNDC issues #80, #69 and #65 are this mistake plus a destructive
rebuild: the container was removed, then the daemon refused to create it
again. Tetherd strips these fields on its own rebuilds and will not remove the
original until a replacement is running. `tetherd doctor` still warns, because
pressing Apply in the Docker tab uses the template, not Tetherd.

## Do not `docker network create container:gluetun`

Rebuild-DNDC's README suggested that. It creates a user-defined bridge named
`container:gluetun`, which is a different thing from `--network container:gluetun`.
Containers on that bridge have their own namespace. They do not share the VPN
container's interfaces, and they are not Tetherd's problem.

## Hostname, DNS, extra hosts

They belong on the provider. A dependent that sets them is asking for a
second opinion on a namespace it does not own. Tetherd strips them on rebuild
so the daemon does not reject the create.

## Two providers

Tetherd watches one provider per process. A container whose `NetworkMode`
points at a *live* container that is not that provider is left alone, with a
reason.

A container pointing at an ID that no longer exists is adopted by default:
nothing else can claim it, and it cannot start. That is the state a host is
in when you install Tetherd to fix it. If you run two VPN containers and
might have orphans of the other one, set `TETHERD_ADOPT_ORPHANS=false`.

## Checking the wiring

```bash
docker inspect -f '{{.Name}} {{.HostConfig.NetworkMode}} {{.State.Running}}' $(docker ps -aq)
```

Dependents show `container:` and a long ID. Independents show `bridge`,
`host`, or a network name. Anything that shows `container:gluetun` as a
*name* rather than an ID is either on a very old daemon or was not created
the way Docker normally creates it; Tetherd still accepts it.

# Tetherd

Keeps containers attached to the network of the container they route through.

If you run apps through a VPN sidecar with `--network container:gluetun`, those
apps lose their networking whenever the VPN container restarts or is recreated.
Tetherd watches for that and repairs it — preferring a plain restart, and only
rebuilding a container when it genuinely has to.

This is a clean-room successor to [Rebuild-DNDC](https://github.com/elmerfds/rebuild-dndc).
It is not a fork. It reads configuration from the live Docker API, not from Unraid
XML templates.

**Unraid:** install from Community Applications (search **Tetherd**) once the
template is listed. Until then, use the XML in the GitHub repo.

**Source and docs:** [github.com/Phil-Barker/tetherd](https://github.com/Phil-Barker/tetherd)

```bash
docker pull philbarker79/tetherd
```

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

The image talks to the host Docker socket and runs as root for the same reason
Watchtower does: that socket is typically `root:docker` mode `660`.

Also published as `ghcr.io/phil-barker/tetherd`.

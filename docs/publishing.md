# Publishing Tetherd

The files are in the repo. The remaining steps are accounts, one git tag, and
the Community Applications review form. Do them in this order: an image that
does not exist yet will fail the CA scan.

## 1. Docker Hub

1. Sign in at [hub.docker.com](https://hub.docker.com) (username **philbarker79**
   is the one the workflow publishes to).
2. Create an access token: Account Settings → Personal access tokens →
   Read, Write, Delete. Not your password.
3. The `philbarker79/tetherd` repository is created automatically on the first
   successful push. You do not need to create it by hand.

## 2. GitHub secrets

On [Phil-Barker/tetherd](https://github.com/Phil-Barker/tetherd) → Settings →
Secrets and variables → Actions, add:

| Secret | Value |
| --- | --- |
| `DOCKERHUB_USERNAME` | `philbarker79` |
| `DOCKERHUB_TOKEN` | the token from step 1 |

GHCR uses `GITHUB_TOKEN`, which GitHub already provides. No extra secret.

## 3. Cut a tag

`0.1.0` is honest while the README still says early development. `v1.0.0`
can wait until this has lived on a few Unraid boxes.

```bash
git tag -a v0.1.0 -m "Initial public release."
git push origin v0.1.0
```

That runs `.github/workflows/release.yml`: tests, then a multi-arch
(`linux/amd64`, `linux/arm64`) push to:

- `philbarker79/tetherd:0.1.0` and `:0.1` (and `:latest`)
- `ghcr.io/phil-barker/tetherd` with the same tags

It also writes [dockerhub.md](../dockerhub.md) onto the Hub page and opens a
GitHub release with generated notes.

Confirm:

```bash
docker pull philbarker79/tetherd:0.1.0
```

On Unraid you can switch the test container from `tetherd:local` to
`philbarker79/tetherd:latest` once that pull works.

## 4. Community Applications

Current Unraid process: a public GitHub repo with `ca_profile.xml` and
`templates/*.xml`, then [ca.unraid.net/submit](https://ca.unraid.net/submit).
This repository is already laid out for that. Official notes:
[Builder guide](https://ca.unraid.net/submit/help/builders),
[XML field reference](https://ca.unraid.net/submit/help/xml-field-reference).

1. **Forum thread (recommended before submit).** Create a topic under
   [Docker Containers](https://forums.unraid.net/forum/47-docker-containers/)
   titled something like `Tetherd - keep VPN-sidecar dependents online`.
   Credit Rebuild-DNDC. Put the GitHub URL and `docker exec tetherd tetherd doctor`
   in the first post. Then set `<Support>` in
   [templates/tetherd.xml](../templates/tetherd.xml) and `<Forum>` in
   [ca_profile.xml](../ca_profile.xml) to that thread URL and push.
2. Open [ca.unraid.net/submit/new](https://ca.unraid.net/submit/new), point it
   at `https://github.com/Phil-Barker/tetherd`.
3. Run **Validate**, then **Scan**. Fix whatever it flags (usually a Support
   URL, a missing icon, or the image not being pullable yet).
4. Submit for review. Moderators look at the template, the Hub image, and
   whether you look like you will support it.

Until it is in the feed, anyone can still add the template URL by hand:

`https://raw.githubusercontent.com/Phil-Barker/tetherd/main/templates/tetherd.xml`

## 5. After it is listed

A courteous note on the Rebuild-DNDC issues Tetherd actually closes, pointing
at this repo, is the last item in the original plan. elmerfds already said he
is not using the tool. That is a succession, not a fork war.

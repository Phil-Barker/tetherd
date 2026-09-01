# Publishing Tetherd

Maintainer runbook for cutting a release and listing the app in Community
Applications. Operator guides live under [docs/](../).

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

`v0.1.1` is the current public tag. `v1.0.0` can wait until this has lived
on a few Unraid boxes.

```bash
git tag -a v0.1.0 -m "Initial public release."
git push origin v0.1.0
```

That runs `.github/workflows/release.yml`: tests, then a multi-arch
(`linux/amd64`, `linux/arm64`) push to:

- `philbarker79/tetherd:0.1.0` and `:0.1` (and `:latest`)
- `ghcr.io/phil-barker/tetherd` with the same tags

It also writes [dockerhub.md](../../dockerhub.md) onto the Hub page and opens a
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

1. **Forum thread.** Support lives at
   [forums.unraid.net/topic/200447-support-tetherd](https://forums.unraid.net/topic/200447-support-tetherd/).
   That URL is already in `<Support>` and `<Forum>`. A move between forum
   sections keeps the topic ID, so the link should not change.
2. Open [ca.unraid.net/submit/new](https://ca.unraid.net/submit/new), point it
   at `https://github.com/Phil-Barker/tetherd`.
3. Run **Validate**, then **Scan**. Fix whatever it flags (usually a Support
   URL, a missing icon, or the image not being pullable yet).
4. Submit for review. Moderators look at the template, the Hub image, and
   whether you look like you will support it.

Until it is in the feed, anyone can still add the template URL by hand:

`https://raw.githubusercontent.com/Phil-Barker/tetherd/main/templates/tetherd.xml`

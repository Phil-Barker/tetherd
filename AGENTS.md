# Agent notes for Tetherd

Guidance for AI agents and maintainers working in this repository.

## Project

Tetherd watches a Docker "provider" container (typically a VPN sidecar such as
gluetun) and repairs dependents that borrow its network. It prefers `docker
restart` and only rebuilds when the provider was recreated.

- Python 3.13+, strict typing, Pydantic settings
- Source under `src/tetherd/`
- Tests under `tests/`; integration tests are marked and skipped in CI by default
- Unraid Community Applications template lives in
  [Phil-Barker/unraid-templates](https://github.com/Phil-Barker/unraid-templates);
  `templates/tetherd.xml` here is a working copy only

Operator docs: `docs/`. Maintainer runbook: `docs/maintainer/publishing.md`.

## Conventions

- Minimise scope; match existing style in surrounding code
- Do not commit unless the user asks
- Conventional Commits for messages when committing
- Never update git config

## Releases and tags

Tags use semver with a `v` prefix: `v0.1.2`.

Pushing a tag runs `.github/workflows/release.yml`: tests, multi-arch Docker
publish, GitHub release.

### Changelog links

GitHub release notes must link to a **compare** between the previous tag and the
new one, not to the tag's commit history page.

- Good: `https://github.com/Phil-Barker/tetherd/compare/v0.1.1...v0.1.2`
- Bad: `https://github.com/Phil-Barker/tetherd/commits/v0.1.2`

The compare URL shows only what changed in that release. The commits URL shows
the entire history reachable from the tag.

The release workflow generates notes via GitHub's API with an explicit
`previous_tag_name` so the compare link is produced correctly. Do not replace
that with `generate_release_notes: true` alone in the release action — that
falls back to the commits URL when no previous tag is supplied.

For the first release (`v0.1.0`), there is no previous tag; a plain release
body is fine.

When cutting a release manually, use:

```bash
git tag -a v0.1.3 -m "Short summary of why this release exists."
git push origin v0.1.3
```

After pushing a tag, also update `<Changes>` and `<Date>` in
`Phil-Barker/unraid-templates` so Unraid Community Applications shows an update
to existing installs. See `docs/maintainer/publishing.md`.

## Testing

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -m "not integration"
```

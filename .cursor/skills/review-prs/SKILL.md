---
name: review-prs
description: >-
  Review all open GitHub issues and pull requests, thank contributors, merge
  accepted PRs, and cut one batched Unraid release. Use when reviewing PRs,
  GitHub issues, merging pull requests, cutting a release, tagging, or updating
  Community Applications.
---

# Review PRs and batch the release

review all, merge when happy, release as many changes as you can within one release, not one release per bugfix/feature

## 1. Inventory everything open

List **all** open issues and **all** open PRs before reviewing or tagging. Do not stop at the first matching pair.

```bash
gh issue list --state open
gh pr list --state open
```

Read each issue and each PR (body, diff, tests). A PR that does not close the reported issue is still in scope if it is ready.

## 2. Review

For each PR:

- Check correctness against the surrounding code and Unraid/Docker behaviour it claims.
- Run the repo checks from [AGENTS.md](../../../AGENTS.md) on the PR branch.
- Merge when happy. If it is wrong, reject it or fix it yourself rather than leaving a half-right change.
- On an accepted PR, comment thanking the author for the contribution before merging. Squash-merge with a Conventional Commits subject that includes `(#N)`.

Do not cut a tag in the middle of this list.

## 3. One release for everything that landed

Only after every ready PR is merged (or rejected) and `main` contains all of those changes:

1. Bump `pyproject.toml`, `src/tetherd/__init__.py`, `uv.lock`, and `tests/test_cli.py` so `tetherd --version` matches the new tag.
2. Update `<Changes>` and `<Date>` in `templates/tetherd.xml` (working copy).
3. Annotated tag, then push it:

   ```bash
   git tag -a v0.1.4 -m "Short summary of why this release exists."
   git push origin v0.1.4
   ```

4. Copy the same `<Changes>` / `<Date>` into [Phil-Barker/unraid-templates](https://github.com/Phil-Barker/unraid-templates) `tetherd/tetherd.xml` so Community Applications shows an update.

Release notes must use a compare URL (`v0.1.3...v0.1.4`). Details: [AGENTS.md](../../../AGENTS.md), [docs/maintainer/publishing.md](../../../docs/maintainer/publishing.md).

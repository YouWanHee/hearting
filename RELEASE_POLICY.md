# Release Policy

Hearting releases are immutable delivery points, not snapshots of every
documentation change. A release is created only when `main` changes something
that can alter the installed harness or its runtime behavior.

## Version decision

The release planner compares `main` with the highest stable
`vMAJOR.MINOR.PATCH` tag reachable from it and applies the highest matching
SemVer change:

| Change since the stable tag | Release |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer in a release-relevant commit | major |
| `feat:` in a release-relevant commit | minor |
| Any other release-relevant behavior change | patch |
| Documentation, reports, fixtures, and tests only | none |

Release-relevant paths include the portable contract, capabilities, roles,
runtime adapters, hooks, installers, generators, and operational tools. Public
documentation, internal reports, CI configuration, research, and test-only
paths do not create a release by themselves. An unclassified commit that
changes a release-relevant path receives a patch release rather than being
silently skipped.

## Automation

Every push to `main` runs `Checks`. Only a successful, completed `Checks` run
from this repository's `main` push admits an automatic release. The serialized
release workflow runs the deterministic planner, builds the four release assets,
creates an annotated SemVer tag, and publishes using that run's exact commit
SHA. Failed, cancelled, pull-request, and foreign-repository runs cannot admit
a release. A later default-branch tip never replaces the tested commit. The
workflow does not rely on the generated tag starting a second workflow.

Maintainers may push an explicit SemVer tag, including a prerelease such as
`v1.2.0-rc.1`. Tag pushes and manual releases on `main` or a version tag run the
same reusable `Checks` workflow at the caller's exact commit before the release
job can run. They do not poll for another workflow or bypass the full suite.
Tag releases do not reinterpret the maintainer-selected version. Stable tags
and release assets are never moved or replaced; a correction gets a new version.

The planner and its policy fixtures can be run locally:

```bash
python3 tools/release/plan.py plan --repo . --head HEAD
./tools/release/plan.test.sh
```

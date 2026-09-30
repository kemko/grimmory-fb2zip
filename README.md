# Grimmory FB2.ZIP patches

This repository maintains a small patch series for importing new `.fb2.zip`
uploads into [Grimmory](https://github.com/grimmory-tools/grimmory). Imported books
are stored as ordinary FB2 files. Direct filesystem imports are outside this scope.

## Install a published version

After the repository owner publishes the package, replace the image in your
existing Grimmory Compose configuration with an exact tag:

```yaml
image: ghcr.io/kemko/grimmory-fb2zip:v3.5.0-fb2zip.1
```

Keep the upstream database,
volumes and environment configuration, then pull and recreate the application
service. Images target `linux/amd64` and `linux/arm64`; no `latest` tag is published.
Back up the database before upgrading. Changing the image tag does not undo
upstream database migrations.

Source and workflows are maintained in [kemko/grimmory-fb2zip](https://github.com/kemko/grimmory-fb2zip).
Check its releases and Actions runs for published images. For a new fork, enable
Actions on the default branch. After the first package is created, configure its
visibility and repository access; verify anonymous pulls if it should be public.
The package name is `grimmory-fb2zip`, independent of the repository name.
Publishing uses `GITHUB_TOKEN`; it needs no separate PAT.

## Supported uploads and limits

The patch handles library uploads, HTTP BookDrop uploads, alternative book files
and the first file attached to a physical book. Supplementary attachments keep
their original bytes, including ZIP files. Copying an archive directly into a
library or BookDrop directory is unsupported; no scanner, watcher or migration
support is added automatically for other entry points.

- The uploaded filename must end in `.fb2.zip`, ignoring case. Generic `.zip`,
  `.fbz`, nested archives and archives containing multiple books are unsupported.
- An archive must contain exactly one `.fb2` file. Empty directory entries are
  allowed, with at most 1,024 entries total. Other files, directory payloads,
  absolute paths, drive paths and `..` path segments are rejected.
- Both the uploaded archive and the actual decompressed bytes must fit Grimmory's
  `maxFileUploadSizeInMb` setting (100 MiB by default at the baseline). Existing
  HTTP and proxy upload limits also apply. ZIP size headers are not trusted.
- JDK `ZipFile` checks the central directory; bounded streaming reads and explicit
  CRC checks reject damaged archives. Encrypted and unsupported ZIPs are rejected.
- Secure XML parsing requires a `FictionBook` root and rejects `DOCTYPE`. The
  original FB2 bytes and encoding are preserved; XML is not reserialized.

The external name `Книга.FB2.ZIP` becomes `Книга.fb2` before the usual naming rules.
Size and hash use the extracted bytes. The application's temporary archive is
removed; the user's original upload remains on their machine. Existing files
are not overwritten. Increase limits only after checking real books and the
memory cost of upstream's DOM-based XML parser.

## Upstream baseline

`upstream.json` pins `v3.5.0` to
`402e89b4452f8e2b17ab95f16c1621c003516cd2`. The upstream remote tag was verified on
2026-09-29. At this tag, `Justfile`, `backend/Justfile`, `frontend/Justfile`,
`Dockerfile`, and `.github/workflows/test-suite.yml` provide the build commands.
The required tools are JDK 25, Node 24.15+, pnpm 11.19.0, `just`, Python 3, Git, and
Docker with Buildx. Local checks used Node 24.21.0. Tag discovery and publication also use `gh` and `skopeo`.
The Gradle wrapper selects 9.7.1; the Docker builder base is
Gradle 9.5.1. Backend CI also installs libarchive.

## Prepare an upstream checkout

Run from this repository root:

```sh
bash scripts/prepare-upstream.sh v3.5.0 .work/upstream
```

Every destination must be a **new** directory below this repository's `.work/`.
Existing directories, including clean and dirty Git checkouts, are rejected and
left untouched. Failed preparations remove only the checkout created by that run.
A retry uses the same now-absent destination. To retain multiple successful builds,
choose another destination.

For another stable tag, pass its expected full commit SHA as the third argument.
The script fetches exactly that tag from the approved upstream repository and
verifies its commit before applying patches. It refuses a different SHA for the
pinned baseline. `patches/series` lists patches in application order; each must
pass `git apply --check` before `git apply --index`. A conflict stops preparation.
No upstream workflows run during preparation.

Offline development and tests may use a local Git repository explicitly:

```sh
bash scripts/prepare-upstream.sh v3.5.0 .work/upstream \
  --local-source /absolute/path/to/grimmory
```

`--local-source` accepts only an existing absolute local directory, never a URL.
The same tag and SHA checks apply. Only committed contents are fetched; working
changes in the source are ignored. Publication workflows must omit this option.

## Releases and maintenance

`publish.yml` checks all stable `vMAJOR.MINOR.PATCH` tags at or above `minimum_tag`
every hour at minute 17. Tags are sorted numerically, including annotated tags;
a GitHub Release in upstream is not required. `publish-version.yml` runs the
reusable checks and build for each pending version independently, with at most
two versions running at once. One failure does not cancel the other versions.
More than 256 pending versions fails explicitly; process selected tags manually
before resuming polling.

Only scheduled or manual runs on the default branch can publish. Pull requests
run checks without publication. In Actions, select **Publish patched versions**,
then **Run workflow** on the default branch:

1. Set `upstream_tag` to an exact tag such as `v3.5.0`; an empty value checks all
   supported tags.
2. Leave `dry_run=true` to run checks, smoke tests and both architecture builds
   without registry login, image push or release writes.
3. After a successful dry run, repeat with `dry_run=false` to publish. Rerunning
   an already published version validates its identity and finishes any missing
   release metadata; it does not overwrite the image. If an image recorded in a
   completed release is missing, publication fails before push. Restore its
   recorded digest or increment `patch_revision` to publish a new tag.

For the **first package only**, set `bootstrap_package=true` on a manual run with
one explicit tag. This asserts that the owner's `grimmory-fb2zip` package is new.
GHCR may return 401 both for a nonexistent package and for an inaccessible one;
normal runs fail on 401. Bootstrap additionally checks the list of visible
packages, but that list cannot prove an inaccessible package does not exist.
Verify the owner and package before making this assertion. Bootstrap defaults to
false and never ignores 403, network failures or image identity conflicts. Use it
for the initial dry run and publication if needed, then leave it disabled.

GitHub schedules run from the default branch, can be delayed, and are disabled in
public repositories after 60 days without activity. Re-enable the workflow in
Actions after inactivity and run it manually to catch up. For unattended service
across such periods, use an external scheduler to dispatch the same workflow on
the default branch. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

### Update the patch series

Keep the two functional patches in `patches/series`: server normalization and its
tests in 0001, then UI acceptance and its tests in 0002. The UI patch declares its
dependency on 0001. Develop against a freshly prepared upstream checkout, keep
changes within upload handling, and export the updated patches with their tests.
Do not edit the original Grimmory checkout or commit generated build files.

When any patch bytes or application order change, increment `patch_revision` in
`upstream.json`. For example, revision 2 produces
`v3.5.0-fb2zip.2` for the same upstream version. README-only changes do not require
a new image. Keep the verified baseline SHA; update the baseline or minimum tag
only deliberately after validating the new supported range. Reapply the series
to a new destination and run the relevant local checks below, then use the manual
dry run before publication.

A preparation failure names the patch or SHA check that failed. Inspect that
step's log and the upstream change, revise the smallest affected patch, and test
again in a fresh `.work/` directory. The script never skips a patch or resolves
conflicts automatically. A moved upstream tag, different patch-series hash, or
mismatched existing release is an error; do not delete an existing image to hide
it. Previous published versions remain available. Check registry credentials and
package access for 401/403 errors; retry transient failures after resolving them.
CI keeps build/test reports and runtime logs as workflow artifacts.

### Source and build identity

Each version gets a release in this patch repository with `patched-source.tar.gz`
and `build-manifest.json` **before** its image is pushed. The archive includes the
applied patches and upstream's tracked `LICENSE` and any `NOTICE` files. Use these
release assets when inspecting or redistributing the patched source.

The manifest records the upstream tag/SHA, patch revision, patch repository
commit, series hash, source tree/checksum, target platforms and image reference.
`image_digest` is initially null and is filled after a successful push; a release
with a null digest is not proof that the image was published. Reruns recover an
interrupted publication without replacing a conflicting archive or image.
Image labels record upstream and patch identities. An existing image must contain
both architectures and match the upstream SHA and patch-series hash.

## Local checks

```sh
python3 -m unittest discover -s tests -v
bash -n scripts/prepare-upstream.sh
```

From the prepared upstream checkout:

```sh
just api test-class org.booklore.service.upload.Fb2ZipUploadExtractorTest
just api test-class org.booklore.service.upload.FileUploadServiceTest
just api test-class org.booklore.service.metadata.extractor.Fb2MetadataExtractorTest
just ui install-ci
just check
just ui audit-ci
just image-build linux/amd64 grimmory-fb2zip:local
```

Use `linux/arm64` for a local image on an ARM host. Set `JAVA_HOME` to JDK 25;
newer JVMs cannot compile this upstream version's Java 25 preview features.

The source commit and patch series are pinned. Docker base tags and upstream
network dependencies remain mutable, so this does not promise bit-for-bit
reproducible images.

## CI and disposable runtime verification

`check.yml` runs on pull requests and pushes, and is reusable by the publishing
workflow with `upstream_tag` and `upstream_sha`. It reads Java, Node and pnpm
versions from the prepared upstream checkout, runs `just check` and the upstream
critical-only dependency audit, then builds the unmodified upstream Dockerfile
with a local path context. It has no publication permissions.

The runtime check uses a fresh MariaDB and named volumes scoped to its Compose
project. Port 6060 is bound to an ephemeral loopback port. Run locally from this
repository root after building `grimmory-fb2zip:local`:

```sh
export COMPOSE_PROJECT_NAME=fb2zip-smoke-local
export SMOKE_IMAGE=grimmory-fb2zip:local
docker compose -f tests/smoke-compose.yml up -d --wait --wait-timeout 240
address=$(docker compose -f tests/smoke-compose.yml port grimmory 6060)
python3 tests/smoke.py "http://$address"
docker compose -f tests/smoke-compose.yml logs --no-color > smoke.log
docker compose -f tests/smoke-compose.yml down --volumes --remove-orphans
```

Use a unique project name for concurrent runs. Always run the final cleanup,
including after failure; it removes only that project's containers and volumes.
The helper requires `COMPOSE_PROJECT_NAME` and refuses an already configured
instance. It waits for each scan's completion in that project's new application
logs and fails on scan errors or timeout. Its credentials are fixed
for disposable tests only. It generates its own FB2 and ZIP bytes, then checks
library upload, embedded metadata and cover, exact downloaded FB2 bytes, rename,
rescan without duplicates, BookDrop import, the first file of a physical book,
an alternative book file, and an unchanged ZIP attachment. This is a real HTTP
check against the built image and database. Browser rendering is a separate
manual check.

Patch `Depends-on` headers are enforced before creating a checkout. A regression
test also removes the backend patch from the actual UI series and verifies that
preparation fails. Backend tests accompany patch 0001; UI tests accompany 0002.

Baseline checks on `v3.5.0` (2026-09-29): backend `check` passed with 3,968 tests
and 3 skips; targeted backend regression tests passed (77 tests). Review then
strengthened the byte-limit and directory-payload tests; all 18 extractor cases
passed, with application code unchanged. The
standalone backend patch compiled before UI changes. The unmodified frontend
passed 1,727 tests before patch 0002; the full series passed 1,736 tests with 135
upstream skips, typecheck, dependency checks, ESLint, stylelint and production
build. The upstream audit policy passed with zero critical vulnerabilities;
7 high and 12 moderate upstream dependency findings remain. These component
checks do not claim a separate Docker image was built for patch 0001 alone.

A cold Docker build needs more than a 2 GiB Docker VM for this upstream version's
Angular production build. The local full build and frontend-only retry exhausted
that limit without changing the upstream Dockerfile; allocate sufficient VM
memory before retrying. Hosted CI uses its runner's default Docker resources.

The ARM64 runtime image built successfully with an 8 GiB Docker VM. Its health
endpoint reported `3.5.0-fb2zip.1`. A live Playwright check logged in, opened
`/ebook-reader/book/1`, and found the generated fixture text in the actual reader
iframe with no page errors. This verifies browser rendering in addition to the
HTTP and component checks.

The complete HTTP smoke check passed against fresh disposable volumes: library
upload, metadata, embedded cover, exact download, rename and rescan, BookDrop
finalization, physical first file, alternative format and unchanged attachment.
The final filesystem contained four `.fb2` books and only the explicitly uploaded
supplementary `.fb2.zip` attachment. Compose resources were removed after testing.

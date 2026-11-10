# Workbench validation record — 2026-11-10

Public-demo/branding follow-up: both supplied PNGs are copied unchanged and
SHA256 checked. New browser-only mode runs without a token, network API or
browser storage, including under /glamdb/. New local validation has **374 Python
tests, 26 Node unit tests and 23 Chromium scenarios**. Capacity rejection and
legacy image-free theme regressions were observed RED before fixes, then GREEN.
Desktop/mobile logo screenshots were inspected. See [PUBLIC_DEMO.md](PUBLIC_DEMO.md)
for the separate UI-only semantics and publishing path. The original baseline
record below remains evidence for its earlier scope; neither mode qualifies core
storage durability.

Scope: independent participant-four tools, Python network SDK, browser UI and a
disposable mock for absent core/HTTP dependencies. **No G1–G4 or production N
gate is declared complete.** The HTTP/HTTPS and Chromium tests below use real
transports against synthetic process-local state, not a durable C++ database.

Base: `glamourdb/glamdb` commit
`e0dfdc9ab6004f45a2dd53bfb47786cb3c0d2d45`. Fork: `nnrpi/glamdb`, branch
`feat/pink-workbench-and-role4-verification`, upstream target `main`.
Published for review as [PR #4](https://github.com/glamourdb/glamdb/pull/4);
no merge performed. HTTPS OAuth lacked workflow scope; the existing SSH
credential successfully published the complete branch including CI. Only this
repository's push URL was set to SSH; global Git configuration was unchanged.

Environment: Darwin25.3.0/arm64, Python3.13.5, AppleClang21.0.0,
CMake4.4.4, Ninja1.13.2, Node26.5.0/npm11.17.0, LLVM format/tidy18.1.8,
Playwright1.56.1/Chromium1194. Python dev versions and release SHA256 hashes
are fixed in `requirements-dev.txt`; browser tooling is locked by npm.

## Executed final checks

`bash scripts/check-workbench.sh` exited **0** after the final source changes:

- **PASS — 366 Python tests:** 142 verifier/CLI/metadata-fixture cases,
  172 SDK cases, 42 mock cases, 10 actual localhost HTTP/HTTPS integration cases.
  In-process package coverage: **89%** (2384 statements, 263 missed).
  CLI subprocesses and browser-server processes are exercised but their lines
  are not included in this in-process coverage number.
- **PASS — both literal canonical golden vectors:** exact preimages/hash,
  stable schema IDs, NULLs, duplicates, UTF-8, timestamp boundaries and int64
  above 2^53. Additional metadata fixtures cover full long/UTF-8 key bounds,
  missing/truncated footer stats and explicit expected matches. Physical
  writer/manifest comparison remains NOT RUN.
- **PASS — simplified ordering helper:** seed17, 104 deterministic plus100
  seeded cuts, with omitted-data-sync, omitted-directory-sync and early-ACK
  negative controls detected. This model assumes one atomic durable catalog
  commit, without SQLite page/journal emulation. Power durability stays
  **NOT VALIDATED** and the unified-image T-POWER gate stays open.
- **PASS — UI format check and 16 Node unit tests.**
- **PASS — 17 actual Chromium browser scenarios**, no uncaught JS errors or
  unexpected console errors. Covered desktop/mobile, keyboard/reduced motion,
  all metadata views, create/append/duplicates, precise int64, typed filters,
  hidden predicate columns/NULL, initial/zero-limit/history, Arrow download,
  runtime config edits and invalid reload, failed authentication, escaped HTML,
  lost-create exact identity recovery, conflicting name correction, frozen
  unknown retries across collections, late-dialog/read/status races, metadata
  refresh failure after commit, and disabling retry once a receipt is resolved.

The HTTP/HTTPS integration uses child processes and real TCP sockets. It covers
JSON↔Arrow↔SDK, retained historical results, old retries, pagination, real
dropped responses, temporary CA trust and wrong/untrusted CA rejection. Test
keys are disposable and the host trust store is untouched. This is network
evidence only; mock restart intentionally discards state.

The final command used `GLAMDB_BROWSER_PATH` to point at the local official
Chromium binary, as documented in [WORKBENCH.md](WORKBENCH.md). Raw output is
`artifacts/final-check.log`; coverage XML, ordering-model JSON and Playwright
report are under `artifacts/`. Generated artifacts stay ignored.

## Packaging, build and tooling

- **PASS — non-editable wheels** built for `glamdb-verify`, `glamdb-sdk`, and
  `glamdb-mock` with `pip wheel --no-deps --no-build-isolation`.
- **PASS — independently installed SDK:** a fresh venv, execution from outside
  the checkout with Python `-I`, no installed mock/verifier, and all172 SDK tests.
  The other installed-wheel suites also pass; CLI fixture checks/help work
  without source-path imports. No source-tree/private C++ dependency is needed.
- **PASS — all28 locked Python dependency wheels validated with
  `--require-hashes --only-binary=:all: --no-deps`; `pip check` reports no broken
  requirements. npm dependency versions/integrity are in `ui/package-lock.json`.
- **PASS — local/debug, release and ASan+UBSan CMake configure/build/smoke**,
  one no-op smoke test per profile. The debug executable also installs/runs.
  This tests the existing scaffold, not engine lifetimes or storage behavior.
- **PASS — LLVM18 format-check, tidy-check and quality-negative-check**:
  intentional formatting and analyzer violations are rejected.
- **PASS — pinned submodule revisions** with
  `cmake -P cmake/dependencies/VerifySubmodules.cmake`. Submodules were
  initialized at the pinned commits; no vendor code or pins were changed.
  Building/linking the complete C++ dependency stack was not required or run.
- **PASS — actionlint1.7.12** for both workflows, using the official macOS
  archive verified against its release checksum.
- **PASS — zizmor1.32.0**, pinned by the existing hash lock,
  `--no-online-audits --pedantic --format plain .github/workflows`: no findings.
  PR permissions remain read-only; the new workflow reuses existing SHA-pinned
  checkout/setup-python Actions and adds no release credentials.
- **PASS — shell syntax, relative documentation file links and git diff
  whitespace checks.** The GitHub-hosted Linux run is a separate CI result;
  local macOS success does not imply that remote run has already executed.

## Visual inspection and fixed regressions

The UI was operated in real Chromium by Playwright. Desktop overview/data and
mobile screenshots were inspected visually for typography, controls, pink
decoration, layout and clipping:

- `artifacts/e2e/overview-desktop.png`
- `artifacts/e2e/data-desktop.png`
- `artifacts/e2e/data-mobile.png`

Regression tests were observed failing before fixes for lost-create response,
cross-dialog append status, pending-operation replacement, older scan response,
duplicate-create rejection and terminal-outcome regression. SDK receipt tests
first exposed39 failures for malformed/mismatched hashes, versions and receipt
fields. Subsequent runs pass; a committed append must match its prepared hash.
Errors fetching metadata after commit no longer relabel the committed write.

During final integration, duplicate `test_cli` module names exposed pytest's
default import collisions. Root pytest configuration now uses importlib mode
and explicit test roots; integration helpers use a relative package import.
Metadata expectations live separately from canonical-byte fixture files so
the canonical checker continues to reject incompatible fixture formats.

## Environment issues and explicit limits

The default sandbox denied localhost sockets/browser bootstrap. Those tests
were rerun with allowed execution access, without skips. The CUA browser host
was unavailable; native CUA/manual-browser control is **NOT RUN**. Chromium was
instead controlled through Playwright and its screenshots inspected.

The Node26 Playwright installer stalled during extraction after downloading
Chromium. The official ZIP passed CRC checking and was extracted with system
unzip; the documented executable override ran all17 tests. No local absolute
binary path or modified vendor code is committed. Starlette's TestClient warns
about deprecated httpx support; Node26 emits upstream deprecation/color warnings.
They remain visible and do not fail the checks.

The following remain **NOT RUN / NOT VALIDATED**: native Parquet/Iceberg/SQLite
integration, physical manifests/metrics/pruning, real create/close/reopen and
recovery, C++ retained-buffer lifetime/capacity/ingestion qualification,
full SQLite-image fault modeling, native process-crash campaigns, qualified
Linux/ext4 power cuts, S3 and KV/COW/compaction. No destructive test was run on
the user's volume. The mock and simplified model cannot establish these claims.

Questions/assumptions and the next-session handoff are in
[OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) and [OWNER_HANDOFF_RU.md](OWNER_HANDOFF_RU.md).

# Test Infrastructure Governance Plan

## Purpose

This plan establishes a reproducible Python 3.14 test baseline for MCP Agent
Mail on Linux and Windows. It separates quick developer feedback from slow and
performance-oriented validation, while keeping the complete suite available as
the final release gate.

## Governing Decisions

1. **Python 3.14 only.** `requires-python`, `.python-version`, Ruff, type
   checking, local environments, CI, and release automation must agree on 3.14.
2. **uv only.** Dependencies are synchronized into the repository `.venv` with
   `uv sync --locked --dev`; tools are invoked with `uv run` or `uvx`.
3. **Locked CI environments.** CI and release jobs fail rather than silently
   changing `uv.lock`.
4. **Cross-platform tests model real paths.** Tests create filesystem-backed
   project keys with `tmp_path`. Tests for opaque remote project keys may use
   explicit POSIX or Windows absolute syntax, and production normalization must
   preserve that foreign syntax.
5. **No weakened production validation.** A Windows-only failure caused by a
   hard-coded Unix test fixture is repaired in the test. Platform skips are
   reserved for behavior that is genuinely unavailable on that platform.
6. **Type errors are failures.** The accepted baseline is zero Ruff and zero ty
   diagnostics; no repository-wide ignore baseline is introduced.

## Test Layers

| Layer | Command | Intended use |
| --- | --- | --- |
| Static quality | `make lint-check && make typecheck` | Every change and CI |
| Fast | `make test-fast` | Curated cross-platform smoke suite for the inner loop and CI |
| Integration/E2E | `make test-integration` | Changes crossing storage, DB, HTTP, or MCP boundaries |
| Slow functional | `make test-slow` | CI after the fast matrix succeeds |
| Functional | `make test-functional` | All non-performance behavior with coverage |
| Benchmarks | `make test-benchmark` | Dedicated Linux performance job |
| CI regression | `make test-ci-regression` | Dedicated threshold/report job |
| Complete | `make test-all` | Final local/release verification |

The broad marker-based layers remain explicit:

```text
slow functional = slow and not benchmark and not ci_regression
performance = benchmark or ci_regression
```

`test-fast` is intentionally curated rather than defined as every test lacking
a marker. A missing `slow` marker must not accidentally turn hundreds of
Git/SQLite/MCP integration cases into the developer smoke gate. The complete
functional and performance jobs retain coverage of the collected suite.

## Implementation Phases

### Phase 1 — Runtime and dependency alignment

- Set `requires-python`, Ruff, mypy, and `.python-version` to 3.14.
- Regenerate `uv.lock` with Python 3.14 metadata.
- Recreate/synchronize `.venv` with `uv sync --locked --dev`.
- On managed Windows networks, set `UV_SYSTEM_CERTS=true` so uv uses the system
  certificate store; TLS verification must never be disabled.

**Acceptance:** `uv run python --version` reports 3.14 and `uv lock --check`
succeeds.

### Phase 2 — Cross-platform correctness

- Replace filesystem fixtures that rely on Unix-only absolute paths with
  `tmp_path` and `pathlib.Path`.
- Preserve intentionally foreign absolute project identifiers as opaque keys.
- Close Git, SQLite, image, and stream handles deterministically so Windows can
  release temporary directories.
- Keep absolute attachment paths disabled by default; tests that exercise this
  opt-in behavior must enable it explicitly.

**Acceptance:** the fast and integration/E2E layers pass on Windows and Linux.

### Phase 3 — Static-analysis baseline

- Run `ruff check --fix --unsafe-fixes` locally, then `ruff check` as the
  non-mutating CI gate.
- Run `uvx ty check` and resolve each diagnostic through type narrowing,
  platform-safe capability detection, or correctly typed SQL expressions.
- Do not hide failures with blanket exclusions.

**Acceptance:** Ruff and ty both report zero diagnostics.

### Phase 4 — CI partitioning

- Run lint, ty, and the curated fast suite on `ubuntu-latest` and
  `windows-latest` with Python 3.14.
- Run the complete non-performance functional suite on Linux after the
  cross-platform matrix succeeds.
- Run benchmark and CI-regression tests in a dedicated Linux job and retain
  their artifacts.
- Use locked dependency synchronization in CI, nightly, and release workflows.

**Acceptance:** all required jobs are green and benchmark artifacts are
published when generated.

### Phase 5 — Final baseline verification

Run, in order:

```bash
uv sync --locked --dev
uv run ruff check --fix --unsafe-fixes
uvx ty check
make test-fast
make test-integration
make test-slow
make test-benchmark
make test-ci-regression
```

Record failures by layer. Fix deterministic product or test defects before
merging; do not redefine known failures as success. `make test-all` remains a
release/CI diagnostic entry point and is not required in every local edit loop.

## Change Safety and Rollback

- Changes are made in place; no parallel “v2” implementation is introduced.
- Existing `.env` files are never rewritten.
- Before changing a test, reproduce the failure in the narrowest relevant
  layer and rerun that layer after the fix.
- If a CI partition proves incorrect, revert only the partition expression or
  workflow step in a follow-up commit. Do not revert cross-platform correctness
  fixes or discard unrelated working-tree changes.
- Database and filesystem cleanup must remain deterministic; increasing sleeps,
  suppressing arbitrary exceptions, or globally extending timeouts is not an
  acceptable rollback strategy.

## Definition of Done

- Python 3.14 is the sole configured and tested runtime.
- `uv.lock` is current and all automation uses locked synchronization.
- Ruff and ty are clean.
- Fast tests pass on Linux and Windows.
- Slow, benchmark, and CI-regression partitions pass in their dedicated jobs.
- The complete Linux CI functional suite passes without an unresolved
  deterministic failure.
- The governance change is committed separately and pushed to `origin/main`.

.PHONY: serve-http migrate lint lint-check typecheck quality test-fast test-integration test-slow test-benchmark test-ci-regression test-functional test-all guard-install guard-uninstall claims

PY=uv run
CLI=$(PY) python -m mcp_agent_mail.cli
PYTEST=$(PY) pytest
FAST_TESTS=tests/test_app_helpers.py tests/test_guard_edges.py tests/test_guard_worktrees.py tests/test_identity.py tests/test_security_path_traversal.py tests/test_attachments_extended.py tests/test_image_processing_edge.py tests/test_toon_encoder_identification.py tests/test_share_export.py tests/test_cli_stub.py

serve-http:
	$(CLI) serve-http



migrate:
	$(CLI) migrate

lint:
	$(PY) ruff check --fix --unsafe-fixes

lint-check:
	$(PY) ruff check

typecheck:
	uvx ty check

quality: lint typecheck test-fast

test-fast:
	$(PYTEST) $(FAST_TESTS) -q --no-cov --tb=short

test-integration:
	$(PYTEST) tests/integration tests/e2e -q --no-cov --tb=short

test-slow:
	$(PYTEST) -m "slow and not benchmark and not ci_regression" -q --no-cov --tb=short

test-benchmark:
	RUN_BENCHMARKS=1 INSTRUMENTATION_ENABLED=true $(PYTEST) tests/benchmarks -m benchmark -v --no-cov --tb=short

test-ci-regression:
	$(PYTEST) tests/benchmarks/test_ci_regression.py -m ci_regression -v --no-cov --tb=short

test-functional:
	$(PYTEST) -m "not benchmark and not ci_regression" -q --tb=short

test-all:
	$(PYTEST) -q --tb=short

guard-install:
	$(CLI) guard install $(PROJECT) $(REPO)

guard-uninstall:
	$(CLI) guard uninstall $(REPO)

claims:
	$(CLI) claims list --active-only $(ACTIVE) $(PROJECT)



"""
Tests for `_looks_like_toon_rust_encoder` (mcp_agent_mail#163).

The function is the gatekeeper for the TOON encoder subprocess: it must
accept the real `toon_rust` binary (currently installed as `toon` from
`cargo install tru`) and reject lookalikes (Node.js toon CLI, coreutils
`tr`, etc.).
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Ensure the in-tree package is importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_agent_mail import app as app_module


def _make_fake_binary(tmp_path: Path, name: str, help_text: str, version_text: str = "") -> str:
    """Build a tiny shell script that prints predictable --help / --version output."""
    script = tmp_path / name
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "${{1:-}}" in
              --help) printf '%s\\n' {help_text!r} ;;
              --version) printf '%s\\n' {version_text!r} ;;
              *) printf 'unknown\\n' ; exit 1 ;;
            esac
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Reset the function's lru_cache so each scripted scenario is evaluated fresh.
    app_module._looks_like_toon_rust_encoder.cache_clear()
    return str(script)


def _mock_binary_banners(monkeypatch, *, help_text: str, version_text: str = "") -> None:
    """Provide platform-independent encoder probe responses."""

    def fake_run(argv, **_kwargs):
        output = help_text if argv[-1] == "--help" else version_text
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    app_module._looks_like_toon_rust_encoder.cache_clear()


def test_binary_named_toon_is_accepted_when_banners_identify_toon_rust(
    monkeypatch,
) -> None:
    """
    `cargo install tru` produces a binary named `toon` (the [[bin]] target's
    name in toon_rust's Cargo.toml). mcp_agent_mail#163: a basename-only
    rejection broke every local install. The post-fix function must
    identify the binary via its help-text banner regardless of basename.
    """
    _mock_binary_banners(
        monkeypatch,
        help_text="TOON reference implementation in Rust (JSON <-> TOON)\nusage: toon ...",
    )
    assert app_module._looks_like_toon_rust_encoder("toon") is True


def test_binary_named_tru_is_accepted_via_version_banner(monkeypatch) -> None:
    """A binary named `tru` that prints the toon_rust version banner is accepted."""
    _mock_binary_banners(
        monkeypatch,
        help_text="usage: tru\n",
        version_text="tru 0.2.3",
    )
    assert app_module._looks_like_toon_rust_encoder("tru") is True


def test_node_js_toon_is_still_rejected(tmp_path: Path) -> None:
    """
    The Node.js toon CLI prints neither the toon_rust help-text marker nor a
    toon_rust version banner, so it must fail identification even though
    it's named `toon`.
    """
    exe = _make_fake_binary(
        tmp_path,
        name="toon",
        help_text="usage: toon [options]\nA JS-based TOON CLI",
        version_text="toon 1.4.2 (node)",
    )
    assert app_module._looks_like_toon_rust_encoder(exe) is False


def test_coreutils_tr_is_rejected(tmp_path: Path) -> None:
    """coreutils `tr` is a real binary on $PATH; it must never be accepted."""
    if not shutil.which("tr"):
        pytest.skip("coreutils tr not available")
    app_module._looks_like_toon_rust_encoder.cache_clear()
    assert app_module._looks_like_toon_rust_encoder("/usr/bin/tr") is False


def test_nonexistent_binary_is_rejected(tmp_path: Path) -> None:
    app_module._looks_like_toon_rust_encoder.cache_clear()
    assert app_module._looks_like_toon_rust_encoder(str(tmp_path / "does-not-exist")) is False


def test_hung_binary_is_rejected_via_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A binary that hangs on --help must not wedge mcp_agent_mail. The
    identification function bounds every subprocess invocation with a
    timeout (`_TOON_IDENT_TIMEOUT_SECONDS`); on TimeoutExpired the binary
    is rejected and the encoder request falls back to JSON, which is the
    safe default.
    """
    script = tmp_path / "hang"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            sleep 30
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Drive the timeout down so the test fails fast rather than waiting the
    # production-default 5s.
    monkeypatch.setattr(app_module, "_TOON_IDENT_TIMEOUT_SECONDS", 0.5)
    app_module._looks_like_toon_rust_encoder.cache_clear()
    assert app_module._looks_like_toon_rust_encoder(str(script)) is False


def test_non_utf8_banner_does_not_crash(tmp_path: Path) -> None:
    """
    `text=True` decodes subprocess output via the active locale's encoding
    (UTF-8 on modern Linux). A banner with raw bytes that are invalid in
    UTF-8 (here: 0xFF, which never appears in a valid UTF-8 sequence) would
    raise UnicodeDecodeError on decode — which is *not* an OSError and would
    escape the function's guardrails, bubbling all the way up to a TOON
    encode request as a 500-class error.

    The function uses errors='replace' so undecodable bytes degrade to
    U+FFFD and identification still completes (returning False here because
    neither banner contains the toon_rust marker).
    """
    script = tmp_path / "garbled"
    # Emit a banner with a raw 0xFF byte that is invalid in UTF-8.
    script.write_bytes(
        b"#!/usr/bin/env bash\n"
        b'case "${1:-}" in\n'
        b"  --help) printf '\\xffgarbage --help\\n' ;;\n"
        b"  --version) printf '\\xffgarbage 0.0.0\\n' ;;\n"
        b"  *) exit 1 ;;\n"
        b"esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    app_module._looks_like_toon_rust_encoder.cache_clear()
    # Must return False (banners don't identify toon_rust), and crucially
    # must NOT raise UnicodeDecodeError.
    assert app_module._looks_like_toon_rust_encoder(str(script)) is False

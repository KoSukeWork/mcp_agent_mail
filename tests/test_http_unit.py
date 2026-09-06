from __future__ import annotations

from fastapi import FastAPI

from mcp_agent_mail import config as _config
from mcp_agent_mail.http import SecurityAndRateLimitMiddleware, _decode_jwt_header_segment
from mcp_agent_mail.localization import (
    gettext,
    reset_interface_locale,
    select_interface_locale,
    set_interface_locale,
)


def test_select_interface_locale_honors_precedence_and_browser_quality():
    assert (
        select_interface_locale(
            query_locale="zh_CN",
            cookie_locale="en",
            accept_language="en-US,en;q=0.9",
        )
        == "zh-CN"
    )
    assert (
        select_interface_locale(
            query_locale=None,
            cookie_locale="en",
            accept_language="zh-CN,zh;q=0.9",
        )
        == "en"
    )
    assert (
        select_interface_locale(
            query_locale=None,
            cookie_locale=None,
            accept_language="en;q=0.7, zh-CN;q=0.9",
        )
        == "zh-CN"
    )


def test_gettext_uses_request_scoped_interface_locale():
    token = set_interface_locale("zh-CN")
    try:
        assert gettext("Projects") == "项目"
        assert gettext("Uncatalogued message") == "Uncatalogued message"
    finally:
        reset_interface_locale(token)

    assert gettext("Projects") == "Projects"


def test_decode_jwt_header_segment_variants():
    # Well-formed header
    import base64
    import json
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode("utf-8")).rstrip(b"=")
    token = hdr.decode("ascii") + ".x.y"
    decoded = _decode_jwt_header_segment(token)
    assert decoded and decoded.get("alg") == "HS256"
    # Malformed returns None
    assert _decode_jwt_header_segment("nope") is None


def test_rate_limits_for_branches(monkeypatch):
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = FastAPI()
    mw = SecurityAndRateLimitMiddleware(app, settings)
    assert mw._rate_limits_for("tools")[0] >= 1
    assert mw._rate_limits_for("resources")[0] >= 1
    assert mw._rate_limits_for("other")[0] >= 1



from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from jinja2 import Environment, StrictUndefined, nodes

from mcp_agent_mail import config as _config
from mcp_agent_mail.http import SecurityAndRateLimitMiddleware, _decode_jwt_header_segment
from mcp_agent_mail.localization import (
    get_interface_locale,
    gettext,
    reset_interface_locale,
    select_interface_locale,
    set_interface_locale,
)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_tutorial_translates_all_steps_and_dynamic_controls(locale):
    """Render the real template, including hidden steps and Alpine expressions."""
    source = (
        Path(__file__).parents[1] / "src/mcp_agent_mail/templates/base.html"
    ).read_text(encoding="utf-8")
    env = Environment(autoescape=True, undefined=StrictUndefined)
    token = set_interface_locale(locale)
    try:
        rendered = env.from_string(source).render(_=gettext, current_locale=get_interface_locale)
        tutorial = rendered.split("<!-- Interactive Tutorial / Onboarding -->", 1)[1].split(
            "<!-- Keyboard", 1
        )[0]
        # Every translated message used by the tutorial must have a catalog entry;
        # checking only the page title or language cookie missed this regression.
        template_section = source.split("<!-- Interactive Tutorial / Onboarding -->", 1)[1].split(
            "<!-- Keyboard", 1
        )[0]
        messages = [
            call.args[0].value
            for call in env.parse(template_section).find_all(nodes.Call)
            if isinstance(call.node, nodes.Name)
            and call.node.name == "_"
            and call.args
            and isinstance(call.args[0], nodes.Const)
        ]
        assert len(messages) > 100
        if locale == "zh-CN":
            assert all(gettext(message) != message for message in messages)
            for label in (
                "什么是项目\uFF1F", "发现关联项目", "什么是智能体\uFF1F", "智能体如何沟通",
                "何时使用人工监督者功能", "避免编辑冲突", "内置实用功能", "教程已完成",
            ):
                assert label in tutorial
        else:
            assert all(gettext(message) == message for message in messages)
            assert "Discover Related Projects" in tutorial
            assert "Tutorial Complete" in tutorial

        class TutorialParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text: list[str] = []
                self.attributes: list[dict[str, str | None]] = []

            def handle_data(self, data: str):
                if data.strip():
                    self.text.append(data.strip())

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
                self.attributes.append(dict(attrs))

        parser = TutorialParser()
        parser.feed(tutorial)
        if locale == "zh-CN":
            # Product/model names, agent identities, paths, and keycaps are not
            # translated. All other English-only text nodes are omissions.
            preserved = {"Claude", "GPT-4", "Gemini", "BlueLake", "GreenCastle",
                         "src/auth/*.ts", "db/migrations/*", "⌘K"}
            untranslated = [text for text in parser.text
                            if re.search(r"[A-Za-z]", text)
                            and not re.search(r"[\u4e00-\u9fff]", text)
                            and text not in preserved]
            assert not untranslated

        next_expression = next(
            attrs["x-text"] for attrs in parser.attributes
            if (attrs.get("x-text") or "").startswith("currentStep < steps.length")
        )
        assert next_expression is not None
        assert json.dumps(gettext("Next step")) in next_expression
        assert json.dumps(gettext("Get Started")) in next_expression
        assert any(attrs.get("data-tippy-content") == gettext("Skip tutorial")
                   for attrs in parser.attributes)
        assert any(attrs.get("x-text") == "steps.length" for attrs in parser.attributes)
        for message in ("Welcome to Agent Mail", "What are Projects?", "What are Agents?",
                        "How Agents Communicate", "Human Overseer", "File Reservations",
                        "Powerful Features", "You're All Set!", "Interactive Tutorial",
                        "Learn how Agent Mail works",
                        "Tutorial completed! Press ? for keyboard shortcuts anytime.",
                        "✨ Nice! You used the command palette!",
                        "⌨️ Great! You checked the shortcuts!",
                        "📬 You found the inbox!", "🔍 You tried searching!"):
            encoded = env.from_string("{{ _(message) | tojson }}").render(message=message, _=gettext)
            assert encoded in rendered
            if locale == "zh-CN":
                assert gettext(message) != message
    finally:
        reset_interface_locale(token)


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



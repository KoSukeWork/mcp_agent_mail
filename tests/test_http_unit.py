from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from jinja2 import Environment, FileSystemLoader, StrictUndefined, nodes

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
def test_project_header_preserves_long_path_and_layout_regions(locale):
    path = "C:\\Users\\admin\\AppData\\Local\\Temp\\" + "long-project-path\\" * 12 + "team's & work"
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/mcp_agent_mail/templates"),
        autoescape=True, undefined=StrictUndefined,
    )
    token = set_interface_locale(locale)
    try:
        html = env.get_template("mail_project.html").render(
            _=gettext, current_locale=get_interface_locale,
            project={"id": 1, "slug": "long-project", "human_key": path,
                     "created_at": "2026-09-08", "archived_at": None},
            agents=[], results=[], q="", scope="", order="", boost="",
        )
    finally:
        reset_interface_locale(token)

    class HeaderParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.attributes: list[dict[str, str | None]] = []

        def handle_starttag(self, tag, attrs):
            self.attributes.append(dict(attrs))

    parser = HeaderParser()
    parser.feed(html)
    for region in ("app-header-layout", "app-header-leading", "app-header-brand",
                   "app-header-breadcrumbs", "app-header-actions"):
        assert sum(region in (attrs.get("class") or "").split() for attrs in parser.attributes) == 1
    assert any(attrs.get("title") == path for attrs in parser.attributes)
    assert 'href="/mail/long-project/overseer/compose"' in html
    assert ".app-header-actions button { flex-shrink: 0; white-space: nowrap; }" in html
    assert "text-overflow: ellipsis;" in html
    assert ".app-header-actions { margin-left: auto; overflow-x: auto; }" in html


def test_template_catalog_covers_translated_strings():
    template_dir = Path(__file__).parents[1] / "src/mcp_agent_mail/templates"
    env = Environment()
    token = set_interface_locale("zh-CN")
    try:
        missing = set()
        for path in template_dir.glob("*.html"):
            tree = env.parse(path.read_text(encoding="utf-8"))
            for call in tree.find_all(nodes.Call):
                if (isinstance(call.node, nodes.Name) and call.node.name == "_"
                        and call.args and isinstance(call.args[0], nodes.Const)):
                    message = call.args[0].value
                    # Technical column abbreviations are intentionally language-neutral.
                    if message != "ID" and gettext(message) == message:
                        missing.add(message)
        assert not missing, "Missing Chinese translations:\n" + "\n".join(sorted(missing))
    finally:
        reset_interface_locale(token)


class _UiScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[tuple[str, str]] = []
        self.expressions: list[str] = []
        self.text_expressions: list[str] = []
        self._script: list[str] | None = None
        self._script_type = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attributes = dict(attrs)
        if tag == "script":
            self._script = []
            self._script_type = attributes.get("type") or ""
        for key, value in attrs:
            if value and (key == "x-text" or key.startswith(":")):
                self.expressions.append(value)
                if key == "x-text":
                    self.text_expressions.append(value)

    def handle_data(self, data: str):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str):
        if tag == "script" and self._script is not None:
            source = "".join(self._script)
            if source.strip():
                self.scripts.append((self._script_type, source))
            self._script = None


@pytest.fixture(params=["zh-CN", "en"])
def rendered_unified_inbox(request):
    locale = request.param
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/mcp_agent_mail/templates"),
        autoescape=True, undefined=StrictUndefined,
    )
    token = set_interface_locale(locale)
    try:
        html = env.get_template("mail_unified_inbox.html").render(
            _=gettext, current_locale=get_interface_locale, projects=[], messages=[],
        )
        yield locale, html
    finally:
        reset_interface_locale(token)


def test_unified_inbox_dynamic_controls_render(rendered_unified_inbox):
    locale, html = rendered_unified_inbox
    parser = _UiScriptParser()
    parser.feed(html)
    fullscreen = next(expr for expr in parser.text_expressions if expr.startswith("isFullscreen ?"))
    assert json.dumps(gettext("Fullscreen")) in fullscreen
    assert json.dumps(gettext("Exit fullscreen")) in fullscreen
    for source in ("Inbox refreshed", "Updated just now", "Updated {count}m ago"):
        assert json.dumps(gettext(source)) in html
    assert len(parser.scripts) >= 5
    assert len(parser.expressions) > 10
    if locale == "zh-CN":
        assert '"Fullscreen"' not in fullscreen
        assert "window.showToast('Inbox refreshed'" not in html
        assert "共 0 个项目" in html


def test_unified_inbox_dynamic_controls_execute(rendered_unified_inbox):
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript runtime checks require Node.js")
    locale, html = rendered_unified_inbox
    parser = _UiScriptParser()
    parser.feed(html)
    expected = [gettext(message) for message in (
        "Refreshing…", "Awaiting first refresh", "Updated just now",
        "Updated {count}s ago", "Updated {count}m ago", "Updated {count}h ago",
        "Updated {count}d ago", "Inbox refreshed", "Refresh failed. Retrying soon.",
        "Failed to refresh inbox", "Fullscreen", "Exit fullscreen",
    )]
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", r"""
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {scripts, expressions, textExpressions, expected} = JSON.parse(readFileSync(0, 'utf8'));
for (const [type, source] of scripts) {
  if (type === 'module') new vm.SourceTextModule(source);
  else if (!type || type === 'text/javascript') new vm.Script(source);
}
for (const expression of expressions) new Function(`return (${expression});`);
const inbox = scripts.filter(([, source]) => source.includes('function unifiedInboxManager()'));
assert.equal(inbox.length, 1);
const toasts = [];
const context = vm.createContext({
  window: {showToast: text => toasts.push(text)},
  console: {error() {}},
  fetch: async () => ({ok: true, json: async () => ({messages: []})}),
});
vm.runInContext(inbox[0][1], context);
const data = vm.runInContext('unifiedInboxManager()', context);
data.isRefreshing = true;
assert.equal(data.lastRefreshLabel, expected[0]);
data.isRefreshing = false;
data.lastRefreshTime = null;
assert.equal(data.lastRefreshLabel, expected[1]);
const ages = [0, 15, 120, 7200, 172800];
const counts = [0, 15, 2, 2, 2];
ages.forEach((seconds, i) => {
  data.lastRefreshTime = new Date(Date.now() - seconds * 1000);
  assert.equal(data.lastRefreshLabel, expected[i + 2].replace('{count}', counts[i]));
});
const fullscreen = textExpressions.find(expr => expr.startsWith('isFullscreen ?'));
for (const [flag, label] of [[false, expected[10]], [true, expected[11]]]) {
  context.isFullscreen = flag;
  assert.equal(vm.runInContext(fullscreen, context), label);
}
data.filterMessages = () => { data.filteredMessages = [...data.allMessages]; };
data.scheduleAutoRefresh = () => {};
(async () => {
  await data.fetchLatestMessages();
  assert.equal(toasts.pop(), expected[7]);
  assert.equal(data.refreshError, null);
  context.fetch = async () => ({ok: false, status: 503});
  await data.fetchLatestMessages();
  assert.equal(toasts.pop(), expected[9]);
  assert.equal(data.refreshError, expected[8]);
  assert.equal(data.isRefreshing, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
        input=json.dumps({
            "scripts": parser.scripts, "expressions": parser.expressions,
            "textExpressions": parser.text_expressions, "expected": expected,
        }),
        text=True, encoding="utf-8", capture_output=True, timeout=15,
    )
    assert result.returncode == 0, f"{locale}: {result.stderr}"


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



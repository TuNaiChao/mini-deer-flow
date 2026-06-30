"""test_community.py — M21 community 联网 provider 框架的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M21）：
- ``_common``：结果归一化 / 4KB 截断 / 通用参数强转 / 配置访问 / ``post_json`` async httpx 封装
- ``utils/readability``：纯 Python 兜底提取（readabilipy 未安装）/ Article.to_markdown
- ``AppConfig.get_tool_config``：按 name 查 / 未找到 None / 非 dict 跳过
- ddg_search：CJK region 推断 / backend 归一 / 缺包→[] / 注入 fake ddgs 发现 + 归一 / config 覆盖
- tavily：缺包→安装提示 / 注入 fake tavily search+fetch（4KB 截断 / failed_results→Error）/ config
- jina_ai：JinaClient + post_json mock（200/markdown/4KB）/ Error 前缀透传 / JINA_API_KEY 头 / 参数强转
- image_search：缺包→无图 / 注入 fake ddgs.images / config max_results
- brave：无 key→Error / mock httpx.Client 发现 + 归一 / max_results clamp / HTTP 错
- serper：无 key→Error / mock httpx.Client / 空 organic→No results
- searxng：mock httpx.AsyncClient / 归一 / 请求错→Error JSON
- browserless：mock httpx.AsyncClient / readability markdown + 4KB / Error 透传 / 非 200
- 软加载占位：firecrawl/exa 缺 SDK→安装提示；infoquest 无 key→Error

hermetic：``ddgs`` / ``tavily`` / ``firecrawl`` / ``exa_py`` 均**未安装**——用 ``sys.modules``
注入 fake 模块；``httpx`` / ``requests`` 已安装——用 monkeypatch 替 ``httpx.Client`` /
``httpx.AsyncClient``。零网络零子进程。config 经 monkeypatch ``_common.get_app_config`` 注入。
"""

from __future__ import annotations

import json
import sys
import types

import httpx
import pytest

from deerflow.community import _common
from deerflow.community._common import (
    MAX_FETCH_CHARS,
    coerce_bool,
    coerce_int,
    coerce_proxy,
    coerce_timeout,
    get_tool_extras,
    normalize_search_result,
    post_json,
    truncate_content,
)
from deerflow.config.app_config import AppConfig
from deerflow.utils.readability import Article, ReadabilityExtractor

# ===========================================================================
# config 注入 helper
# ===========================================================================


class _FakeConfig:
    """假 AppConfig：``get_tool_config(name)`` 返回预设 dict 或 None。"""

    def __init__(self, tools_map: dict[str, dict] | None = None) -> None:
        self._tools = tools_map or {}

    def get_tool_config(self, name: str):
        return self._tools.get(name)


def _patch_config(monkeypatch: pytest.MonkeyPatch, tools_map: dict[str, dict] | None = None) -> _FakeConfig:
    """把 ``_common.get_app_config`` 替成返回 ``_FakeConfig``。影响所有 provider 的 ``get_tool_extras``。"""
    cfg = _FakeConfig(tools_map)
    monkeypatch.setattr(_common, "get_app_config", lambda: cfg)
    return cfg


# ===========================================================================
# fake ddgs / tavily 注入 helper
# ===========================================================================


def _install_fake_ddgs(monkeypatch: pytest.MonkeyPatch, text_results=None, image_results=None) -> None:
    """注入 fake ``ddgs`` 模块进 sys.modules。

    Args:
        text_results: ``DDGS().text(...)`` 返回值（list 或抛异常的可调用）。
        image_results: ``DDGS().images(...)`` 返回值。
    """
    text_results = text_results if text_results is not None else []
    image_results = image_results if image_results is not None else []

    class _FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query, **kwargs):
            return text_results

        def images(self, query, **kwargs):
            return image_results

    fake_mod = types.SimpleNamespace(DDGS=_FakeDDGS)
    monkeypatch.setitem(sys.modules, "ddgs", fake_mod)


def _install_fake_tavily(monkeypatch: pytest.MonkeyPatch, *, search_payload=None, extract_payload=None) -> None:
    """注入 fake ``tavily`` 模块。``TavilyClient(api_key=)`` 的 ``.search`` / ``.extract`` 返回预设。"""

    class _FakeTavilyClient:
        def __init__(self, *args, **kwargs):
            self.api_key = kwargs.get("api_key")

        def search(self, query, max_results=5):
            return search_payload or {"results": []}

        def extract(self, urls):
            return extract_payload or {"results": [], "failed_results": []}

    fake_mod = types.SimpleNamespace(TavilyClient=_FakeTavilyClient)
    monkeypatch.setitem(sys.modules, "tavily", fake_mod)


# ===========================================================================
# fake httpx 响应 / 客户端 helper
# ===========================================================================


class _FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json if self._json is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=httpx.Request("GET", "http://x"), response=self)


class _FakeSyncClient:
    """假同步 httpx.Client：``get``/``post`` 返回预设 ``_FakeResponse``。"""

    def __init__(self, get_response=None, post_response=None):
        self._get = get_response
        self._post = post_response
        self.last_get_kwargs = None
        self.last_post_kwargs = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.last_get_kwargs = kwargs
        return self._get or _FakeResponse()

    def post(self, url, **kwargs):
        self.last_post_kwargs = kwargs
        return self._post or _FakeResponse()


class _FakeAsyncClient:
    """假异步 httpx.AsyncClient。"""

    def __init__(self, get_response=None, post_response=None):
        self._get = get_response
        self._post = post_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return self._get or _FakeResponse()

    async def post(self, url, **kwargs):
        return self._post or _FakeResponse()


# ===========================================================================
# Test_common
# ===========================================================================


class TestCommon:
    def test_max_fetch_chars_is_4096(self):
        assert MAX_FETCH_CHARS == 4096

    def test_normalize_default_content_key(self):
        assert normalize_search_result("T", "U", "S") == {"title": "T", "url": "U", "content": "S"}

    def test_normalize_snippet_key(self):
        assert normalize_search_result("T", "U", "S", content_key="snippet") == {
            "title": "T",
            "url": "U",
            "snippet": "S",
        }

    def test_normalize_none_to_empty(self):
        out = normalize_search_result(None, None, None)
        assert out == {"title": "", "url": "", "content": ""}

    def test_truncate_default_limit(self):
        assert len(truncate_content("x" * 9999)) == MAX_FETCH_CHARS

    def test_truncate_custom_limit(self):
        assert truncate_content("abcdef", 3) == "abc"

    def test_truncate_none_safe(self):
        assert truncate_content(None) == ""
        assert truncate_content("") == ""

    @pytest.mark.parametrize(
        "value,default,expected",
        [
            ("yes", False, True),
            ("TRUE", False, True),
            ("1", False, True),
            ("on", False, True),
            ("no", True, False),
            ("0", True, False),
            ("off", True, False),
            (True, False, True),
            (False, True, False),
            (None, True, True),
            ("maybe", True, True),  # 非法 → default
            (5, False, False),  # 非 bool/str → default
        ],
    )
    def test_coerce_bool(self, value, default, expected):
        assert coerce_bool(value, default) is expected

    @pytest.mark.parametrize(
        "value,default,expected",
        [
            ("42", 0, 42),
            (42, 0, 42),
            (3.9, 0, 3),
            (True, 5, 5),  # bool 不当 int
            (None, 7, 7),
            ("abc", 9, 9),
            ("  100  ", 0, 100),
        ],
    )
    def test_coerce_int(self, value, default, expected):
        assert coerce_int(value, default) == expected

    def test_coerce_timeout_alias(self):
        assert coerce_timeout("10", 5) == 10
        assert coerce_timeout(None, 5) == 5

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("http://proxy:8080", "http://proxy:8080"),
            ("  ", None),
            ("", None),
            (None, None),
            (12345, None),
        ],
    )
    def test_coerce_proxy(self, value, expected):
        assert coerce_proxy(value) == expected

    def test_get_tool_extras_no_config_returns_empty(self, monkeypatch):
        _patch_config(monkeypatch, None)
        assert get_tool_extras("web_search") == {}

    def test_get_tool_extras_found(self, monkeypatch):
        _patch_config(monkeypatch, {"web_search": {"name": "web_search", "api_key": "k", "max_results": 7}})
        extras = get_tool_extras("web_search")
        assert extras["api_key"] == "k"
        assert extras["max_results"] == 7
        # 默认值
        assert extras.get("region", "wt-wt") == "wt-wt"


class TestPostJson:
    @pytest.mark.asyncio
    async def test_returns_text_on_200(self, monkeypatch):
        client = _FakeAsyncClient(post_response=_FakeResponse(status_code=200, text="hello world"))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        result = await post_json("https://x", headers={}, json_body={"u": "1"})
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_error_on_non_200(self, monkeypatch):
        client = _FakeAsyncClient(post_response=_FakeResponse(status_code=500, text="boom"))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        result = await post_json("https://x", headers={}, json_body={})
        assert result.startswith("Error:")
        assert "500" in result

    @pytest.mark.asyncio
    async def test_error_on_empty_response(self, monkeypatch):
        client = _FakeAsyncClient(post_response=_FakeResponse(status_code=200, text="   "))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        result = await post_json("https://x", headers={}, json_body={})
        assert result.startswith("Error:")
        assert "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_error_on_request_exception(self, monkeypatch):
        class _BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *a, **kw):
                raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _BoomClient())
        result = await post_json("https://x", headers={}, json_body={})
        assert result.startswith("Error:")


# ===========================================================================
# Test readability
# ===========================================================================


class TestReadability:
    def test_article_to_markdown_with_title(self):
        art = Article(title="Hi", html_content="<p>x</p>")
        # markdownify 可用（未安装）→ 走兜底：html_content 原样（非 <head> 内）
        md = art.to_markdown()
        assert md.startswith("# Hi\n\n")

    def test_article_empty_content_placeholder(self):
        art = Article(title="T", html_content="")
        assert "No content available" in art.to_markdown()

    def test_extractor_pure_python_fallback_strips_script(self):
        html = "<html><head><title>Page</title></head><body><p>Hello <b>world</b></p><script>evil()</script></body></html>"
        art = ReadabilityExtractor().extract_article(html)
        assert art.title == "Page"
        md = art.to_markdown()
        assert "Hello" in md
        assert "world" in md
        assert "evil" not in md  # script 被剥
        assert "<script>" not in md

    def test_extractor_strips_nav_footer(self):
        html = "<html><body><nav>menu1 menu2</nav><main><p>real content</p></main><footer>(c) 2026</footer></body></html>"
        art = ReadabilityExtractor().extract_article(html)
        md = art.to_markdown()
        assert "real content" in md
        assert "menu1" not in md
        assert "(c) 2026" not in md

    def test_extractor_default_title_when_missing(self):
        art = ReadabilityExtractor().extract_article("<html><body><p>no title here</p></body></html>")
        assert art.title == "Untitled"

    def test_article_to_message_splits_text_and_images(self):
        """#3575/M21：``to_message`` 把 markdown 切成 text + image_url 交替块。"""
        art = Article(title="T", html_content="<p>ignored</p>", url="http://x.com/page")
        # 直接构造 to_markdown 的输出形态（绕过 markdownify 缺包）：文本 + 图片 + 文本。
        art.html_content = "before ![alt](img/a.png) after"
        # to_markdown 缺 markdownify 时原样用 html_content；to_message 在其上切图片。
        msg = Article.to_message(art)
        types = [b["type"] for b in msg]
        assert "image_url" in types
        # 图片 URL 用文章 url 拼成绝对
        img_block = next(b for b in msg if b["type"] == "image_url")
        assert img_block["image_url"]["url"] == "http://x.com/img/a.png"
        # 文本块都在
        text_blocks = [b["text"] for b in msg if b["type"] == "text"]
        assert any("before" in t for t in text_blocks)
        assert any("after" in t for t in text_blocks)

    def test_article_to_message_empty_content_fallback(self, monkeypatch):
        """to_markdown 返空时，to_message 回退 No content available。"""
        art = Article(title="T", html_content="<p>x</p>")
        monkeypatch.setattr(art, "to_markdown", lambda including_title=True: "")
        assert art.to_message() == [{"type": "text", "text": "No content available"}]

    def test_article_url_attribute(self):
        art = Article(title="T", html_content="<p>x</p>", url="http://example.com/")
        assert art.url == "http://example.com/"
        # 默认空
        assert Article(title="T", html_content="<p>x</p>").url == ""

    def test_extractor_subprocess_failure_retries_use_readability_false(self, monkeypatch):
        """readabilipy 装了但 Readability.js 子进程失败 → 回退 use_readability=False（再失败落 regex）。"""
        calls = {"n": 0}

        def _flaky_simple_json(html, use_readability=True):
            calls["n"] += 1
            if use_readability:
                # 模拟 Readability.js 子进程缺失（FileNotFoundError 是 builtin，非 subprocess 的）
                raise FileNotFoundError("node not found")
            # use_readability=False 的纯 Python 模式成功
            return {"title": "Pure", "content": "<p>pure-python extract</p>"}

        import sys
        import types as _types

        monkeypatch.setitem(sys.modules, "readabilipy", _types.SimpleNamespace(simple_json_from_html_string=_flaky_simple_json))
        art = ReadabilityExtractor().extract_article("<html><body><p>x</p></body></html>")
        assert calls["n"] == 2  # 先试 use_readability=True，炸后回退 use_readability=False
        assert art.title == "Pure"
        assert "pure-python extract" in art.html_content


# ===========================================================================
# Test AppConfig.get_tool_config
# ===========================================================================


class TestGetToolConfig:
    def test_found_returns_dict_with_extras(self):
        cfg = AppConfig(tools=[{"name": "web_search", "group": "g", "use": "p:t", "api_key": "k"}])
        result = cfg.get_tool_config("web_search")
        assert result is not None
        assert result["api_key"] == "k"

    def test_not_found_returns_none(self):
        cfg = AppConfig(tools=[{"name": "web_search"}])
        assert cfg.get_tool_config("web_fetch") is None

    def test_empty_tools_returns_none(self):
        assert AppConfig().get_tool_config("web_search") is None

    def test_multiple_tools_returns_correct_one(self):
        cfg = AppConfig(
            tools=[
                {"name": "bash", "group": "g", "use": "p:bash"},
                {"name": "web_search", "group": "g", "use": "p:t", "api_key": "k"},
                {"name": "web_fetch", "group": "g", "use": "p:f"},
            ]
        )
        result = cfg.get_tool_config("web_search")
        assert result is not None and result["api_key"] == "k"
        assert cfg.get_tool_config("web_fetch") is not None
        assert cfg.get_tool_config("bash") is not None
        assert cfg.get_tool_config("nonexistent") is None


# ===========================================================================
# Test ddg_search
# ===========================================================================


class TestDdgRegion:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("こんにちは", "jp-ja"),  # 平假名
            ("안녕하세요", "kr-ko"),  # 韩文
            ("你好世界", "cn-zh"),  # CJK 统一表意
            ("привет", "ru-ru"),  # 西里尔
            ("γειά", "gr-el"),  # 希腊
            ("שלום", "il-he"),  # 希伯来
            ("مرحبا", "xa-ar"),  # 阿拉伯
            ("hello world", "us-en"),  # 拉丁 → 默认
        ],
    )
    def test_infer_wikipedia_region(self, query, expected):
        from deerflow.community.ddg_search.tools import _infer_wikipedia_region

        assert _infer_wikipedia_region(query) == expected

    def test_resolve_region_worldwide_infers(self):
        from deerflow.community.ddg_search.tools import _resolve_ddgs_region

        # 全球 region + wikipedia backend → 按 CJK 推断
        assert _resolve_ddgs_region("你好", "wt-wt", "auto") == "cn-zh"

    def test_resolve_region_non_wikipedia_backend_passthrough(self):
        from deerflow.community.ddg_search.tools import _resolve_ddgs_region

        # 非 wikipedia backend → region 原样（lower）
        assert _resolve_ddgs_region("你好", "US-EN", "duckduckgo") == "us-en"

    def test_resolve_region_no_dash_defaults(self):
        from deerflow.community.ddg_search.tools import _resolve_ddgs_region

        # wikipedia backend + 无 dash region → 默认 wikipedia region
        assert _resolve_ddgs_region("x", "fr", "wikipedia") == "us-en"

    def test_resolve_region_language_alias(self):
        from deerflow.community.ddg_search.tools import _resolve_ddgs_region

        # jp 别名 → ja
        assert _resolve_ddgs_region("x", "us-jp", "wikipedia") == "us-ja"


class TestDdgSearch:
    def test_normalize_backend_variants(self):
        from deerflow.community.ddg_search.tools import _normalize_backend

        assert _normalize_backend(None) == "auto"
        assert _normalize_backend(["duckduckgo", " brave "]) == "duckduckgo,brave"
        assert _normalize_backend("") == "auto"
        assert _normalize_backend("auto") == "auto"

    def test_search_text_missing_ddgs_returns_empty(self):
        from deerflow.community.ddg_search.tools import _search_text

        # ddgs 未安装（conftest 不注入）→ []
        # 注意：若其它测试注入了 sys.modules['ddgs']，pytest 会清理 monkeypatch
        results = _search_text("anything")
        assert results == []

    def test_search_text_with_fake_ddgs(self, monkeypatch):
        from deerflow.community.ddg_search import tools as ddg_mod

        _install_fake_ddgs(
            monkeypatch,
            text_results=[{"title": "T1", "href": "http://a", "body": "B1"}, {"title": "T2", "link": "http://b", "snippet": "B2"}],
        )
        results = ddg_mod._search_text("query", max_results=2)
        assert len(results) == 2
        assert results[0]["title"] == "T1"

    def test_web_search_tool_normalizes_results(self, monkeypatch):
        from deerflow.community.ddg_search import tools as ddg_mod

        _install_fake_ddgs(
            monkeypatch,
            text_results=[{"title": "Title", "href": "http://x", "body": "Snippet text"}],
        )
        out = json.loads(ddg_mod.web_search_tool.invoke({"query": "q"}))
        assert out["query"] == "q"
        assert out["total_results"] == 1
        assert out["results"][0] == {"title": "Title", "url": "http://x", "content": "Snippet text"}

    def test_web_search_tool_no_results(self, monkeypatch):
        from deerflow.community.ddg_search import tools as ddg_mod

        _install_fake_ddgs(monkeypatch, text_results=[])
        out = json.loads(ddg_mod.web_search_tool.invoke({"query": "q"}))
        assert out["error"] == "No results found"

    def test_web_search_tool_config_overrides(self, monkeypatch):
        from deerflow.community.ddg_search import tools as ddg_mod

        _patch_config(monkeypatch, {"web_search": {"max_results": 3, "region": "us-en", "backend": "duckduckgo"}})

        captured = {}

        class _DDGS:
            def __init__(self, *a, **kw):
                pass

            def text(self, query, **kw):
                captured.update(kw)
                captured["query"] = query
                return []

        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=_DDGS))
        ddg_mod.web_search_tool.invoke({"query": "q"})
        assert captured["max_results"] == 3
        assert captured["region"] == "us-en"
        assert captured["backend"] == "duckduckgo"


# ===========================================================================
# Test tavily
# ===========================================================================


class TestTavily:
    def test_missing_sdk_returns_install_hint(self):
        from deerflow.community.tavily import tools as tav_mod

        # tavily 未安装
        result = tav_mod.web_search_tool.invoke({"query": "q"})
        assert "tavily-python" in result
        assert result.startswith("Error:")

    def test_search_normalizes_snippet_key(self, monkeypatch):
        from deerflow.community.tavily import tools as tav_mod

        _install_fake_tavily(
            monkeypatch,
            search_payload={"results": [{"title": "T", "url": "http://u", "content": "C"}]},
        )
        out = json.loads(tav_mod.web_search_tool.invoke({"query": "q"}))
        assert out == [{"title": "T", "url": "http://u", "snippet": "C"}]

    def test_search_config_max_results(self, monkeypatch):
        from deerflow.community.tavily import tools as tav_mod

        _patch_config(monkeypatch, {"web_search": {"max_results": 9}})
        captured = {}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def search(self, query, max_results=5):
                captured["max_results"] = max_results
                return {"results": []}

        monkeypatch.setitem(sys.modules, "tavily", types.SimpleNamespace(TavilyClient=_Client))
        tav_mod.web_search_tool.invoke({"query": "q"})
        assert captured["max_results"] == 9

    def test_fetch_truncates_and_titles(self, monkeypatch):
        from deerflow.community.tavily import tools as tav_mod

        _install_fake_tavily(
            monkeypatch,
            extract_payload={"results": [{"title": "P", "raw_content": "X" * 9999}], "failed_results": []},
        )
        result = tav_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert result.startswith("# P\n\n")
        # 4KB 截断
        assert len(result) < 9999

    def test_fetch_failed_results_returns_error(self, monkeypatch):
        from deerflow.community.tavily import tools as tav_mod

        _install_fake_tavily(
            monkeypatch,
            extract_payload={"results": [], "failed_results": [{"error": "blocked"}]},
        )
        result = tav_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert result.startswith("Error:")
        assert "blocked" in result

    def test_fetch_no_results(self, monkeypatch):
        from deerflow.community.tavily import tools as tav_mod

        _install_fake_tavily(monkeypatch, extract_payload={"results": [], "failed_results": []})
        assert tav_mod.web_fetch_tool.invoke({"url": "http://u"}) == "Error: No results found"


# ===========================================================================
# Test jina_ai
# ===========================================================================


class TestJina:
    @pytest.mark.asyncio
    async def test_fetch_returns_markdown_truncated(self, monkeypatch):
        from deerflow.community.jina_ai import tools as jina_mod

        long_html = "<html><head><title>T</title></head><body><p>" + "A" * 9999 + "</p></body></html>"
        monkeypatch.setattr(
            jina_mod.JinaClient,
            "crawl",
            lambda self, url, **kw: _async_return(long_html),
        )
        result = await jina_mod.web_fetch_tool.ainvoke({"url": "http://u"})
        assert result.startswith("# T")
        assert len(result) < 9999  # 4KB 截断

    @pytest.mark.asyncio
    async def test_fetch_error_passthrough(self, monkeypatch):
        from deerflow.community.jina_ai import tools as jina_mod

        monkeypatch.setattr(jina_mod.JinaClient, "crawl", lambda self, url, **kw: _async_return("Error: Jina down"))
        result = await jina_mod.web_fetch_tool.ainvoke({"url": "http://u"})
        assert result == "Error: Jina down"

    @pytest.mark.asyncio
    async def test_crawl_adds_authorization_when_key_set(self, monkeypatch):
        from deerflow.community.jina_ai.jina_client import JinaClient

        monkeypatch.setenv("JINA_API_KEY", "secret-key")
        captured = {}

        async def fake_post_json(url, *, headers, json_body, **kw):
            captured["headers"] = headers
            captured["url"] = url
            captured["json_body"] = json_body
            return "ok"

        monkeypatch.setattr("deerflow.community.jina_ai.jina_client.post_json", fake_post_json)
        client = JinaClient()
        result = await client.crawl("http://u")
        assert result == "ok"
        assert captured["headers"]["Authorization"] == "Bearer secret-key"
        assert captured["json_body"] == {"url": "http://u"}

    @pytest.mark.asyncio
    async def test_crawl_config_coercion(self, monkeypatch):
        from deerflow.community.jina_ai import tools as jina_mod

        _patch_config(monkeypatch, {"web_fetch": {"timeout": "15", "proxy": "  ", "trust_env": "no"}})
        captured = {}

        async def fake_post_json(url, *, headers, json_body, timeout, proxy, trust_env, **kw):
            captured["timeout"] = timeout
            captured["proxy"] = proxy
            captured["trust_env"] = trust_env
            return "<html><body><p>x</p></body></html>"

        monkeypatch.setattr("deerflow.community.jina_ai.jina_client.post_json", fake_post_json)
        await jina_mod.web_fetch_tool.ainvoke({"url": "http://u"})
        assert captured["timeout"] == 15
        assert captured["proxy"] is None  # 空白 → None
        assert captured["trust_env"] is False  # "no" → False


async def _async_return(value):
    return value


# ===========================================================================
# Test image_search
# ===========================================================================


class TestImageSearch:
    def test_missing_ddgs_no_images(self):
        from deerflow.community.image_search import tools as img_mod

        out = json.loads(img_mod.image_search_tool.invoke({"query": "cat"}))
        assert out["error"] == "No images found"

    def test_returns_thumbnail_urls(self, monkeypatch):
        from deerflow.community.image_search import tools as img_mod

        _install_fake_ddgs(
            monkeypatch,
            image_results=[{"title": "Cat", "thumbnail": "http://img/cat.png"}],
        )
        out = json.loads(img_mod.image_search_tool.invoke({"query": "cat"}))
        assert out["total_results"] == 1
        assert out["results"][0]["image_url"] == "http://img/cat.png"
        assert out["results"][0]["thumbnail_url"] == "http://img/cat.png"


# ===========================================================================
# Test brave
# ===========================================================================


class TestBrave:
    def test_no_api_key_returns_error(self, monkeypatch):
        from deerflow.community.brave import tools as brave_mod

        _patch_config(monkeypatch, None)
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        out = json.loads(brave_mod.web_search_tool.invoke({"query": "q"}))
        assert "BRAVE_SEARCH_API_KEY" in out["error"]

    def test_coerce_max_results_clamps(self):
        from deerflow.community.brave.tools import _BRAVE_MAX_COUNT, _coerce_max_results

        assert _coerce_max_results(100) == _BRAVE_MAX_COUNT  # 上限 20
        assert _coerce_max_results(0) == 1  # 下限 1
        assert _coerce_max_results("abc", default=5) == 5  # 非法 → default
        assert _coerce_max_results("7") == 7

    def test_search_normalizes_results(self, monkeypatch):
        from deerflow.community.brave import tools as brave_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(get_response=_FakeResponse(json_data={"web": {"results": [{"title": "T", "url": "http://u", "description": "D"}]}}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(brave_mod.web_search_tool.invoke({"query": "q"}))
        assert out["results"][0] == {"title": "T", "url": "http://u", "content": "D"}

    def test_search_http_error(self, monkeypatch):
        from deerflow.community.brave import tools as brave_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(get_response=_FakeResponse(status_code=503, text="unavailable"))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(brave_mod.web_search_tool.invoke({"query": "q"}))
        assert "503" in out["error"]

    def test_search_no_results(self, monkeypatch):
        from deerflow.community.brave import tools as brave_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(get_response=_FakeResponse(json_data={"web": {"results": []}}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(brave_mod.web_search_tool.invoke({"query": "q"}))
        assert out["error"] == "No results found"


# ===========================================================================
# Test serper
# ===========================================================================


class TestSerper:
    def test_no_api_key_returns_error(self, monkeypatch):
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, None)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        out = json.loads(serper_mod.web_search_tool.invoke({"query": "q"}))
        assert "SERPER_API_KEY" in out["error"]

    def test_search_normalizes(self, monkeypatch):
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"organic": [{"title": "T", "link": "http://u", "snippet": "S"}]}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(serper_mod.web_search_tool.invoke({"query": "q"}))
        assert out["results"][0] == {"title": "T", "url": "http://u", "content": "S"}

    def test_search_empty_organic(self, monkeypatch):
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"organic": []}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(serper_mod.web_search_tool.invoke({"query": "q"}))
        assert out["error"] == "No results found"


# ===========================================================================
# Test searxng
# ===========================================================================


class TestSearxng:
    @pytest.mark.asyncio
    async def test_search_normalizes(self, monkeypatch):
        from deerflow.community.searxng import tools as sx_mod

        _patch_config(monkeypatch, {"web_search": {"base_url": "http://localhost:8088"}})
        client = _FakeAsyncClient(get_response=_FakeResponse(json_data={"results": [{"title": "T", "url": "http://u", "content": "C"}]}))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        out = json.loads(await sx_mod.web_search_tool.ainvoke({"query": "q"}))
        assert out == [{"title": "T", "url": "http://u", "snippet": "C"}]

    @pytest.mark.asyncio
    async def test_search_request_error(self, monkeypatch):
        from deerflow.community.searxng import tools as sx_mod

        _patch_config(monkeypatch, {"web_search": {"base_url": "http://localhost:8088"}})

        class _BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *a, **kw):
                raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _BoomClient())
        out = json.loads(await sx_mod.web_search_tool.ainvoke({"query": "q"}))
        assert "error" in out


# ===========================================================================
# Test browserless
# ===========================================================================


class TestBrowserless:
    @pytest.mark.asyncio
    async def test_fetch_markdown_truncated(self, monkeypatch):
        from deerflow.community.browserless import tools as bl_mod

        _patch_config(monkeypatch, {"web_fetch": {"base_url": "http://localhost:3032"}})
        long_html = "<html><head><title>P</title></head><body><p>" + "B" * 9999 + "</p></body></html>"
        client = _FakeAsyncClient(post_response=_FakeResponse(status_code=200, text=long_html))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        result = await bl_mod.web_fetch_tool.ainvoke({"url": "http://u"})
        assert result.startswith("# P")
        assert len(result) < 9999

    @pytest.mark.asyncio
    async def test_fetch_error_passthrough(self, monkeypatch):
        from deerflow.community.browserless import tools as bl_mod

        _patch_config(monkeypatch, {"web_fetch": {"base_url": "http://localhost:3032"}})
        client = _FakeAsyncClient(post_response=_FakeResponse(status_code=502, text="bad gateway"))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
        result = await bl_mod.web_fetch_tool.ainvoke({"url": "http://u"})
        assert result.startswith("Error:")
        assert "502" in result


# ===========================================================================
# Test 软加载占位 provider
# ===========================================================================


class TestSoftLoadPlaceholders:
    def test_firecrawl_search_missing_sdk(self):
        from deerflow.community.firecrawl import tools as fc_mod

        result = fc_mod.web_search_tool.invoke({"query": "q"})
        assert "firecrawl-py" in result
        assert result.startswith("Error:")

    def test_firecrawl_fetch_missing_sdk(self):
        from deerflow.community.firecrawl import tools as fc_mod

        result = fc_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert "firecrawl-py" in result

    def test_exa_search_missing_sdk(self):
        from deerflow.community.exa import tools as exa_mod

        result = exa_mod.web_search_tool.invoke({"query": "q"})
        assert "exa_py" in result

    def test_exa_fetch_missing_sdk(self):
        from deerflow.community.exa import tools as exa_mod

        result = exa_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert "exa_py" in result

    def test_infoquest_no_api_key(self, monkeypatch):
        from deerflow.community.infoquest import tools as iq_mod

        monkeypatch.delenv("INFOQUEST_API_KEY", raising=False)
        assert iq_mod.web_search_tool.invoke({"query": "q"}).startswith("Error:")
        assert iq_mod.image_search_tool.invoke({"query": "q"}).startswith("Error:")
        assert iq_mod.web_fetch_tool.invoke({"url": "http://u"}).startswith("Error:")


# ===========================================================================
# Test 加载机制：所有 provider 经 resolve_variable 可 resolve（M15 tools[].use: 路径）
# ===========================================================================


class TestResolvePaths:
    """验证 config.yaml 的 ``tools[].use:`` 路径经 ``resolve_variable`` 能 resolve 到 BaseTool。

    这是 M15 ``get_available_tools`` 加载 community 工具的契约——本测试锁定它。
    """

    @pytest.mark.parametrize(
        "path,attr",
        [
            ("deerflow.community.ddg_search.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.tavily.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.tavily.tools:web_fetch_tool", "web_fetch_tool"),
            ("deerflow.community.jina_ai.tools:web_fetch_tool", "web_fetch_tool"),
            ("deerflow.community.image_search.tools:image_search_tool", "image_search_tool"),
            ("deerflow.community.brave.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.serper.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.searxng.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.browserless.tools:web_fetch_tool", "web_fetch_tool"),
            ("deerflow.community.firecrawl.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.exa.tools:web_search_tool", "web_search_tool"),
            ("deerflow.community.infoquest.tools:web_search_tool", "web_search_tool"),
        ],
    )
    def test_resolve_variable_finds_tool(self, path, attr):
        from langchain.tools import BaseTool

        from deerflow.reflection import resolve_variable

        obj = resolve_variable(path, BaseTool)
        assert isinstance(obj, BaseTool)
        # 软加载 provider 的工具即使 SDK 缺包也 resolve 成功（缺包在调用时才报错）


# ===========================================================================
# Test fastcrw（Firecrawl 兼容变体，软加载）
# ===========================================================================


class TestFastcrw:
    def test_search_normalizes(self, monkeypatch):
        from deerflow.community.fastcrw import tools as fc_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})

        class _Result:
            def __init__(self):
                self.web = [types.SimpleNamespace(title="T", url="http://u", description="D")]

        class _App:
            def __init__(self, *a, **kw):
                pass

            def search(self, query, limit=5):
                return _Result()

        monkeypatch.setitem(__import__("sys").modules, "firecrawl", types.SimpleNamespace(FirecrawlApp=_App))
        out = json.loads(fc_mod.web_search_tool.invoke({"query": "q"}))
        assert out[0] == {"title": "T", "url": "http://u", "content": "D"}

    def test_search_missing_sdk_returns_install_hint(self, monkeypatch):
        from deerflow.community.fastcrw import tools as fc_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        # 让 ``from firecrawl import FirecrawlApp`` 抛 ImportError
        import builtins

        real_import = builtins.__import__

        def _block_firecrawl(name, *a, **kw):
            if name == "firecrawl":
                raise ImportError("no firecrawl")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _block_firecrawl)
        out = fc_mod.web_search_tool.invoke({"query": "q"})
        assert "firecrawl-py" in out and out.startswith("Error:")

    def test_fetch_truncates_and_titles(self, monkeypatch):
        from deerflow.community.fastcrw import tools as fc_mod

        _patch_config(monkeypatch, {"web_fetch": {"api_key": "k"}})

        class _Meta:
            title = "Title"

        class _Result:
            markdown = "x" * 5000
            metadata = _Meta()

        class _App:
            def __init__(self, *a, **kw):
                pass

            def scrape(self, url, formats=None):
                return _Result()

        monkeypatch.setitem(__import__("sys").modules, "firecrawl", types.SimpleNamespace(FirecrawlApp=_App))
        out = fc_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert out.startswith("# Title")
        assert len(out) < 5000  # 截到 4KB


# ===========================================================================
# Test groundroute（纯 httpx meta 搜索）
# ===========================================================================


class TestGroundroute:
    def test_no_api_key_returns_error(self, monkeypatch):
        from deerflow.community.groundroute import tools as gr_mod

        _patch_config(monkeypatch, None)
        monkeypatch.delenv("GROUNDROUTE_API_KEY", raising=False)
        out = json.loads(gr_mod.web_search_tool.invoke({"query": "q"}))
        assert "GROUNDROUTE_API_KEY" in out["error"]

    def test_search_normalizes(self, monkeypatch):
        from deerflow.community.groundroute import tools as gr_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"results": [{"title": "T", "url": "http://u", "snippet": "S", "source_engine": "serper"}]}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(gr_mod.web_search_tool.invoke({"query": "q"}))
        assert out[0] == {"title": "T", "url": "http://u", "snippet": "S", "source_engine": "serper"}

    def test_search_empty_results(self, monkeypatch):
        from deerflow.community.groundroute import tools as gr_mod

        _patch_config(monkeypatch, {"web_search": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"results": []}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(gr_mod.web_search_tool.invoke({"query": "q"}))
        assert out["error"] == "No results found"

    def test_fetch_returns_content(self, monkeypatch):
        from deerflow.community.groundroute import tools as gr_mod

        _patch_config(monkeypatch, {"web_fetch": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"results": [{"title": "T", "content": "C"}]}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = gr_mod.web_fetch_tool.invoke({"url": "http://u"})
        assert out.startswith("# T") and "C" in out


# ===========================================================================
# Test Serper image_search + SSRF 守卫（#3575）
# ===========================================================================


class TestSerperImages:
    def test_image_search_filters_private_urls(self, monkeypatch):
        """#3575：私有/loopback 图片 URL 被 SSRF 守卫滤掉。"""
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, {"image_search": {"api_key": "k"}})
        client = _FakeSyncClient(
            post_response=_FakeResponse(
                json_data={
                    "images": [
                        {"title": "safe", "imageUrl": "https://example.com/a.png", "thumbnailUrl": "https://example.com/t.png"},
                        {"title": "private", "imageUrl": "http://127.0.0.1/x.png", "thumbnailUrl": "http://10.0.0.1/t.png"},
                        {"title": "obfuscated", "imageUrl": "http://2130706433/x.png"},  # 127.0.0.1 的十进制
                    ]
                }
            )
        )
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(serper_mod.image_search_tool.invoke({"query": "cat"}))
        titles = [r["title"] for r in out["results"]]
        assert titles == ["safe"]  # private + obfuscated 都被滤掉

    def test_image_search_no_safe_urls(self, monkeypatch):
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, {"image_search": {"api_key": "k"}})
        client = _FakeSyncClient(post_response=_FakeResponse(json_data={"images": [{"imageUrl": "http://192.168.1.1/x.png"}]}))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        out = json.loads(serper_mod.image_search_tool.invoke({"query": "q"}))
        assert out["error"] == "No safe image URLs found"

    def test_image_search_no_api_key(self, monkeypatch):
        from deerflow.community.serper import tools as serper_mod

        _patch_config(monkeypatch, None)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        out = json.loads(serper_mod.image_search_tool.invoke({"query": "q"}))
        assert "SERPER_API_KEY" in out["error"]


class TestSerperSSRFGuard:
    """``_safe_public_url`` 的 SSRF 守卫单测（#3575）。"""

    def test_rejects_non_http_schemes(self):
        from deerflow.community.serper.tools import _safe_public_url as f

        assert f("ftp://example.com/x") == ""
        assert f("file:///etc/passwd") == ""
        assert f("javascript:alert(1)") == ""

    def test_rejects_localhost_and_private_ip(self):
        from deerflow.community.serper.tools import _safe_public_url as f

        assert f("http://localhost/x") == ""
        assert f("http://localhost./x") == ""
        assert f("http://sub.localhost/x") == ""
        assert f("http://127.0.0.1/x") == ""
        assert f("http://10.0.0.1/x") == ""
        assert f("http://192.168.1.1/x") == ""
        assert f("http://172.16.0.1/x") == ""

    def test_accepts_public_urls(self):
        from deerflow.community.serper.tools import _safe_public_url as f

        assert f("https://example.com/a.png") == "https://example.com/a.png"
        assert f("http://8.8.8.8/x") == "http://8.8.8.8/x"

    def test_rejects_obfuscated_ipv4_literals(self):
        """十进制 / 十六进制 / 八进制混淆的私有 IP 也要被识破。"""
        from deerflow.community.serper.tools import _safe_public_url as f

        assert f("http://2130706433/x") == ""  # 127.0.0.1 十进制
        assert f("http://0x7f000001/x") == ""  # 127.0.0.1 十六进制
        assert f("http://0177.0.0.1/x") == ""  # 127.0.0.1 八进制

    def test_decode_ipv4_returns_address_for_obfuscated(self):
        from ipaddress import ip_address

        from deerflow.community.serper.tools import _decode_ipv4

        assert _decode_ipv4("2130706433") == ip_address("127.0.0.1")
        assert _decode_ipv4("0x7f000001") == ip_address("127.0.0.1")
        assert _decode_ipv4("cafe.com") is None  # 真域名解不了

    def test_non_string_returns_empty(self):
        from deerflow.community.serper.tools import _safe_public_url as f

        assert f(None) == ""
        assert f(123) == ""

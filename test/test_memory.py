"""``test_memory.py`` —— 记忆模块（M13）hermetic 测试。

覆盖（对齐 deer ``tests/test_memory_*.py``，mini 适配 ``DEER_FLOW_HOME`` 驱动 get_paths）：

- **storage**：load/save/reload、per-user 隔离、per-agent、原子写（temp 残留清理）、损坏 JSON
  回退空、mtime 缓存、``storage_path`` 绝对路径退出隔离、agent_name 校验（红线 #32）。
- **message_processing**：filter（跳 tool-call AI / 剥纯上传 / 留 user+最终 AI）、
  detect_correction / detect_reinforcement（中英模式）。
- **prompt**：format_memory_for_injection（空 / 段 / fact 按置信度排序 / token 预算截断 /
  char 模式 / correction sourceError）、format_conversation_for_update（剥上传 / 截断 / 角色）、
  _count_tokens（char vs tiktoken）、tiktoken 冷却降级。
- **updater**：_parse_memory_update_response（干净 / 围栏 / 思考包裹 / 损坏抛 / 不安全部分更新抛）、
  _apply_updates（shouldUpdate / fact 去重 casefold / 置信度阈值 / max_facts 裁剪 / factsToRemove）、
  _strip_upload_mentions、fact CRUD、clear、_finalize_update（fake model）。
- **queue**：去抖合并（同 key）/ 不同 key 分开 / user_id 跨线程捕获 / add_nowait 立即 /
  enabled 跳过 / flush / clear / pending_count。
- **middleware**：enabled 跳过 / 无 thread_id 跳过 / 无 messages 跳过 / 无 user 或 assistant 跳过 /
  user_id 捕获 / agent_name 传递 / correction 检测。
- **dynamic_context + _get_memory_context**：禁用→"" / 空→"" / 注入门控 / 首轮 ID-swap /
  同天 no-op / 跨午夜日期更新。

hermetic：``DEER_FLOW_HOME`` → tmp_path；autouse 重置 queue + storage 单例防跨测试污染；
updater 用 fake model（不碰真 LLM）；middleware 的 get_config 经 monkeypatch 替身。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.agents.memory import (
    FileMemoryStorage,
    MemoryUpdateQueue,
    create_empty_memory,
    create_memory_fact,
    delete_memory_fact,
    format_conversation_for_update,
    format_memory_for_injection,
    get_memory_data,
    get_memory_queue,
    get_memory_storage,
    update_memory_fact,
)
from deerflow.agents.memory import prompt as prompt_module
from deerflow.agents.memory import queue as queue_module
from deerflow.agents.memory import storage as storage_module
from deerflow.agents.memory import updater as updater_module
from deerflow.agents.memory.message_processing import (
    detect_correction,
    detect_reinforcement,
    filter_messages_for_memory,
)
from deerflow.agents.memory.updater import (
    MemoryUpdater,
    _parse_memory_update_response,
    _strip_upload_mentions_from_memory,
)

# ---------------------------------------------------------------------------
# fixtures
# ===========================================================================


@pytest.fixture()
def home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``DEER_FLOW_HOME`` → 临时目录；返回 resolve 后的 base_dir（路径相等断言用）。"""
    home = (tmp_path / "deer-flow-home").resolve()
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_memory_singletons(monkeypatch):
    """每个测试前后重置 queue + storage 单例，防跨测试污染 / 阻塞 timer 触发真 LLM。"""
    # 测试前：清掉单例
    queue_module._memory_queue = None
    storage_module._storage_instance = None
    yield
    # 测试后：清 queue（cancel timer）+ 清单例
    q = queue_module._memory_queue
    if q is not None:
        q.clear()
    queue_module._memory_queue = None
    storage_module._storage_instance = None


def _human(text: str):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=text)


def _ai(text: str, tool_calls=None):
    from langchain_core.messages import AIMessage

    return AIMessage(content=text, tool_calls=tool_calls or [])


def _fake_runtime(context: dict | None = None):
    """构造最小 runtime 桩（``runtime.context`` 是 dict）。"""
    return SimpleNamespace(context=context)


def _patch_mem_config(monkeypatch: pytest.MonkeyPatch, config) -> None:
    """把 ``get_memory_config`` 替身注入所有在模块加载期绑定它的消费方。

    ``storage`` / ``queue`` / ``updater`` / middleware 都 ``from ... import get_memory_config``
    了自己的绑定，patch 源模块不传播——必须逐模块替换。
    """
    from deerflow.agents.middlewares import memory_middleware as mm
    from deerflow.config import memory_config as mc

    for mod in (storage_module, queue_module, updater_module, mm, mc):
        monkeypatch.setattr(mod, "get_memory_config", lambda: config)


# ===========================================================================
# 1. storage
# ===========================================================================


class TestStorage:
    def test_create_empty_memory_shape(self):
        mem = create_empty_memory()
        assert mem["version"] == "1.0"
        assert "lastUpdated" in mem
        assert mem["facts"] == []
        for section in ("workContext", "personalContext", "topOfMind"):
            assert mem["user"][section] == {"summary": "", "updatedAt": ""}
        for section in ("recentMonths", "earlierContext", "longTermBackground"):
            assert mem["history"][section] == {"summary": "", "updatedAt": ""}

    def test_load_missing_file_returns_empty(self, home_env):
        storage = FileMemoryStorage()
        mem = storage.load(user_id="u1")
        assert mem["facts"] == []
        assert mem["user"]["workContext"]["summary"] == ""

    def test_save_then_load_roundtrip(self, home_env):
        storage = FileMemoryStorage()
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = "Engineer at Acme"
        mem["facts"].append({"id": "f1", "content": "likes python", "category": "preference", "confidence": 0.9})
        assert storage.save(mem, user_id="u1") is True

        loaded = FileMemoryStorage().load(user_id="u1")  # 新实例也读到（盘上持久）
        assert loaded["user"]["workContext"]["summary"] == "Engineer at Acme"
        assert loaded["facts"][0]["content"] == "likes python"
        # save 注入 lastUpdated
        assert loaded["lastUpdated"]

    def test_per_user_isolation(self, home_env):
        storage = FileMemoryStorage()
        mem_a = create_empty_memory()
        mem_a["user"]["workContext"]["summary"] = "Alice's job"
        storage.save(mem_a, user_id="alice")

        mem_b = create_empty_memory()
        mem_b["user"]["workContext"]["summary"] = "Bob's job"
        storage.save(mem_b, user_id="bob")

        # 各读各的，互不影响
        fresh = FileMemoryStorage()
        assert fresh.load(user_id="alice")["user"]["workContext"]["summary"] == "Alice's job"
        assert fresh.load(user_id="bob")["user"]["workContext"]["summary"] == "Bob's job"

    def test_per_agent_isolation(self, home_env):
        storage = FileMemoryStorage()
        mem = create_empty_memory()
        mem["facts"].append({"id": "f1", "content": "agent fact", "category": "context", "confidence": 0.9})
        storage.save(mem, agent_name="reviewer", user_id="u1")

        # 全局记忆不含 per-agent fact
        assert FileMemoryStorage().load(user_id="u1")["facts"] == []
        # per-agent 读到
        assert FileMemoryStorage().load(agent_name="reviewer", user_id="u1")["facts"][0]["content"] == "agent fact"

    def test_corrupt_json_falls_back_to_empty(self, home_env):
        # 写一个损坏的 memory.json
        mem_file = home_env / "users" / "u1" / "memory.json"
        mem_file.parent.mkdir(parents=True)
        mem_file.write_text("{not valid json", encoding="utf-8")

        storage = FileMemoryStorage()
        mem = storage.load(user_id="u1")
        assert mem["facts"] == []  # 回退空结构，不抛

    def test_atomic_write_no_temp_residue(self, home_env):
        storage = FileMemoryStorage()
        storage.save(create_empty_memory(), user_id="u1")
        # 写完后无残留 .tmp 文件
        user_dir = home_env / "users" / "u1"
        temps = list(user_dir.glob("*.tmp"))
        assert temps == []
        assert (user_dir / "memory.json").exists()

    def test_mtime_cache_invalidates_on_external_change(self, home_env):
        storage = FileMemoryStorage()
        storage.save(create_empty_memory(), user_id="u1")
        first = storage.load(user_id="u1")
        assert first["facts"] == []

        # 外部改盘（绕过缓存）
        mem_file = home_env / "users" / "u1" / "memory.json"
        data = json.loads(mem_file.read_text(encoding="utf-8"))
        data["facts"].append({"id": "x", "content": "external", "category": "context", "confidence": 0.9})
        # 保证 mtime 变化（某些文件系统精度低）
        time.sleep(0.01)
        mem_file.write_text(json.dumps(data), encoding="utf-8")

        # mtime 变了 → 缓存失效 → 重读
        reloaded = storage.load(user_id="u1")
        assert any(f.get("content") == "external" for f in reloaded["facts"])

    def test_reload_forces_cache_bypass(self, home_env):
        storage = FileMemoryStorage()
        storage.save(create_empty_memory(), user_id="u1")
        # 改盘
        mem_file = home_env / "users" / "u1" / "memory.json"
        data = json.loads(mem_file.read_text(encoding="utf-8"))
        data["user"]["personalContext"]["summary"] = "reloaded"
        mem_file.write_text(json.dumps(data), encoding="utf-8")

        reloaded = storage.reload(user_id="u1")
        assert reloaded["user"]["personalContext"]["summary"] == "reloaded"

    def test_absolute_storage_path_opts_out_of_isolation(self, home_env, monkeypatch, tmp_path):
        # 绝对 storage_path → 所有 user 共享同一文件
        shared = tmp_path / "shared.json"
        _patch_mem_config(monkeypatch, _mem_config_with_storage(str(shared)))
        storage = FileMemoryStorage()
        storage.save(create_empty_memory(), user_id="alice")
        # 文件写到共享路径（所有 user 共享）
        assert shared.exists()

    def test_validate_agent_name_pattern(self, home_env):
        storage = FileMemoryStorage()
        # 合法名 OK
        storage.save(create_empty_memory(), agent_name="code-reviewer", user_id="u1")
        # 非法名（下划线）抛——红线 #32
        with pytest.raises(ValueError, match="Invalid agent name"):
            storage.save(create_empty_memory(), agent_name="bad_name", user_id="u1")
        # 空名抛
        with pytest.raises(ValueError, match="non-empty"):
            storage.save(create_empty_memory(), agent_name="", user_id="u1")

    def test_get_memory_storage_singleton(self, home_env):
        s1 = get_memory_storage()
        s2 = get_memory_storage()
        assert s1 is s2
        assert isinstance(s1, FileMemoryStorage)

    def test_get_memory_storage_fallback_on_bad_class(self, home_env, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_with_storage_class("nonexistent.module:Nope"))
        storage_module._storage_instance = None  # 强制重新解析
        s = get_memory_storage()
        assert isinstance(s, FileMemoryStorage)  # 回退


def _mem_config_with_storage(path: str):
    """构造一个 storage_path=path 的 MemoryConfig 替身。"""
    cfg = MagicMock()
    cfg.storage_path = path
    cfg.storage_class = "deerflow.agents.memory.storage.FileMemoryStorage"
    return cfg


def _mem_config_with_storage_class(cls_path: str):
    cfg = MagicMock()
    cfg.storage_path = ""
    cfg.storage_class = cls_path
    return cfg


# ===========================================================================
# 2. message_processing
# ===========================================================================


class TestMessageProcessing:
    def test_filter_keeps_user_and_final_ai(self):
        msgs = [_human("hi"), _ai("tool call", tool_calls=[{"name": "bash", "args": {}, "id": "1"}]), _ai("final answer")]
        filtered = filter_messages_for_memory(msgs)
        types = [getattr(m, "type", None) for m in filtered]
        assert types == ["human", "ai"]  # tool-call AI 被跳，只留最终 AI

    def test_filter_strips_upload_only_message(self):
        upload_msg = _human("<uploaded_files>[{'name':'a.pdf'}]</uploaded_files>")
        msgs = [upload_msg, _ai("thanks for the file")]
        filtered = filter_messages_for_memory(msgs)
        # 纯上传消息 + 其后 AI 回复都被跳过
        assert filtered == []

    def test_filter_strips_upload_block_but_keeps_rest(self):
        msgs = [_human("<uploaded_files>[{'name':'a.pdf'}]</uploaded_files>\nplease review")]
        filtered = filter_messages_for_memory(msgs)
        assert len(filtered) == 1
        assert "uploaded_files" not in getattr(filtered[0], "content", "")
        assert "please review" in filtered[0].content

    def test_filter_drops_hide_from_ui_messages(self):
        """#3697：``hide_from_ui`` 标记的 human 消息（中间件注入的隐藏消息）不进记忆 LLM。"""
        from langchain_core.messages import HumanMessage

        hidden = HumanMessage(content="todo reminder", additional_kwargs={"hide_from_ui": True})
        msgs = [hidden, _human("real question"), _ai("answer")]
        filtered = filter_messages_for_memory(msgs)
        contents = [getattr(m, "content", "") for m in filtered]
        assert "todo reminder" not in contents  # hide_from_ui 那条被丢
        assert len(filtered) == 2  # 只留 real question + answer

    def test_detect_correction_english(self):
        assert detect_correction([_human("that's wrong, try again")]) is True
        assert detect_correction([_human("you misunderstood me")]) is True

    def test_detect_correction_chinese(self):
        assert detect_correction([_human("不对，重新来")]) is True
        assert detect_correction([_human("你理解错了")]) is True

    def test_detect_correction_negative(self):
        assert detect_correction([_human("that's great")]) is False

    def test_detect_reinforcement_english(self):
        assert detect_reinforcement([_human("yes, exactly right")]) is True
        assert detect_reinforcement([_human("perfect.")]) is True

    def test_detect_reinforcement_chinese(self):
        assert detect_reinforcement([_human("完全正确。")]) is True
        assert detect_reinforcement([_human("对，就是这样")]) is True

    def test_detect_reinforcement_negative(self):
        assert detect_reinforcement([_human("try again")]) is False


# ===========================================================================
# 3. prompt
# ===========================================================================


class TestFormatMemoryForInjection:
    def test_empty_returns_empty(self):
        assert format_memory_for_injection({}) == ""
        assert format_memory_for_injection(create_empty_memory()) == ""

    def test_user_context_sections(self):
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = "Engineer"
        mem["user"]["topOfMind"]["summary"] = "Building X"
        out = format_memory_for_injection(mem, use_tiktoken=False)
        assert "Work: Engineer" in out
        assert "Current Focus: Building X" in out
        assert "User Context:" in out

    def test_facts_ranked_by_confidence(self):
        mem = create_empty_memory()
        mem["facts"] = [
            {"content": "low", "category": "context", "confidence": 0.5},
            {"content": "high", "category": "preference", "confidence": 0.99},
            {"content": "mid", "category": "knowledge", "confidence": 0.7},
        ]
        out = format_memory_for_injection(mem, use_tiktoken=False)
        # high 排在 low 前面
        assert out.index("high") < out.index("low")
        assert "[preference | 0.99]" in out

    def test_token_budget_truncation(self):
        # 超大 user 段 + 小预算 → 最终串超预算 → 按 token/字符比例截断尾部加 ...
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = "x" * 500
        out = format_memory_for_injection(mem, max_tokens=20, use_tiktoken=False)
        assert "..." in out  # 截断标记
        assert len(out) < 600  # 远短于未截断全量

    def test_facts_incrementally_fit_budget(self):
        # fact 逐条加入直到预算耗尽；超出预算的 fact 被跳过（不截断单条）
        mem = create_empty_memory()
        for i in range(20):
            mem["facts"].append({"content": f"fact-{i}-" * 10, "category": "context", "confidence": 0.9})
        out = format_memory_for_injection(mem, max_tokens=100, use_tiktoken=False)
        # 不是全部 20 条都进去（预算有限）
        assert out.count("- [context") < 20

    def test_char_mode_vs_tiktoken_mode(self):
        mem = create_empty_memory()
        mem["facts"].append({"content": "a test fact", "category": "context", "confidence": 0.9})
        char_out = format_memory_for_injection(mem, use_tiktoken=False)
        assert "a test fact" in char_out  # char 模式正常工作（不碰 tiktoken）

    def test_correction_fact_with_source_error(self):
        mem = create_empty_memory()
        mem["facts"].append({"content": "use async path", "category": "correction", "confidence": 0.99, "sourceError": "sync caused loop bug"})
        out = format_memory_for_injection(mem, use_tiktoken=False)
        assert "(avoid: sync caused loop bug)" in out

    def test_empty_summary_skipped(self):
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = ""  # 空
        mem["user"]["personalContext"]["summary"] = "kept"
        out = format_memory_for_injection(mem, use_tiktoken=False)
        assert "Work:" not in out
        assert "Personal: kept" in out


class TestFormatConversationForUpdate:
    def test_strips_upload_blocks(self):
        msgs = [_human("<uploaded_files>[{'name':'a.pdf'}]</uploaded_files>\nreview this")]
        out = format_conversation_for_update(msgs)
        assert "uploaded_files" not in out
        assert "review this" in out

    def test_skips_upload_only_turn(self):
        msgs = [_human("<uploaded_files>[{'name':'a.pdf'}]</uploaded_files>")]
        assert format_conversation_for_update(msgs) == ""

    def test_truncates_long_messages(self):
        long_text = "x" * 2000
        msgs = [_human(long_text)]
        out = format_conversation_for_update(msgs)
        assert "..." in out
        assert len(out) < 2000

    def test_roles_labeled(self):
        msgs = [_human("hello"), _ai("world")]
        out = format_conversation_for_update(msgs)
        assert "User: hello" in out
        assert "Assistant: world" in out


class TestCountTokens:
    def test_char_estimate_english(self):
        # char 模式：英文 ~4 字符/token
        n = prompt_module._char_based_token_estimate("hello world!")  # 12 字符
        assert n == 3  # 12 // 4

    def test_char_estimate_cjk(self):
        # CJK ~2 字符/token
        n = prompt_module._char_based_token_estimate("你好世界")  # 4 CJK
        assert n == 2  # 4 // 2

    def test_count_tokens_char_mode_skips_tiktoken(self):
        # char 模式不碰 tiktoken
        n = prompt_module._count_tokens("hello", use_tiktoken=False)
        assert n > 0

    def test_count_tokens_tiktoken_unavailable_falls_back(self, monkeypatch):
        # 模拟 tiktoken 不可用 → 回退 char 估算
        monkeypatch.setattr(prompt_module, "_get_tiktoken_encoding", lambda name="cl100k_base": None)
        n = prompt_module._count_tokens("hello world", use_tiktoken=True)
        assert n > 0  # 回退成功


# ===========================================================================
# 4. updater
# ===========================================================================


class TestParseMemoryUpdateResponse:
    def test_clean_json(self):
        resp = json.dumps(
            {
                "user": {"workContext": {"summary": "Eng", "shouldUpdate": True}},
                "history": {},
                "newFacts": [{"content": "likes python", "category": "preference", "confidence": 0.9}],
                "factsToRemove": [],
            }
        )
        parsed = _parse_memory_update_response(resp)
        assert parsed["user"]["workContext"]["summary"] == "Eng"
        assert parsed["newFacts"][0]["content"] == "likes python"

    def test_fenced_json(self):
        resp = "```json\n" + json.dumps({"user": {}, "history": {}, "newFacts": [], "factsToRemove": []}) + "\n```"
        parsed = _parse_memory_update_response(resp)
        assert parsed["newFacts"] == []

    def test_thinking_wrapped_json(self):
        # 模型把 JSON 包在散文里
        resp = "Let me analyze...\n" + json.dumps({"user": {}, "history": {}, "newFacts": [{"content": "x", "category": "context", "confidence": 0.9}], "factsToRemove": []}) + "\nDone."
        parsed = _parse_memory_update_response(resp)
        assert len(parsed["newFacts"]) == 1

    def test_malformed_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_memory_update_response("not json at all")

    def test_unsafe_partial_update_raises(self):
        # factsToRemove 非空 + newFacts 含非法条目 → 视为不安全部分更新，抛
        resp = json.dumps(
            {
                "user": {},
                "history": {},
                "newFacts": [{"content": "", "category": "x", "confidence": 0.5}],  # 空 content 非法
                "factsToRemove": ["fact_abc"],
            }
        )
        with pytest.raises(json.JSONDecodeError, match="Unsafe partial"):
            _parse_memory_update_response(resp)

    def test_drops_invalid_fact_fields(self):
        resp = json.dumps(
            {
                "user": {},
                "history": {},
                "newFacts": [
                    {"content": "valid", "category": "context", "confidence": 0.9},
                    {"content": 123, "category": "x", "confidence": 0.5},  # content 非字符串 → 丢
                    "not a dict",  # 非 dict → 丢
                ],
                "factsToRemove": [],
            }
        )
        parsed = _parse_memory_update_response(resp)
        assert len(parsed["newFacts"]) == 1
        assert parsed["newFacts"][0]["content"] == "valid"

    def test_confidence_string_coerced(self):
        resp = json.dumps(
            {
                "user": {},
                "history": {},
                "newFacts": [{"content": "x", "category": "context", "confidence": "0.85"}],
                "factsToRemove": [],
            }
        )
        parsed = _parse_memory_update_response(resp)
        assert parsed["newFacts"][0]["confidence"] == 0.85


class TestApplyUpdates:
    def test_user_section_updated_when_should_update(self):
        mem = create_empty_memory()
        update = {"user": {"workContext": {"summary": "New role", "shouldUpdate": True}}, "history": {}, "newFacts": [], "factsToRemove": []}
        result = MemoryUpdater()._apply_updates(mem, update)
        assert result["user"]["workContext"]["summary"] == "New role"
        assert result["user"]["workContext"]["updatedAt"]  # 时间戳被设

    def test_user_section_skipped_when_should_update_false(self):
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = "Old"
        update = {"user": {"workContext": {"summary": "New", "shouldUpdate": False}}, "history": {}, "newFacts": [], "factsToRemove": []}
        result = MemoryUpdater()._apply_updates(mem, update)
        assert result["user"]["workContext"]["summary"] == "Old"  # 未改

    def test_fact_dedup_casefold(self, monkeypatch):
        # 低阈值让所有 fact 都进
        _patch_mem_config(monkeypatch, _mem_config_simple(fact_confidence_threshold=0.0))
        mem = create_empty_memory()
        mem["facts"].append({"id": "f1", "content": "Likes Python", "category": "preference", "confidence": 0.9})
        update = {
            "user": {},
            "history": {},
            "newFacts": [{"content": "likes python", "category": "preference", "confidence": 0.9}],  # casefold 相同 → 去重
            "factsToRemove": [],
        }
        result = MemoryUpdater()._apply_updates(mem, update)
        assert len(result["facts"]) == 1  # 去重，不重复加

    def test_whitespace_only_fact_skipped(self, monkeypatch):
        """#3719：空白/纯空白 content 的 fact 跳过（不写空 fact 进记忆）。

        旧版 ``if fact_key is not None and fact_key in existing`` 合并条件在
        fact_key is None（空白 content）时为 False、不跳过，空 fact 仍被 append。"""
        _patch_mem_config(monkeypatch, _mem_config_simple(fact_confidence_threshold=0.0))
        mem = create_empty_memory()
        update = {
            "user": {},
            "history": {},
            "newFacts": [
                {"content": "   ", "category": "context", "confidence": 0.9},  # 纯空白
                {"content": "", "category": "context", "confidence": 0.9},  # 空
                {"content": "real fact", "category": "context", "confidence": 0.9},
            ],
            "factsToRemove": [],
        }
        result = MemoryUpdater()._apply_updates(mem, update)
        assert len(result["facts"]) == 1  # 只有 real fact 进
        assert result["facts"][0]["content"] == "real fact"

    def test_fact_below_confidence_threshold_dropped(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple(fact_confidence_threshold=0.8))
        mem = create_empty_memory()
        update = {
            "user": {},
            "history": {},
            "newFacts": [{"content": "low conf", "category": "context", "confidence": 0.5}],
            "factsToRemove": [],
        }
        result = MemoryUpdater()._apply_updates(mem, update)
        assert len(result["facts"]) == 0  # 低于阈值丢弃

    def test_max_facts_trim_keeps_top_confidence(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple(max_facts=2, fact_confidence_threshold=0.0))
        mem = create_empty_memory()
        update = {
            "user": {},
            "history": {},
            "newFacts": [
                {"content": "a", "category": "context", "confidence": 0.5},
                {"content": "b", "category": "context", "confidence": 0.99},
                {"content": "c", "category": "context", "confidence": 0.7},
            ],
            "factsToRemove": [],
        }
        result = MemoryUpdater()._apply_updates(mem, update)
        assert len(result["facts"]) == 2
        contents = [f["content"] for f in result["facts"]]
        assert "b" in contents  # 最高置信度留下
        assert "a" not in contents  # 最低被裁

    def test_facts_to_remove(self):
        mem = create_empty_memory()
        mem["facts"] = [
            {"id": "f1", "content": "old", "category": "context", "confidence": 0.9},
            {"id": "f2", "content": "keep", "category": "context", "confidence": 0.9},
        ]
        update = {"user": {}, "history": {}, "newFacts": [], "factsToRemove": ["f1"]}
        result = MemoryUpdater()._apply_updates(mem, update)
        assert len(result["facts"]) == 1
        assert result["facts"][0]["id"] == "f2"

    def test_new_fact_carries_thread_id_source(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple(fact_confidence_threshold=0.0))
        mem = create_empty_memory()
        update = {"user": {}, "history": {}, "newFacts": [{"content": "x", "category": "context", "confidence": 0.9}], "factsToRemove": []}
        result = MemoryUpdater()._apply_updates(mem, update, thread_id="thread-42")
        assert result["facts"][0]["source"] == "thread-42"


class TestStripUploadMentions:
    def test_strips_upload_sentence_from_summary(self):
        mem = create_empty_memory()
        mem["user"]["workContext"]["summary"] = "User is an engineer. User uploaded file report.pdf. Likes Python."
        result = _strip_upload_mentions_from_memory(dict(mem))
        summary = result["user"]["workContext"]["summary"]
        assert "uploaded" not in summary.lower()
        assert "Likes Python" in summary

    def test_removes_upload_facts(self):
        mem = create_empty_memory()
        mem["facts"] = [
            {"id": "f1", "content": "User uploaded file data.csv", "category": "context", "confidence": 0.9},
            {"id": "f2", "content": "Prefers dark mode", "category": "preference", "confidence": 0.9},
        ]
        result = _strip_upload_mentions_from_memory(dict(mem))
        contents = [f["content"] for f in result["facts"]]
        assert "Prefers dark mode" in contents
        assert all("uploaded" not in c.lower() for c in contents)

    def test_preserves_csv_related_fact(self):
        # 收窄：不误删「works with CSV files」
        mem = create_empty_memory()
        mem["facts"] = [{"id": "f1", "content": "User works with CSV files daily", "category": "context", "confidence": 0.9}]
        result = _strip_upload_mentions_from_memory(dict(mem))
        assert len(result["facts"]) == 1  # 保留


class TestFactCRUD:
    def test_create_fact(self, home_env):
        mem = create_memory_fact("likes tea", category="preference", confidence=0.9, user_id="u1")
        assert len(mem["facts"]) == 1
        assert mem["facts"][0]["content"] == "likes tea"
        assert mem["facts"][0]["source"] == "manual"

    def test_create_fact_empty_content_raises(self, home_env):
        with pytest.raises(ValueError, match="content"):
            create_memory_fact("  ", user_id="u1")

    def test_create_fact_bad_confidence_raises(self, home_env):
        with pytest.raises(ValueError, match="confidence"):
            create_memory_fact("x", confidence=1.5, user_id="u1")

    def test_delete_fact(self, home_env):
        create_memory_fact("to delete", confidence=0.9, user_id="u1")
        mem = get_memory_data(user_id="u1")
        fact_id = mem["facts"][0]["id"]
        result = delete_memory_fact(fact_id, user_id="u1")
        assert result["facts"] == []

    def test_delete_fact_missing_raises(self, home_env):
        with pytest.raises(KeyError):
            delete_memory_fact("nonexistent", user_id="u1")

    def test_update_fact(self, home_env):
        create_memory_fact("original", confidence=0.9, user_id="u1")
        mem = get_memory_data(user_id="u1")
        fact_id = mem["facts"][0]["id"]
        result = update_memory_fact(fact_id, content="updated", confidence=0.95, user_id="u1")
        assert result["facts"][0]["content"] == "updated"
        assert result["facts"][0]["confidence"] == 0.95

    def test_update_fact_missing_raises(self, home_env):
        with pytest.raises(KeyError):
            update_memory_fact("nonexistent", content="x", user_id="u1")


class TestUpdaterFinalizeWithFakeModel:
    """用 fake model 测 _do_update_memory_sync 全链路（不碰真 LLM）。"""

    def test_full_update_roundtrip(self, home_env, monkeypatch):
        # 准备：LLM 返回一个合法更新 JSON
        llm_response = json.dumps(
            {
                "user": {"workContext": {"summary": "Data Scientist", "shouldUpdate": True}},
                "history": {},
                "newFacts": [{"content": "uses pandas", "category": "knowledge", "confidence": 0.9}],
                "factsToRemove": [],
            }
        )

        class _FakeResp:
            content = llm_response

        class _FakeModel:
            def invoke(self, prompt, config=None):
                return _FakeResp()

        updater = MemoryUpdater()
        monkeypatch.setattr(updater, "_get_model", lambda: _FakeModel())

        msgs = [_human("I work as a data scientist using pandas"), _ai("Great!")]
        ok = updater._do_update_memory_sync(messages=msgs, thread_id="t1", user_id="u1")
        assert ok is True

        # 验证写盘
        mem = get_memory_data(user_id="u1")
        assert mem["user"]["workContext"]["summary"] == "Data Scientist"
        assert any(f["content"] == "uses pandas" for f in mem["facts"])

    def test_disabled_returns_false(self, home_env, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple(enabled=False))
        updater = MemoryUpdater()
        ok = updater._do_update_memory_sync(messages=[_human("x")], user_id="u1")
        assert ok is False

    def test_empty_conversation_returns_false(self, home_env):
        updater = MemoryUpdater()
        ok = updater._do_update_memory_sync(messages=[_human("   ")], user_id="u1")
        assert ok is False  # format_conversation_for_update 剥光后为空

    def test_malformed_llm_response_returns_false(self, home_env, monkeypatch):
        class _FakeResp:
            content = "totally not json"

        class _FakeModel:
            def invoke(self, prompt, config=None):
                return _FakeResp()

        updater = MemoryUpdater()
        monkeypatch.setattr(updater, "_get_model", lambda: _FakeModel())
        ok = updater._do_update_memory_sync(messages=[_human("hi"), _ai("yo")], user_id="u1")
        assert ok is False  # JSONDecodeError 被吞


def _mem_config_simple(*, enabled=True, max_facts=100, fact_confidence_threshold=0.7, injection_enabled=True, model_name=None):
    """构造一个可调字段的 MemoryConfig 替身。"""
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.max_facts = max_facts
    cfg.fact_confidence_threshold = fact_confidence_threshold
    cfg.injection_enabled = injection_enabled
    cfg.model_name = model_name
    cfg.storage_path = ""
    cfg.storage_class = "deerflow.agents.memory.storage.FileMemoryStorage"
    cfg.debounce_seconds = 30
    cfg.max_injection_tokens = 2000
    cfg.token_counting = "char"
    return cfg


# ===========================================================================
# 5. queue
# ===========================================================================


class TestMemoryUpdateQueue:
    def test_add_increments_pending(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        assert q.pending_count == 0
        q.add("t1", [_human("hi"), _ai("yo")], user_id="u1")
        assert q.pending_count == 1

    def test_same_key_merges(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a")], user_id="u1")
        q.add("t1", [_human("b")], user_id="u1")  # 同 (t1, u1, None)
        assert q.pending_count == 1  # 合并
        # 合并后取最新消息
        assert q._queue[0].messages[0].content == "b"

    def test_different_keys_separate(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a")], user_id="u1")
        q.add("t1", [_human("b")], user_id="u2")  # 不同 user
        q.add("t2", [_human("c")], user_id="u1")  # 不同 thread
        assert q.pending_count == 3

    def test_correction_flag_merged_with_or(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a")], user_id="u1", correction_detected=False)
        q.add("t1", [_human("b")], user_id="u1", correction_detected=True)  # 合并取或
        assert q._queue[0].correction_detected is True

    def test_user_id_captured_in_context(self, monkeypatch):
        # user_id 必须存进 ConversationContext（跨 threading.Timer 边界）
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a"), _ai("b")], user_id="alice-123")
        assert q._queue[0].user_id == "alice-123"

    def test_add_nowait_schedules_immediate(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add_nowait("t1", [_human("a"), _ai("b")], user_id="u1")
        assert q.pending_count == 1

    def test_disabled_skips_add(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple(enabled=False))
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a")], user_id="u1")
        assert q.pending_count == 0

    def test_clear_cancels_timer(self, monkeypatch):
        _patch_mem_config(monkeypatch, _mem_config_simple())
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a")], user_id="u1")
        q.clear()
        assert q.pending_count == 0
        assert q._timer is None

    def test_flush_processes_queue(self, monkeypatch):
        # flush 应触发 _process_queue；用 fake updater 防真 LLM
        _patch_mem_config(monkeypatch, _mem_config_simple())
        calls = []

        class _FakeUpdater:
            def update_memory(self, **kwargs):
                calls.append(kwargs)
                return True

        monkeypatch.setattr(updater_module, "MemoryUpdater", _FakeUpdater)
        q = MemoryUpdateQueue()
        q.add("t1", [_human("a"), _ai("b")], user_id="u1")
        q.flush()
        assert len(calls) == 1
        assert q.pending_count == 0
        assert calls[0]["user_id"] == "u1"

    def test_get_memory_queue_singleton(self):
        q1 = get_memory_queue()
        q2 = get_memory_queue()
        assert q1 is q2


# ===========================================================================
# 6. MemoryMiddleware
# ===========================================================================


@pytest.fixture()
def _patched_get_config(monkeypatch):
    """patch get_config 避免测试里 RuntimeError（无 LangGraph 上下文）。"""
    from deerflow.agents.middlewares import memory_middleware as mm

    monkeypatch.setattr(mm, "get_config", lambda: {})
    return mm


class TestMemoryMiddleware:
    def test_disabled_skips(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple(enabled=False))
        mw = MemoryMiddleware()
        state = {"messages": [_human("hi"), _ai("yo")]}
        result = mw.after_agent(state, _fake_runtime({"thread_id": "t1"}))
        assert result is None
        assert get_memory_queue().pending_count == 0

    def test_no_thread_id_skips(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware()
        state = {"messages": [_human("hi"), _ai("yo")]}
        # runtime 无 thread_id，get_config 也返 {} → 跳过
        result = mw.after_agent(state, _fake_runtime({}))
        assert result is None
        assert get_memory_queue().pending_count == 0

    def test_no_messages_skips(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware()
        result = mw.after_agent({"messages": []}, _fake_runtime({"thread_id": "t1"}))
        assert result is None

    def test_no_assistant_response_skips(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware()
        # 只有 human，无 ai → 跳过
        result = mw.after_agent({"messages": [_human("hi")]}, _fake_runtime({"thread_id": "t1"}))
        assert result is None
        assert get_memory_queue().pending_count == 0

    def test_queues_and_captures_user_id(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware()
        state = {"messages": [_human("hello"), _ai("world")]}
        result = mw.after_agent(state, _fake_runtime({"thread_id": "t1"}))
        assert result is None
        q = get_memory_queue()
        assert q.pending_count == 1
        ctx = q._queue[0]
        assert ctx.thread_id == "t1"
        assert ctx.user_id is not None  # 捕获了 autouse 上下文的 user

    def test_agent_name_passed_through(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware(agent_name="reviewer")
        mw.after_agent({"messages": [_human("hi"), _ai("yo")]}, _fake_runtime({"thread_id": "t1"}))
        assert get_memory_queue()._queue[0].agent_name == "reviewer"

    def test_correction_detected(self, _patched_get_config, monkeypatch):
        from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

        _patch_mem_config(monkeypatch, _mem_config_simple())
        mw = MemoryMiddleware()
        mw.after_agent(
            {"messages": [_human("that's wrong"), _ai("sorry, fixed")]},
            _fake_runtime({"thread_id": "t1"}),
        )
        assert get_memory_queue()._queue[0].correction_detected is True


# ===========================================================================
# 7. DynamicContextMiddleware + _get_memory_context
# ===========================================================================


class TestGetMemoryContext:
    def test_disabled_returns_empty(self, monkeypatch):
        from deerflow.agents.lead_agent.prompt import _get_memory_context

        _patch_mem_config(monkeypatch, _mem_config_simple(enabled=False))
        assert _get_memory_context() == ""

    def test_injection_disabled_returns_empty(self, monkeypatch):
        from deerflow.agents.lead_agent.prompt import _get_memory_context

        _patch_mem_config(monkeypatch, _mem_config_simple(injection_enabled=False))
        assert _get_memory_context() == ""

    def test_empty_memory_returns_empty(self, home_env):
        from deerflow.agents.lead_agent.prompt import _get_memory_context

        # 无记忆数据 → format 返回 "" → 包不进 <memory>
        assert _get_memory_context() == ""

    def test_with_memory_returns_xml(self, home_env, monkeypatch):
        from deerflow.agents.lead_agent.prompt import _get_memory_context
        from deerflow.runtime.user_context import get_effective_user_id

        # char 模式避免 hermetic 测试触发 tiktoken BPE 下载
        _patch_mem_config(monkeypatch, _mem_config_simple())
        # 写一条 fact（用 _get_memory_context 会读的同一 user_id）
        uid = get_effective_user_id()
        create_memory_fact("likes python", category="preference", confidence=0.9, user_id=uid)
        out = _get_memory_context()
        assert "<memory>" in out
        assert "likes python" in out

    def test_exception_returns_empty(self, monkeypatch):
        # 让 get_memory_data 抛 → 吞掉返回 ""
        import deerflow.agents.memory.updater as up
        from deerflow.agents.lead_agent.prompt import _get_memory_context

        def boom(*a, **k):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(up, "get_memory_data", boom)
        assert _get_memory_context() == ""


class TestDynamicContextMiddleware:
    def test_first_turn_injects_reminder_with_id_swap(self, home_env):
        from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware

        mw = DynamicContextMiddleware(app_config=_mem_config_simple())
        original = _human("hello")
        original.id = "msg-1"
        state = {"messages": [original]}
        result = mw._inject(state)
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 2
        # reminder 复用原 ID（ID-swap，原地替换）
        assert msgs[0].id == "msg-1"
        assert "<system-reminder>" in msgs[0].content
        assert "<current_date>" in msgs[0].content
        # 原内容用派生 ID 紧随
        assert msgs[1].id == "msg-1__user"
        assert msgs[1].content == "hello"

    def test_same_day_no_op(self, home_env):
        from deerflow.agents.middlewares.dynamic_context_middleware import (
            _DYNAMIC_CONTEXT_REMINDER_KEY,
            DynamicContextMiddleware,
        )

        mw = DynamicContextMiddleware(app_config=_mem_config_simple())
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d, %A")
        # 已注入今天的提醒 → 同天，无操作
        existing = _human("next turn")
        existing.id = "msg-2"
        existing.additional_kwargs[_DYNAMIC_CONTEXT_REMINDER_KEY] = True
        existing.content = f"<system-reminder>\n<current_date>{today}</current_date>\n</system-reminder>"
        state = {"messages": [existing, _human("hello")]}
        assert mw._inject(state) is None

    def test_midnight_injection(self, home_env):
        from deerflow.agents.middlewares.dynamic_context_middleware import (
            _DYNAMIC_CONTEXT_REMINDER_KEY,
            DynamicContextMiddleware,
        )

        mw = DynamicContextMiddleware(app_config=_mem_config_simple())
        # 历史注入的是「昨天」
        old = _human("old turn")
        old.id = "msg-1"
        old.additional_kwargs[_DYNAMIC_CONTEXT_REMINDER_KEY] = True
        old.content = "<system-reminder>\n<current_date>2020-01-01, Wednesday</current_date>\n</system-reminder>"
        current = _human("new turn today")
        current.id = "msg-2"
        state = {"messages": [old, current]}
        result = mw._inject(state)
        assert result is not None
        # 给当前轮注入日期更新
        msgs = result["messages"]
        assert msgs[0].id == "msg-2"  # ID-swap 到当前轮
        assert "<current_date>" in msgs[0].content
        assert "2020-01-01" not in msgs[0].content  # 是今天，不是昨天

    def test_injection_enabled_gate(self, home_env):
        from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware

        # injection_enabled=False → 提醒里无 <memory> 段（仍有日期）
        mw = DynamicContextMiddleware(app_config=_mem_config_simple(injection_enabled=False))
        original = _human("hi")
        original.id = "m1"
        result = mw._inject({"messages": [original]})
        reminder = result["messages"][0].content
        assert "<memory>" not in reminder
        assert "<current_date>" in reminder

    def test_empty_messages_no_op(self, home_env):
        from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware

        mw = DynamicContextMiddleware(app_config=_mem_config_simple())
        assert mw._inject({"messages": []}) is None

    def test_is_dynamic_context_reminder(self):
        from deerflow.agents.middlewares.dynamic_context_middleware import (
            _DYNAMIC_CONTEXT_REMINDER_KEY,
            is_dynamic_context_reminder,
        )

        reminder = _human("x")
        reminder.additional_kwargs[_DYNAMIC_CONTEXT_REMINDER_KEY] = True
        assert is_dynamic_context_reminder(reminder) is True
        assert is_dynamic_context_reminder(_human("normal")) is False

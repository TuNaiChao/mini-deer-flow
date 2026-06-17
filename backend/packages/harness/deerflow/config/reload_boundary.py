"""配置热重载边界的单一真相源。

``get_app_config()`` 每次请求都重新解析 ``AppConfig``，所以**运行期可变**的字段
（models / memory / title / summarization / subagents / tools / 系统 prompt 等）
下一条消息就生效，无需重启。本模块登记的是另一类——**基础设施**字段：引擎、
单例、日志处理器等，它们在启动时被捕获一次，因此改它们需要**进程重启**。

登记项有两类：
- 顶层 ``AppConfig`` 字段（``database`` / ``checkpointer`` / ``run_events`` /
  ``stream_bridge`` / ``sandbox`` / ``log_level``）。:func:`format_field_description`
  为它们产出标准化的 ``"startup-only: ..."`` 前缀，写进对应 Pydantic
  ``Field(description=...)``，让边界在 IDE 悬浮提示里直接可见。
- mini 裁剪掉了 deer 的 ``channels`` / ``channel_connections``（IM 渠道，本期不做）。

未来任何「需要重启」的扫描器（运维工具、lint、文档生成器）都应以本登记表为准，
而非重新解析散文。
"""

from __future__ import annotations

from collections.abc import Iterator

#: 每个需要重启的字段描述开头的标准化前缀。
STARTUP_ONLY_PREFIX = "startup-only:"


#: 需重启字段路径 → 人话原因。原因文字会写进 ``Field(description=...)``，
#: 必须解释**哪段代码捕获了快照**（不只是「需重启」），让运维知道重启哪个子系统。
STARTUP_ONLY_FIELDS: dict[str, str] = {
    "database": "init_engine_from_config() 在启动时运行一次；SQLAlchemy 引擎持有连接池，不会因 config.yaml 编辑而重建。",
    "checkpointer": "make_checkpointer() 在启动时绑定持久化 checkpointer 一次，含 SQLite WAL / busy_timeout 设置。",
    "run_events": "make_run_event_store() 在启动时选定内存版 / SQL 版实现，并冻结到 app.state 与底层事件存储配对。",
    "stream_bridge": "make_stream_bridge() 在启动时构造流桥单例一次。",
    "sandbox": "get_sandbox_provider() 缓存 provider 单例；不同的 ``sandbox.use`` 类路径只在下次进程启动时生效。",
    "log_level": "apply_logging_level() 仅在启动时运行，设置 deerflow/app logger 级别并可能降低 root handler 阈值；重新加载的 AppConfig 不会再次触发它。",
}


def iter_startup_only_field_paths() -> Iterator[str]:
    """产出每个已登记的需重启字段路径。"""
    return iter(STARTUP_ONLY_FIELDS)


def is_startup_only_field(field_path: str) -> bool:
    """``field_path`` 是否登记为需重启。

    只接受顶层路径（``"database"`` / ``"sandbox"`` 等）；嵌套键如
    ``"database.url"`` 不建模，因为边界是按「节」而非「叶子」。
    """
    return field_path in STARTUP_ONLY_FIELDS


def format_field_description(field_path: str, *, field_doc: str | None = None) -> str:
    """为已登记字段构造标准化描述。

    用于 ``AppConfig`` 的 ``Field(description=...)``，让 IDE 悬浮文本与登记表一致，
    漂移测试能把两边互相对齐。

    Args:
        field_path: 已登记的顶层字段路径（如 ``"log_level"``）。
        field_doc: 字段本身的人话描述（允许值、语义等）。给出时，会以空行分隔
            接在 ``startup-only:`` 标记块之后，让 IDE 悬浮同时显示重启原因**和**
            字段常规文档。

    Raises:
        KeyError: ``field_path`` 未登记时。这是有意的——静默返回占位符会让笔误
            绕过漂移覆盖。
    """
    reason = STARTUP_ONLY_FIELDS[field_path]
    header = f"{STARTUP_ONLY_PREFIX} {reason}"
    if field_doc is None:
        return header
    return f"{header}\n\n{field_doc.strip()}"

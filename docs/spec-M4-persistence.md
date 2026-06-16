# M4 persistence — 完整签名级规格说明

> 本文是 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) 中 **M4 persistence** 的细化，精确到每个文件的类、方法签名（带类型）、ORM 列定义。后续 AI 可照此直接实现。
>
> **Phase**：1（与 checkpointer/events 同期；含 runs 基类层前置）
> **移植源**：`../../deer-flow/backend/packages/harness/deerflow/persistence/` + `runtime/runs/`
> **依赖**：config/database_config(M0)、utils/time(M1)、runtime/user_context(M3)
> **被依赖**：RunEventStore.db(M6)、RunManager/RunRepository(M18)、worker 的 thread_store(M18)

---

## 0. 文件总览（16 个文件）

```
runtime/runs/
  schemas.py              # RunStatus / DisconnectMode（Phase 1 前置）
  store/
    __init__.py           # 占位
    base.py               # RunStore ABC（Phase 1 前置，RunRepository 继承它）

persistence/
  __init__.py             # 导出 engine 函数
  base.py                 # Base(DeclarativeBase) + to_dict
  engine.py               # init_engine / get_session_factory / close_engine
  json_compat.py          # JsonMatch（方言可移植 JSON 匹配）
  models/
    __init__.py           # ORM 注册入口（mini 裁剪：仅 3 张表）
    run_event.py          # RunEventRow
  run/
    __init__.py           # 导出 RunRow / RunRepository
    model.py              # RunRow
    sql.py                # RunRepository(RunStore)
  thread_meta/
    __init__.py           # 导出 + make_thread_store 工厂
    base.py               # ThreadMetaStore ABC + InvalidMetadataFilterError
    model.py              # ThreadMetaRow
    memory.py             # MemoryThreadMetaStore（包 LangGraph BaseStore）
    sql.py                # ThreadMetaRepository(ThreadMetaStore)
```

---

## 1. `runtime/runs/schemas.py`（Phase 1 前置）

**职责**：run 生命周期枚举。

```python
from enum import StrEnum

class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"

class DisconnectMode(StrEnum):
    cancel = "cancel"
    continue_ = "continue"   # 注意别名 continue_（避免关键字冲突）
```

**要点**：`StrEnum` 使 `.value` 即字符串，`RunStatus("running")` 可反序列化。`cancel` 为默认断连行为。

---

## 2. `runtime/runs/store/base.py`（Phase 1 前置）— RunStore ABC

**职责**：run 元数据持久化的抽象接口（RunRepository 与 MemoryRunStore 都实现它）。

```python
import abc
from typing import Any

class RunStore(abc.ABC):

    @abc.abstractmethod
    async def put(self, run_id: str, *, thread_id: str,
                  assistant_id: str | None = None, user_id: str | None = None,
                  model_name: str | None = None, status: str = "pending",
                  multitask_strategy: str = "reject",
                  metadata: dict[str, Any] | None = None,
                  kwargs: dict[str, Any] | None = None,
                  error: str | None = None, created_at: str | None = None) -> None: ...

    @abc.abstractmethod
    async def get(self, run_id: str, *, user_id: str | None = None) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_by_thread(self, thread_id: str, *, user_id: str | None = None,
                             limit: int = 100) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def update_status(self, run_id: str, status: str, *,
                            error: str | None = None) -> bool | None: ...
        # 返回 False = 证明无行更新；None = 无法报告 rowcount（轻量实现）

    @abc.abstractmethod
    async def delete(self, run_id: str) -> None: ...

    @abc.abstractmethod
    async def update_model_name(self, run_id: str, model_name: str | None) -> None: ...

    @abc.abstractmethod
    async def update_run_completion(self, run_id: str, *, status: str,
                  total_input_tokens: int = 0, total_output_tokens: int = 0,
                  total_tokens: int = 0, llm_call_count: int = 0,
                  lead_agent_tokens: int = 0, subagent_tokens: int = 0,
                  middleware_tokens: int = 0, message_count: int = 0,
                  last_ai_message: str | None = None,
                  first_human_message: str | None = None,
                  error: str | None = None) -> bool | None: ...

    # 非抽象：默认 no-op，运行中快照（不改 status）
    async def update_run_progress(self, run_id: str, *,
                  total_input_tokens: int | None = None,
                  total_output_tokens: int | None = None,
                  total_tokens: int | None = None,
                  llm_call_count: int | None = None,
                  lead_agent_tokens: int | None = None,
                  subagent_tokens: int | None = None,
                  middleware_tokens: int | None = None,
                  message_count: int | None = None,
                  last_ai_message: str | None = None,
                  first_human_message: str | None = None) -> None:
        return None

    @abc.abstractmethod
    async def list_pending(self, *, before: str | None = None) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def list_inflight(self, *, before: str | None = None) -> list[dict[str, Any]]: ...
        # 返回 pending|running 的持久化行（启动恢复用）

    @abc.abstractmethod
    async def aggregate_tokens_by_thread(self, thread_id: str, *,
                  include_active: bool = False) -> dict[str, Any]: ...
        # 返回 {total_tokens, total_input_tokens, total_output_tokens, total_runs,
        #       by_model: {name: {tokens, runs}}, by_caller: {lead_agent, subagent, middleware}}
```

**要点**：`update_status`/`update_run_completion` 的 `bool|None` 返回是 RunManager row-recovery 的关键（红线 #12）。

---

## 3. `persistence/__init__.py`

```python
from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine

__all__ = ["close_engine", "get_engine", "get_session_factory", "init_engine"]
```

---

## 4. `persistence/base.py`

**职责**：所有 app ORM 模型的声明基类（checkpointer 表**不**归它管）。

```python
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    def to_dict(self, *, exclude: set[str] | None = None) -> dict:
        exclude = exclude or set()
        return {c.key: getattr(self, c.key)
                for c in sa_inspect(type(self)).mapper.column_attrs
                if c.key not in exclude}

    def __repr__(self) -> str:
        cols = ", ".join(f"{c.key}={getattr(self, c.key)!r}"
                         for c in sa_inspect(type(self)).mapper.column_attrs)
        return f"{type(self).__name__}({cols})"
```

**要点**：通用 `to_dict` 免得每个模型写序列化；`__repr__` 调试用。

---

## 5. `persistence/engine.py`

**职责**：async engine + session_factory 生命周期；memory 模式 no-op。

```python
# 模块级单例
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

async def init_engine(backend: str, *, url: str = "", echo: bool = False,
                      pool_size: int = 5, sqlite_dir: str = "") -> None: ...
async def init_engine_from_config(config: DatabaseConfig) -> None: ...
def get_session_factory() -> async_sessionmaker[AsyncSession] | None: ...   # memory 时返回 None
def get_engine() -> AsyncEngine | None: ...
async def close_engine() -> None: ...

# 内部
async def _auto_create_postgres_db(url: str) -> None: ...   # 连 postgres 库 AUTOCOMMIT 建 DB
def _json_serializer(obj) -> str: ...                        # ensure_ascii=False
```

**关键实现**：
- `backend == "memory"`：直接 return，`_engine`/`_session_factory` 保持 None。
- sqlite：`os.makedirs(sqlite_dir)`；`create_async_engine(url, json_serializer=_json_serializer)`；`@event.listens_for(_engine.sync_engine, "connect")` 内执行 `PRAGMA journal_mode=WAL` / `synchronous=NORMAL` / `foreign_keys=ON`。
- postgres：`create_async_engine(url, pool_size=, pool_pre_ping=True, json_serializer=)`；缺 `asyncpg` 抛可操作 ImportError。
- `_session_factory = async_sessionmaker(_engine, expire_on_commit=False)`。
- 自动建表：`import deerflow.persistence.models`（让 metadata 发现表）→ `async with _engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)`；postgres "does not exist" → `_auto_create_postgres_db` 后重建 engine 重试。

**依赖**：`sqlalchemy.ext.asyncio`、config/database_config、`persistence.base.Base`、`persistence.models`（注册）。
**可靠性**：WAL 并发读写（红线 #2）；asyncpg 缺包可操作提示（红线 #24）；json ensure_ascii=False（中文）。

---

## 6. `persistence/json_compat.py`

**职责**：跨方言（SQLite/PostgreSQL）的 JSON 列 `column[key] == value` 匹配，供 thread_meta.search 的 metadata 过滤用。

```python
_KEY_CHARSET_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
ALLOWED_FILTER_VALUE_TYPES: tuple[type, ...] = (type(None), bool, int, float, str)

def validate_metadata_filter_key(key: object) -> bool: ...    # [A-Za-z0-9_-]+
def validate_metadata_filter_value(value: object) -> bool: ... # 类型 + int64 范围

class JsonMatch(ColumnElement):     # inherit_cache=True, type=Boolean()
    column: ColumnElement
    key: str
    value: object
    def __init__(self, column, key, value) -> None: ...   # 校验 key/value

def json_match(column: ColumnElement, key: str, value: object) -> JsonMatch: ...

# @compiles(JsonMatch, "sqlite") → json_type/json_extract
# @compiles(JsonMatch, "postgresql") → json_typeof/->>
# @compiles(JsonMatch) → NotImplementedError
```

**要点**：key 字符集严格限制防 JSONPath/SQL 注入；bool 校验先于 int（Python bool 是 int 子类）；int 限 signed-64（防 SQLite 溢出）。
**裁剪**：若 mini 不做 metadata 过滤搜索，可只保留 `validate_*` + 一个简化 `json_match`，但建议完整移植（search 会用到）。

---

## 7. `persistence/models/__init__.py`（mini 裁剪版）

**职责**：ORM 注册入口，让 `Base.metadata.create_all` 发现所有表。

```python
# mini 版（裁掉 feedback/user/channel_connections）：
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

__all__ = ["RunEventRow", "RunRow", "ThreadMetaRow"]
```

> ⚠️ deer 原版还 import feedback/user/channel_connections —— **mini 不做这些**，必须裁剪，否则 import 报错。

---

## 8. `persistence/models/run_event.py` — RunEventRow

```python
from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from deerflow.persistence.base import Base

class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str]     = mapped_column(String(64), nullable=False)
    run_id: Mapped[str]        = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str]    = mapped_column(String(32), nullable=False)
    category: Mapped[str]      = mapped_column(String(16), nullable=False)   # message|trace|lifecycle
    content: Mapped[str]       = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int]           = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
```

**要点**：`(thread_id, seq)` 唯一约束是 db seq 单调的物理保障（红线 #3）；`ix_events_thread_cat_seq` 加速 `list_messages`；`user_id` nullable 兼容旧数据。

---

## 9. `persistence/run/__init__.py`

```python
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.run.sql import RunRepository

__all__ = ["RunRepository", "RunRow"]
```

---

## 10. `persistence/run/model.py` — RunRow

```python
class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str]          = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str]       = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None]  = mapped_column(String(64), index=True)
    status: Mapped[str]          = mapped_column(String(20), default="pending")
        # pending|running|success|error|timeout|interrupted
    model_name: Mapped[str | None] = mapped_column(String(128))
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="reject")
    metadata_json: Mapped[dict]  = mapped_column(JSON, default=dict)
    kwargs_json: Mapped[dict]    = mapped_column(JSON, default=dict)
    error: Mapped[str | None]    = mapped_column(Text)

    # 便利字段（列表页免查 RunEventStore）
    message_count: Mapped[int]       = mapped_column(default=0)
    first_human_message: Mapped[str | None] = mapped_column(Text)
    last_ai_message: Mapped[str | None]     = mapped_column(Text)

    # token 用量（RunJournal 累加，完成时写入）
    total_input_tokens: Mapped[int]  = mapped_column(default=0)
    total_output_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int]        = mapped_column(default=0)
    llm_call_count: Mapped[int]      = mapped_column(default=0)
    lead_agent_tokens: Mapped[int]   = mapped_column(default=0)
    subagent_tokens: Mapped[int]     = mapped_column(default=0)
    middleware_tokens: Mapped[int]   = mapped_column(default=0)

    follow_up_to_run_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=lambda: datetime.now(UTC),
                                                  onupdate=lambda: datetime.now(UTC))

    __table_args__ = (Index("ix_runs_thread_status", "thread_id", "status"),)
```

**要点**：`metadata_json`/`kwargs_json` 在 `_row_to_dict` 时 remap 为 `metadata`/`kwargs` 以匹配 RunStore 接口；token 桶与 RunJournal `get_completion_data()` 一一对应。

---

## 11. `persistence/run/sql.py` — RunRepository(RunStore)

**职责**：SQL 实现；每方法独立短 session（不跨长 run 持有连接）。

```python
class RunRepository(RunStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str | None: ...  # strip + 截 128
    @staticmethod
    def _safe_json(obj: Any) -> Any: ...        # 递归保 JSON 可序列化（model_dump/dict/str 兜底）
    @staticmethod
    def _row_to_dict(row: RunRow) -> dict[str, Any]: ...  # metadata_json→metadata, kwargs_json→kwargs, datetime→coerce_iso

    async def put(self, run_id, *, thread_id, assistant_id=None,
                  user_id=AUTO, model_name=None, status="pending",
                  multitask_strategy="reject", metadata=None, kwargs=None,
                  error=None, created_at=None, follow_up_to_run_id=None) -> None: ...
        # 幂等 upsert：先 get，存在则更新，不存在则 insert。重试不会变主键冲突。

    async def get(self, run_id, *, user_id=AUTO) -> dict | None: ...
    async def list_by_thread(self, thread_id, *, user_id=AUTO, limit=100) -> list[dict]: ...
    async def update_status(self, run_id, status, *, error=None) -> bool: ...   # 返回 rowcount != 0
    async def update_model_name(self, run_id, model_name) -> None: ...
    async def delete(self, run_id, *, user_id=AUTO) -> None: ...
    async def list_pending(self, *, before=None) -> list[dict]: ...
    async def list_inflight(self, *, before=None) -> list[dict]: ...
    async def update_run_completion(self, run_id, *, status, ...全部 token 桶...) -> bool: ...  # rowcount
    async def update_run_progress(self, run_id, *, ...可选 token 桶...) -> None: ...
        # WHERE status='running'，只在运行中更新
    async def aggregate_tokens_by_thread(self, thread_id, *, include_active=False) -> dict: ...
        # 单条 GROUP BY model_name
```

**要点**：所有写方法 `user_id` 默认 `AUTO`（三态解析，红线 #10 UUID→str 边界由 user_context 处理）；`put` 幂等是 RunManager busy 重试的前提；`datetime` 经 `coerce_iso` 归一为 UTC ISO。
**依赖**：runtime/user_context（`AUTO`/`resolve_user_id`）、utils/time（`coerce_iso`）、persistence/run/model.RunRow、runtime/runs/store.base.RunStore。

---

## 12. `persistence/thread_meta/__init__.py`

```python
from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

__all__ = ["InvalidMetadataFilterError", "MemoryThreadMetaStore", "ThreadMetaRepository",
           "ThreadMetaRow", "ThreadMetaStore", "make_thread_store"]

def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession] | None,
    store: BaseStore | None = None,
) -> ThreadMetaStore:
    """session_factory 非 None → ThreadMetaRepository；否则用 LangGraph BaseStore → MemoryThreadMetaStore。"""
```

**要点**：这是 D.5 `build_thread_store` 的核心；memory 模式需要传入一个 LangGraph `InMemoryStore`（来自 M19 runtime/store）。

---

## 13. `persistence/thread_meta/base.py` — ThreadMetaStore ABC

```python
class InvalidMetadataFilterError(ValueError): ...

class ThreadMetaStore(abc.ABC):
    @abc.abstractmethod
    async def create(self, thread_id: str, *, assistant_id: str | None = None,
                     user_id: str | None | _AutoSentinel = AUTO,
                     display_name: str | None = None,
                     metadata: dict | None = None) -> dict: ...
    @abc.abstractmethod
    async def get(self, thread_id: str, *, user_id=AUTO) -> dict | None: ...
    @abc.abstractmethod
    async def search(self, *, metadata: dict[str, Any] | None = None,
                     status: str | None = None, limit: int = 100, offset: int = 0,
                     user_id=AUTO) -> list[dict[str, Any]]: ...
    @abc.abstractmethod
    async def update_display_name(self, thread_id: str, display_name: str, *, user_id=AUTO) -> None: ...
    @abc.abstractmethod
    async def update_status(self, thread_id: str, status: str, *, user_id=AUTO) -> None: ...
    @abc.abstractmethod
    async def update_metadata(self, thread_id: str, metadata: dict, *, user_id=AUTO) -> None: ...
        # 合并进 metadata_json（read-modify-write 单事务）
    @abc.abstractmethod
    async def update_owner(self, thread_id: str, owner_user_id: str, *, user_id=AUTO) -> None: ...
        # 迁移/修复用
    @abc.abstractmethod
    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool: ...
        # require_existing=False 宽松（读：行缺失/共享/同主都 True）
        # require_existing=True 严格（写/删：必须存在且同主或共享）
    @abc.abstractmethod
    async def delete(self, thread_id: str, *, user_id=AUTO) -> None: ...
```

**要点**：`check_access` 双语义防「删除后跨用户再操作」漏洞；`AUTO` 三态贯穿。

---

## 14. `persistence/thread_meta/model.py` — ThreadMetaRow

```python
class ThreadMetaRow(Base):
    __tablename__ = "threads_meta"

    thread_id: Mapped[str]       = mapped_column(String(64), primary_key=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None]  = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str]          = mapped_column(String(20), default="idle")
    metadata_json: Mapped[dict]  = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=lambda: datetime.now(UTC),
                                                  onupdate=lambda: datetime.now(UTC))
```

---

## 15. `persistence/thread_meta/memory.py` — MemoryThreadMetaStore

**职责**：memory 模式实现，包 LangGraph `BaseStore`（命名空间 `("threads",)`）。

```python
THREADS_NS: tuple[str, ...] = ("threads",)

class MemoryThreadMetaStore(ThreadMetaStore):
    def __init__(self, store: BaseStore) -> None: ...

    async def _get_owned_record(self, thread_id, user_id, method_name) -> dict | None: ...
        # aget → 校验 owner → 返回可变副本
    async def create(self, thread_id, *, assistant_id=None, user_id=AUTO,
                     display_name=None, metadata=None) -> dict: ...
    async def get(self, thread_id, *, user_id=AUTO) -> dict | None: ...
    async def search(self, *, metadata=None, status=None, limit=100, offset=0, user_id=AUTO) -> list[dict]: ...
    async def check_access(self, thread_id, user_id, *, require_existing=False) -> bool: ...
    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None: ...
    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None: ...
    async def update_metadata(self, thread_id, metadata, *, user_id=AUTO) -> None: ...
    async def update_owner(self, thread_id, owner_user_id, *, user_id=AUTO) -> None: ...
    async def delete(self, thread_id, *, user_id=AUTO) -> None: ...

    @staticmethod
    def _item_to_dict(item) -> dict[str, Any]: ...   # created_at/updated_at 经 coerce_iso 修复旧值
```

**要点**：record 结构 `{thread_id, assistant_id, user_id, display_name, status, metadata, values, created_at, updated_at}`；`coerce_iso` 修复历史 `str(time.time())` 旧值。
**依赖**：`langgraph.store.base.BaseStore`（M19 提供 InMemoryStore）、utils/time、runtime/user_context。

---

## 16. `persistence/thread_meta/sql.py` — ThreadMetaRepository

**职责**：SQL 实现，实现 ThreadMetaStore 全部方法。

```python
class ThreadMetaRepository(ThreadMetaStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    @staticmethod
    def _row_to_dict(row: ThreadMetaRow) -> dict[str, Any]: ...  # metadata_json→metadata, datetime→coerce_iso

    async def create(self, thread_id, *, assistant_id=None, user_id=AUTO,
                     display_name=None, metadata=None) -> dict: ...
    async def get(self, thread_id, *, user_id=AUTO) -> dict | None: ...
    async def check_access(self, thread_id, user_id, *, require_existing=False) -> bool: ...
    async def search(self, *, metadata=None, status=None, limit=100, offset=0, user_id=AUTO) -> list[dict]: ...
        # metadata 用 json_match；全部 key 被拒 → raise InvalidMetadataFilterError
    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None: ...
    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None: ...
    async def update_metadata(self, thread_id, metadata, *, user_id=AUTO) -> None: ...
        # 单 session read-modify-write
    async def update_owner(self, thread_id, owner_user_id, *, user_id=AUTO) -> None: ...
    async def delete(self, thread_id, *, user_id=AUTO) -> None: ...

    async def _check_ownership(self, session, thread_id, resolved_user_id) -> bool: ...
```

**依赖**：persistence/json_compat（`json_match`）、persistence/thread_meta/model、runtime/user_context、utils/time。

---

## 接入点（lifespan / D.5 工厂）

```python
# D.5 build_thread_store 等价于 make_thread_store
await init_engine_from_config(cfg.database)              # memory → no-op, sf=None
sf = get_session_factory()
# memory 模式需要一个 InMemoryStore（来自 M19）：
langgraph_store = InMemoryStore() if sf is None else None
thread_store = make_thread_store(sf, store=langgraph_store)   # sf 有→SQL；否则→Memory(BaseStore)
run_store = RunRepository(sf) if sf else MemoryRunStore()      # M18
```

---

## mini 适配清单（与 deer 的差异）

1. **models/__init__.py 裁剪**：只注册 `RunEventRow`/`RunRow`/`ThreadMetaRow`（删 feedback/user/channel_connections）。
2. **路径**：deer 用 `config/runtime_paths`；mini 一律用 `config/paths.resolve_path`（M0）。
3. **DatabaseConfig**：用 M0 的 `database_config.py`（`app_sqlalchemy_url`/`sqlite_dir`/`sqlite_path`）。
4. **user_context**：用 M3 的 `AUTO`/`resolve_user_id`/`get_current_user`（mini 无鉴权时 `get_current_user()` 返回 None，user_id stamp 为 None，列允许 nullable）。
5. **utils/time**：用 M1 的 `now_iso`/`coerce_iso`。
6. **memory ThreadMetaStore** 依赖 LangGraph `InMemoryStore`（M19）—— 若 M19 未做，可临时用一个最小 dict 实现 BaseStore 接口，但建议先做 M19 的 memory 版。

---

## 测试（`backend/test/test_persistence.py`）

| 用例 | 验证点 |
|------|--------|
| `test_engine_memory_noop` | backend=memory → `get_session_factory()` 返回 None，不建 engine |
| `test_engine_sqlite_wal` | sqlite → 连接级 `PRAGMA journal_mode=WAL` 生效 |
| `test_engine_auto_create_dir` | `sqlite_dir` 不存在 → 自动创建 |
| `test_engine_postgres_missing_asyncpg` | postgres + 无 asyncpg → 可操作 ImportError |
| `test_run_event_row_crud` | RunEventRow 增查；`(thread_id, seq)` 唯一约束触发 |
| `test_run_row_crud` | RunRow put(幂等 upsert)/get/list_by_thread/update_status(rowcount)/update_run_completion |
| `test_run_repository_user_filter` | user_id 三态：AUTO(None→不过滤)/显式/None |
| `test_run_aggregate_tokens` | aggregate_tokens_by_thread 的 by_model/by_caller 聚合 |
| `test_thread_meta_crud` | create/get/update_display_name/update_status/update_metadata(合并)/delete |
| `test_thread_meta_check_access` | require_existing True/False 双语义、共享行(user_id=None)、跨用户拒绝 |
| `test_thread_meta_search_metadata` | json_match 命中/未命中、非法 key 抛 InvalidMetadataFilterError |
| `test_json_match_dialects` | SQLite/PG 编译产物含正确函数；非法 key/value 拒绝；bool≠int；int64 溢出拒绝 |
| `test_uuid_to_str_boundary` | user_id 为 UUID 对象时 str() 后入库（防 aiosqlite 静默回滚） |
| `test_runstore_abc_contract` | RunRepository/MemoryRunStore 都满足 RunStore 抽象方法 |
| `test_coerce_iso` | naive datetime / 旧 str(time.time()) → UTC ISO |

---

## 学习文档（`docs/persistence.md`）大纲

1. 一句话定位：app 数据的 SQLAlchemy 异步 ORM 层（区别于 checkpointer）。
2. 为什么需要：run 历史/线程归属/token 统计要跨重启存活；memory 模式降级。
3. 核心概念：DeclarativeBase、async sessionmaker、WAL、三态 user_id。
4. 设计原理：为什么 checkpointer 表与 app 表物理分离（红线 #—）；为什么 update_status 返回 bool（#12 row recovery）；为什么 `(thread_id, seq)` 唯一（#3）；为什么 models/__init__ 要 import 全部模型（metadata 发现）。
5. 文件结构：16 文件职责表。
6. 关键接口：engine、RunStore、ThreadMetaStore、make_thread_store。
7. 应用方法：config.yaml 配 sqlite、lifespan init/close、内存降级。
8. 模块关系：被 M6(DbRunEventStore)/M18(RunManager/worker) 依赖；依赖 M0/M1/M3/M19。
9. 排错：database is locked（→WAL+busy 重试）、aiosqlite UUID 报错（→str）、表没建（→models 注册）。

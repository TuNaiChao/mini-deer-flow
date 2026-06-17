"""SQLAlchemy 的方言感知 JSON 值匹配（SQLite + PostgreSQL）。

提供 ``json_match(column, key, value)``：在 JSON 列上做 ``column[key] == value``
的、可跨方言（sqlite / postgresql）移植的谓词。用于线程元数据的 metadata 过滤
（``ThreadMetaRepository.search``）。

为什么需要单独写：

- SQLite 与 PostgreSQL 的 JSON 查询语法完全不同（SQLite 用
  ``json_type`` / ``json_extract``，PostgreSQL 用 ``json_typeof`` / ``->>``）。
- 直接 ``column["key"].as_string() == value`` 无法区分 ``bool`` 与 ``int``、
  ``NULL`` 与「键不存在」，会得到错误匹配。
- 本模块对每个值类型（None / bool / int / float / str）编译出类型安全的比较，
  并把 key 限制为 ``[A-Za-z0-9_-]+``（key 会被插值进编译出的 SQL，放宽字符集
  会打开 SQL/JSONPath 注入面）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import BigInteger, Float, String, bindparam
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import Boolean, TypeEngine

# Key 会被插值进编译出的 SQL；限制字符集以防注入。
_KEY_CHARSET_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# metadata 过滤值允许的类型（与 JsonMatch 接受的集合一致）。
ALLOWED_FILTER_VALUE_TYPES: tuple[type, ...] = (type(None), bool, int, float, str)

# SQLite 在绑定超出有符号 64 位范围的值时会溢出；PostgreSQL 在 BIGINT cast 时溢出。
# 在校验阶段就拒绝，而不是等到运行时。
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def validate_metadata_filter_key(key: object) -> bool:
    """``key`` 是否可作为 JSON metadata 过滤的键。

    当 ``key`` 是匹配 ``[A-Za-z0-9_-]+`` 的字符串时为 True。限制字符集是因为
    key 会被插值进编译出的 SQL 路径表达式（``$."<key>"`` / ``->`` 字面量），
    任何更宽松的模式都会打开 SQL/JSONPath 注入面。
    """
    return isinstance(key, str) and bool(_KEY_CHARSET_RE.match(key))


def validate_metadata_filter_value(value: object) -> bool:
    """``value`` 是否是允许的 JSON metadata 过滤值类型。

    与 ``_build_clause`` 知道如何编译成跨方言谓词的类型集合一致。其它类型
    （list/dict/bytes/...）被有意拒绝，而不是静默用 ``str()`` 强转——静默强转会
    （a）产生错误匹配，且（b）当 ``value`` 不可哈希时破坏 SQLAlchemy 的
    ``inherit_cache`` 不变量。

    int 值额外限制在有符号 64 位范围 ``[-2**63, 2**63 - 1]`` 内：SQLite 在绑定
    更大的值时溢出，PostgreSQL 在 ``BIGINT`` cast 时溢出。
    """
    if not isinstance(value, ALLOWED_FILTER_VALUE_TYPES):
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        if not (_INT64_MIN <= value <= _INT64_MAX):
            return False
    return True


class JsonMatch(ColumnElement):
    """跨方言的 ``column[key] == value``（JSON 列）。

    在 SQLite 编译成 ``json_type``/``json_extract``，在 PostgreSQL 编译成
    ``json_typeof``/``->>``，做类型安全的比较，区分 bool vs int、NULL vs 缺键。

    ``key`` 必须是匹配 ``[A-Za-z0-9_-]+`` 的单个字面量键。
    ``value`` 必须是：``None`` / ``bool`` / ``int``（有符号 64 位）/ ``float`` / ``str``。
    """

    inherit_cache = True
    type = Boolean()
    _is_implicitly_boolean = True

    _traverse_internals = [
        ("column", InternalTraversal.dp_clauseelement),
        ("key", InternalTraversal.dp_string),
        ("value", InternalTraversal.dp_plain_obj),
    ]

    def __init__(self, column: ColumnElement, key: str, value: object) -> None:
        if not validate_metadata_filter_key(key):
            raise ValueError(f"JsonMatch key must match {_KEY_CHARSET_RE.pattern!r}; got: {key!r}")
        if not validate_metadata_filter_value(value):
            if isinstance(value, int) and not isinstance(value, bool):
                raise TypeError(f"JsonMatch int value out of signed 64-bit range [-2**63, 2**63-1]: {value!r}")
            raise TypeError(f"JsonMatch value must be None, bool, int, float, or str; got: {type(value).__name__!r}")
        self.column = column
        self.key = key
        self.value = value
        super().__init__()


@dataclass(frozen=True)
class _Dialect:
    """编译 JSON 类型/值比较时，每个方言用到的名称。"""

    null_type: str
    num_types: tuple[str, ...]
    num_cast: str
    int_types: tuple[str, ...]
    int_cast: str
    # SQLite 为 None（json_type 已区分 'integer'/'real'）；PostgreSQL 为正则字面量
    # （json_typeof 对 int/float 都返回 'number'，需额外 guard 防 float 上的 CAST 报错）。
    int_guard: str | None
    string_type: str
    bool_type: str | None


_SQLITE = _Dialect(
    null_type="null",
    num_types=("integer", "real"),
    num_cast="REAL",
    int_types=("integer",),
    int_cast="INTEGER",
    int_guard=None,
    string_type="text",
    bool_type=None,
)

_PG = _Dialect(
    null_type="null",
    num_types=("number",),
    num_cast="DOUBLE PRECISION",
    int_types=("number",),
    int_cast="BIGINT",
    int_guard="'^-?[0-9]+$'",
    string_type="string",
    bool_type="boolean",
)


def _bind(compiler: SQLCompiler, value: object, sa_type: TypeEngine[Any], **kw: Any) -> str:
    param = bindparam(None, value, type_=sa_type)
    return compiler.process(param, **kw)


def _type_check(typeof: str, types: tuple[str, ...]) -> str:
    if len(types) == 1:
        return f"{typeof} = '{types[0]}'"
    quoted = ", ".join(f"'{t}'" for t in types)
    return f"{typeof} IN ({quoted})"


def _build_clause(compiler: SQLCompiler, typeof: str, extract: str, value: object, dialect: _Dialect, **kw: Any) -> str:
    if value is None:
        return f"{typeof} = '{dialect.null_type}'"
    if isinstance(value, bool):
        # bool 检查必须在 int 检查之前——Python 中 bool 是 int 的子类。
        bool_str = "true" if value else "false"
        if dialect.bool_type is None:
            return f"{typeof} = '{bool_str}'"
        return f"({typeof} = '{dialect.bool_type}' AND {extract} = '{bool_str}')"
    if isinstance(value, int):
        bp = _bind(compiler, value, BigInteger(), **kw)
        if dialect.int_guard:
            # CASE 防止 json_typeof = 'number' 同时匹配 float 时的 CAST 报错
            return f"(CASE WHEN {_type_check(typeof, dialect.int_types)} AND {extract} ~ {dialect.int_guard} THEN CAST({extract} AS {dialect.int_cast}) END = {bp})"
        return f"({_type_check(typeof, dialect.int_types)} AND CAST({extract} AS {dialect.int_cast}) = {bp})"
    if isinstance(value, float):
        bp = _bind(compiler, value, Float(), **kw)
        return f"({_type_check(typeof, dialect.num_types)} AND CAST({extract} AS {dialect.num_cast}) = {bp})"
    bp = _bind(compiler, str(value), String(), **kw)
    return f"({typeof} = '{dialect.string_type}' AND {extract} = {bp})"


@compiles(JsonMatch, "sqlite")
def _compile_sqlite(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    if not validate_metadata_filter_key(element.key):
        raise ValueError(f"Key escaped validation: {element.key!r}")
    col = compiler.process(element.column, **kw)
    path = f'$."{element.key}"'
    typeof = f"json_type({col}, '{path}')"
    extract = f"json_extract({col}, '{path}')"
    return _build_clause(compiler, typeof, extract, element.value, _SQLITE, **kw)


@compiles(JsonMatch, "postgresql")
def _compile_pg(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    if not validate_metadata_filter_key(element.key):
        raise ValueError(f"Key escaped validation: {element.key!r}")
    col = compiler.process(element.column, **kw)
    typeof = f"json_typeof({col} -> '{element.key}')"
    extract = f"({col} ->> '{element.key}')"
    return _build_clause(compiler, typeof, extract, element.value, _PG, **kw)


@compiles(JsonMatch)
def _compile_default(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    raise NotImplementedError(f"JsonMatch supports only sqlite and postgresql; got dialect: {compiler.dialect.name}")


def json_match(column: ColumnElement, key: str, value: object) -> JsonMatch:
    """构造一个跨方言的 ``column[key] == value`` 谓词。"""
    return JsonMatch(column, key, value)

# backend 构建与开发命令（对齐 deer-flow backend/Makefile 命名）。
# 所有命令在 backend/ 目录下执行：`cd backend && make <target>`。
#
# PYTHONPATH=packages/harness：harness 包源码在 packages/harness/deerflow/。
# 该环境（Python 3.14 + uv）下 editable install 的 .pth 在 `uv run` 重新 sync 后
# 会回退到不可用态（`import deerflow` 失败），故显式把源码目录加到 PYTHONPATH，
# 让 `make dev` / `make test` 稳定可跑（不依赖 editable install 是否生效）。
#
# ⚠ 若项目落在 iCloud 同步目录（如 ~/Documents，本机即是），venv 的海量小文件会被
# macOS 同步/eviction，导致随机 ``ModuleNotFoundError``（uvicorn.protocols.http /
# typing_extensions.py 被清掉等）。把 venv 挪到非同步路径即可根治：
#     export UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow
# （一次配进 shell profile；之后 ``make dev`` / ``make test`` 自动用它。）

.PHONY: install dev test test-blocking-io lint format

install:
	uv sync

# 本地开发：启动 langgraph dev server（读取 langgraph.json）
dev:
	PYTHONPATH=packages/harness PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run langgraph dev

# 全量测试（含 boundary / 各模块单测）。test/ 已移到 backend 外（mini-deer-flow/test）。
test:
	PYTHONPATH=packages/harness PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest ../test -v

# 仅跑严格 blocking-IO gate（../test/blocking_io/）
test-blocking-io:
	PYTHONPATH=packages/harness PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest ../test/blocking_io -q --tb=short

# lint/format 覆盖项目根（backend/ + test/），配置在根 ruff.toml
lint:
	uv run ruff check ..
	uv run ruff format --check ..

format:
	uv run ruff check .. --fix && uv run ruff format ..

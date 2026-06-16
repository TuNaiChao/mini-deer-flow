# backend 构建与开发命令（对齐 deer-flow backend/Makefile 命名）。
# 所有命令在 backend/ 目录下执行：`cd backend && make <target>`。

.PHONY: install dev test test-blocking-io lint format

install:
	uv sync

# 本地开发：启动 langgraph dev server（读取 langgraph.json）
dev:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run langgraph dev

# 全量测试（含 boundary / 各模块单测）。test/ 已移到 backend 外（mini-deer-flow/test）。
test:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest ../test -v

# 仅跑严格 blocking-IO gate（../test/blocking_io/）
test-blocking-io:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest ../test/blocking_io -q --tb=short

# lint/format 覆盖项目根（backend/ + test/），配置在根 ruff.toml
lint:
	uv run ruff check ..
	uv run ruff format --check ..

format:
	uv run ruff check .. --fix && uv run ruff format ..

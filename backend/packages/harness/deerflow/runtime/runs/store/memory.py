"""内存版 RunStore——``database.backend=memory``（默认）和测试用。

等价于早期 ``RunManager._runs`` 字典的行为：所有 run 元数据存在进程内存的 dict 里，进程
重启即丢。开发 / 单进程 / 测试场景够用；多 worker / 要持久历史的场景用 SQL 实现
（``deerflow.persistence.run.sql.RunRepository``）。

``user_id`` 过滤：``user_id=None`` 不加过滤（单用户 / 迁移）；给了就只返回该用户的 run
（用户隔离）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deerflow.runtime.runs.store.base import RunStore


class MemoryRunStore(RunStore):
    """内存字典实现的 RunStore。"""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        # 二级索引：thread_id -> 插入序 run_id 集合（dict 当有序集合用），与 ``_runs`` 同步维护，
        # 让 per-thread 查询不必 O(全部内存 run) 全扫。镜像 ``RunManager`` 在它自己的内存 record
        # 上维护的同款索引（#3562）。
        self._runs_by_thread: dict[str, dict[str, None]] = {}

    def _index_run(self, run_id: str, thread_id: str) -> None:
        """把 *run_id* 登记到 *thread_id* 的二级索引桶里。"""
        self._runs_by_thread.setdefault(thread_id, {})[run_id] = None

    def _unindex_run(self, run_id: str, thread_id: str) -> None:
        """从 *thread_id* 桶移除 *run_id*，桶空了就摘掉键。"""
        bucket = self._runs_by_thread.get(thread_id)
        if bucket is not None:
            bucket.pop(run_id, None)
            if not bucket:
                self._runs_by_thread.pop(thread_id, None)

    async def put(
        self,
        run_id,
        *,
        thread_id,
        assistant_id=None,
        user_id=None,
        model_name=None,
        status="pending",
        multitask_strategy="reject",
        metadata=None,
        kwargs=None,
        error=None,
        created_at=None,
    ):
        now = datetime.now(UTC).isoformat()
        self._runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "user_id": user_id,
            "model_name": model_name,
            "status": status,
            "multitask_strategy": multitask_strategy,
            "metadata": metadata or {},
            "kwargs": kwargs or {},
            "error": error,
            "created_at": created_at or now,
            "updated_at": now,
        }
        self._index_run(run_id, thread_id)

    async def get(self, run_id, *, user_id=None):
        run = self._runs.get(run_id)
        if run is None:
            return None
        if user_id is not None and run.get("user_id") != user_id:
            return None
        return run

    async def list_by_thread(self, thread_id, *, user_id=None, limit=100):
        # 用 thread 索引做 O(该 thread 的 run 数) 查找，而非扫每个 run（#3562）。
        # ``self._runs.get`` 是纵深防御：丢弃索引里还在、但 ``_runs`` 已没有的陈旧 id。
        run_ids = self._runs_by_thread.get(thread_id)
        if not run_ids:
            return []
        results = [run for run_id in run_ids if (run := self._runs.get(run_id)) is not None and (user_id is None or run.get("user_id") == user_id)]
        results.sort(key=lambda r: r["created_at"], reverse=True)
        return results[:limit]

    async def update_status(self, run_id, status, *, error=None):
        if run_id in self._runs:
            self._runs[run_id]["status"] = status
            if error is not None:
                self._runs[run_id]["error"] = error
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()
            return True
        return False

    async def update_model_name(self, run_id, model_name):
        if run_id in self._runs:
            self._runs[run_id]["model_name"] = model_name
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()

    async def delete(self, run_id):
        run = self._runs.pop(run_id, None)
        if run is not None:
            self._unindex_run(run_id, run["thread_id"])

    async def update_run_completion(self, run_id, *, status, **kwargs):
        if run_id in self._runs:
            self._runs[run_id]["status"] = status
            for key, value in kwargs.items():
                if value is not None:
                    self._runs[run_id][key] = value
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()
            return True
        return False

    async def update_run_progress(self, run_id, **kwargs):
        if run_id in self._runs and self._runs[run_id].get("status") == "running":
            for key, value in kwargs.items():
                if value is not None:
                    self._runs[run_id][key] = value
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()

    async def list_pending(self, *, before=None):
        now = before or datetime.now(UTC).isoformat()
        results = [r for r in self._runs.values() if r["status"] == "pending" and r["created_at"] <= now]
        results.sort(key=lambda r: r["created_at"])
        return results

    async def list_inflight(self, *, before=None):
        now = before or datetime.now(UTC).isoformat()
        results = [r for r in self._runs.values() if r["status"] in ("pending", "running") and r["created_at"] <= now]
        results.sort(key=lambda r: r["created_at"])
        return results

    async def aggregate_tokens_by_thread(self, thread_id: str, *, include_active: bool = False) -> dict[str, Any]:
        """聚合某 thread 内已完成 run 的 token 用量。

        ``include_active=True`` 时把 running 也算进去（UI 实时显示）。返回 total_tokens /
        total_input_tokens / total_output_tokens / total_runs / by_model / by_caller。
        """
        statuses = ("success", "error", "running") if include_active else ("success", "error")
        # 用 thread 索引做 O(该 thread 的 run 数) 查找，而非扫进程里每个 run（同 ``list_by_thread``，#3562）。
        run_ids = self._runs_by_thread.get(thread_id) or ()
        completed = [run for run_id in run_ids if (run := self._runs.get(run_id)) is not None and run.get("status") in statuses]
        by_model: dict[str, dict] = {}
        for r in completed:
            usage_by_model = r.get("token_usage_by_model") or {}
            if usage_by_model:
                # #3658：按模型归桶——一次 run 可能调多个模型（lead + 多个子代理），各模型 token 分开计。
                for model, usage in usage_by_model.items():
                    entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                    entry["tokens"] += usage.get("total_tokens", 0)
                    entry["runs"] += 1
            else:
                # 兜底：per-model 落地前写的旧行，把整 run 归到它的单一 ``model_name``。
                # 保留 legacy 的 lead-only 行为，而非静默丢老数据。
                model = r.get("model_name") or "unknown"
                entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                entry["tokens"] += r.get("total_tokens", 0)
                entry["runs"] += 1
        return {
            "total_tokens": sum(r.get("total_tokens", 0) for r in completed),
            "total_input_tokens": sum(r.get("total_input_tokens", 0) for r in completed),
            "total_output_tokens": sum(r.get("total_output_tokens", 0) for r in completed),
            "total_runs": len(completed),
            "by_model": by_model,
            "by_caller": {
                "lead_agent": sum(r.get("lead_agent_tokens", 0) for r in completed),
                "subagent": sum(r.get("subagent_tokens", 0) for r in completed),
                "middleware": sum(r.get("middleware_tokens", 0) for r in completed),
            },
        }

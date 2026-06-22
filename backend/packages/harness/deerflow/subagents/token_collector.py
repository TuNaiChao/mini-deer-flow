"""子代理执行内收集 LLM token 用量的回调处理器。

每次子代理执行创建自己的 collector。子代理结束后，收集到的记录经
:meth:`RunJournal.record_external_llm_usage_records` 回灌父 RunJournal
（caller 标 ``subagent:<name>``），让父 run 的 token 核算含子代理开销。

去重：同一 ``run_id`` 只计一次（流式可能多次 ``on_llm_end``）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


class SubagentTokenCollector(BaseCallbackHandler):
    """收集子代理内 LLM token 用量的轻量回调处理器。"""

    def __init__(self, caller: str):
        super().__init__()
        self.caller = caller
        self._records: list[dict[str, int | str]] = []
        self._counted_run_ids: set[str] = set()

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        rid = str(run_id)
        if rid in self._counted_run_ids:
            return

        for generation in response.generations:
            for gen in generation:
                if not hasattr(gen, "message"):
                    continue
                usage = getattr(gen.message, "usage_metadata", None)
                usage_dict = dict(usage) if usage else {}
                input_tk = usage_dict.get("input_tokens", 0) or 0
                output_tk = usage_dict.get("output_tokens", 0) or 0
                total_tk = usage_dict.get("total_tokens", 0) or 0
                if total_tk <= 0:
                    total_tk = input_tk + output_tk
                if total_tk <= 0:
                    continue
                self._counted_run_ids.add(rid)
                self._records.append(
                    {
                        "source_run_id": rid,
                        "caller": self.caller,
                        "input_tokens": input_tk,
                        "output_tokens": output_tk,
                        "total_tokens": total_tk,
                    }
                )
                return

    def snapshot_records(self) -> list[dict[str, int | str]]:
        """返回累计用量记录的副本。"""
        return list(self._records)

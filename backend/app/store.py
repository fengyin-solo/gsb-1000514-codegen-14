"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

多孔水位对照台用到的时序/复核/偏离/补测四张辅助表也挂在这里，
但不计入通用业务模块清单（module_names / overview），避免污染运营概览。
辅助表的写入统一走 transaction()：进入时做整库快照，异常时整体回滚，
用来保证“补测任务与偏离标记同一事务落库，未成功整批回滚”。
"""
from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.seed import SEED_ROWS

# 对照台辅助表：不属于通用业务模块，不进 module_names/overview
HYDRO_AUX_TABLES = {"hydro_series", "hydro_reviews", "hydro_deviations", "hydro_retasks"}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in HYDRO_AUX_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """按表现有最大 id 分配下一个自增 id。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """整库快照事务：with 块内任意一步抛错，所有表恢复到进入前状态。"""
        snapshot = copy.deepcopy(self._tables)
        try:
            yield self
        except Exception:
            self._tables = snapshot
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

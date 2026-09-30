"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    # 领域内部表：通过专门接口暴露，不进运营概览的分模块清单
    INTERNAL_TABLES = {
        "hydro_wells", "hydro_series", "hydro_reviews",
        "hydro_deviations", "hydro_tasks",
    }

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in self.INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """领域动作的事务边界：区间内直接改内存表，任意异常都用快照整批回滚。

        补测任务与偏离标记必须同生共死，靠这里保证「未成功时整批回滚」。
        """
        snapshot = copy.deepcopy(self._tables)
        try:
            yield
        except Exception:
            self._tables = snapshot
            raise

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

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
        # 多孔水位对照台的偏离结论驱动概览看板重算，而不只是刷曲线。
        # 汇总函数由服务层注册，避免 store -> services 的导入环。
        extra_cards = self._compare_cards_provider() if self._compare_cards_provider else []
        return {"cards": cards + extra_cards, "modules": modules}

    def set_compare_cards_provider(self, provider: Any) -> None:
        self._compare_cards_provider = provider

    _compare_cards_provider: Any = None


store = Store()

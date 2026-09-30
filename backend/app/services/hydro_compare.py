"""多孔水位对照台业务规则。

领域表（都挂在内存 store 上，语义按真实库设计，方便之后换成数据库实现）：
- hydro_wells       观测孔：井号、观测类型、所在钻孔、基准水位与偏离带、观测周期
- hydro_series      时间序列：按「观测编号」幂等写入，只追加不改写，乱序按观测时间归位
- hydro_reviews     人工复核记录：复核值优先于原始值参与偏离判定，但原始曲线不追溯改写
- hydro_deviations  偏离段标记：与补测任务同事务落库；已派发/已补测的段冻结，不被重算回退
- hydro_tasks       补测任务

偏离结论每次重算后同步到：水文观测台账（hydro 表）、钻孔详情（由 borehole 路由拼装）、
补测待办清单（hydro_tasks），并驱动 store.overview 概览看板重算。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

WELLS = "hydro_wells"
SERIES = "hydro_series"
REVIEWS = "hydro_reviews"
DEVIATIONS = "hydro_deviations"
TASKS = "hydro_tasks"
LEDGER = "hydro"

# 偏离段状态机
DEV_OPEN = "偏离中"        # 由重算发现、尚未派发补测
DEV_PENDING = "待补测"     # 已派发补测任务，段被冻结
DEV_FILLED = "已补测"      # 补测任务完成，段被冻结
DEV_CLEARED = "已消除"     # 未派发任务，补数/复核后偏离消失

TASK_PENDING = "待补测"
TASK_DONE = "已补测"

ACTIVE_DEV_STATUSES = (DEV_OPEN, DEV_PENDING)
FROZEN_DEV_STATUSES = (DEV_PENDING, DEV_FILLED)


class DomainError(Exception):
    """业务校验失败：在事务内抛出即触发整批回滚。"""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _parse_dt(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("观测时间为空")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # 兼容只传日期的情况
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())


def _to_float(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DomainError(f"{label}不是合法数值：{value!r}")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _expected_slots(well: dict[str, Any], horizon: str) -> list[date]:
    """按观测周期生成应测日期序列（含今日）。"""
    start = date.fromisoformat(str(well["计划开始日期"]))
    end = date.fromisoformat(horizon)
    step = max(int(well.get("观测周期", 1) or 1), 1)
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(0, days + 1, step)]


def _latest_reviews() -> dict[str, dict[str, Any]]:
    """每个观测编号只认最新一条复核记录（以复核记录为准）。"""
    latest: dict[str, dict[str, Any]] = {}
    for review in store.rows(REVIEWS):
        code = str(review["观测编号"])
        old = latest.get(code)
        if old is None or str(review["复核时间"]) >= str(old["复核时间"]):
            latest[code] = review
    return latest


class HydroCompareService:
    # ------------------------------------------------------------------ 读取
    def list_wells(self) -> list[dict[str, Any]]:
        return store.rows(WELLS)

    def get_well(self, well_id: int) -> dict[str, Any] | None:
        return store.find(WELLS, well_id)

    def well_tasks(self, well_id: int) -> list[dict[str, Any]]:
        return [t for t in store.rows(TASKS) if int(t["well_id"]) == well_id]

    def _slot_snapshot(
        self, well: dict[str, Any], horizon: str
    ) -> list[dict[str, Any]]:
        """把乱序回传的序列按观测时间归位到应测时隙，叠加以复核记录为准的有效值。"""
        well_id = int(well["id"])
        raw_rows = [r for r in store.rows(SERIES) if int(r["well_id"]) == well_id]
        # 同一时隙允许多条（补测另编号），取观测时间最新的一条作为曲线点
        latest_by_slot: dict[date, dict[str, Any]] = {}
        for row in raw_rows:
            slot = _parse_dt(row["观测时间"]).date()
            old = latest_by_slot.get(slot)
            if old is None or str(row["观测时间"]) >= str(old["观测时间"]):
                latest_by_slot[slot] = row

        reviews = _latest_reviews()
        low = float(well["允许下限"])
        high = float(well["允许上限"])
        horizon_d = date.fromisoformat(horizon)
        points: list[dict[str, Any]] = []
        for slot in _expected_slots(well, horizon):
            raw = latest_by_slot.get(slot)
            value: float | None = None
            reviewed = False
            review_note = ""
            if raw is not None:
                value = float(raw["水位值"])
                review = reviews.get(str(raw["观测编号"]))
                if review is not None:
                    value = float(review["复核值"])
                    reviewed = True
                    review_note = str(review.get("复核人") or "")
            if value is None:
                status = "待观测" if slot == horizon_d else "缺测"
            elif value < low or value > high:
                status = "超限"
            else:
                status = "正常"
            points.append({
                "时隙": slot.isoformat(),
                "观测编号": raw["观测编号"] if raw else None,
                "原始水位": float(raw["水位值"]) if raw else None,
                "水位值": value,
                "已复核": reviewed,
                "复核人": review_note,
                "状态": status,
            })
        return points

    def _truth_segments(
        self, well: dict[str, Any], horizon: str,
        frozen_slots: set[date] | None = None,
    ) -> list[dict[str, Any]]:
        """根据当前有效数据计算偏离真值段：相邻应测时隙连续缺测/超限即合并为一段。

        今日时隙尚无数据只算「待观测」，不产生偏离，避免把当班没测误报成偏离。
        被冻结段（待补测/已补测）覆盖的时隙从真值中扣除，保证开放段不会与
        冻结段重叠、偏离天数不重复统计。
        """
        frozen_slots = frozen_slots or set()
        cycle = max(int(well.get("观测周期", 1) or 1), 1)
        segments: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        prev_slot: date | None = None
        for point in self._slot_snapshot(well, horizon):
            slot = date.fromisoformat(point["时隙"])
            bad = point["状态"] in ("缺测", "超限") and slot not in frozen_slots
            adjacent = prev_slot is not None and (slot - prev_slot).days == cycle
            if bad:
                if current is None or not adjacent:
                    current = {
                        "well_id": int(well["id"]),
                        "开始时间": slot.isoformat(),
                        "结束时间": slot.isoformat(),
                        "缺测天数": 0,
                        "超限天数": 0,
                    }
                    segments.append(current)
                else:
                    current["结束时间"] = slot.isoformat()
                if point["状态"] == "缺测":
                    current["缺测天数"] += 1
                else:
                    current["超限天数"] += 1
                prev_slot = slot
            else:
                current = None
                # 冻结时隙打断相邻性：跨过它的开放段必须断开
                if point["状态"] in ("缺测", "超限"):
                    prev_slot = None
                else:
                    prev_slot = slot
        for seg in segments:
            start = date.fromisoformat(seg["开始时间"])
            end = date.fromisoformat(seg["结束时间"])
            seg["偏离天数"] = (end - start).days + 1
        return segments

    def _well_marks(self, well_id: int) -> list[dict[str, Any]]:
        return [m for m in store.rows(DEVIATIONS) if int(m["well_id"]) == well_id]

    # ------------------------------------------------------------ 偏离重算
    def recompute(self, horizon: str | None = None) -> None:
        """以当前数据真值重算所有孔的偏离段结论。

        - 无任务的开放段跟随真值：延伸/收缩/消除/新建；
        - 已派发（待补测）与已补测的段冻结，补数/复核不追溯改写任务结论；
        - 消除的段保留行做审计，只是状态变「已消除」。
        """
        horizon = horizon or date.today().isoformat()
        now = _now()
        for well in store.rows(WELLS):
            well_id = int(well["id"])
            marks = self._well_marks(well_id)
            frozen_slots = set()
            for mark in marks:
                if mark["状态"] in FROZEN_DEV_STATUSES:
                    start = date.fromisoformat(str(mark["开始时间"]))
                    end = date.fromisoformat(str(mark["结束时间"]))
                    cur = start
                    while cur <= end:
                        frozen_slots.add(cur)
                        cur += timedelta(days=max(int(well.get("观测周期", 1) or 1), 1))
            truth = {seg["开始时间"]: seg
                     for seg in self._truth_segments(well, horizon, frozen_slots)}
            by_start = {str(m["开始时间"]): m for m in marks}

            # 1) 开放段跟随真值更新；冻结段原样保留
            for mark in marks:
                if mark["状态"] in FROZEN_DEV_STATUSES:
                    continue
                seg = truth.get(str(mark["开始时间"]))
                if seg is None:
                    if mark["状态"] == DEV_OPEN:
                        mark["状态"] = DEV_CLEARED
                        mark["更新时间"] = now
                else:
                    mark.update({
                        "状态": DEV_OPEN,
                        "结束时间": seg["结束时间"],
                        "偏离天数": seg["偏离天数"],
                        "缺测天数": seg["缺测天数"],
                        "超限天数": seg["超限天数"],
                        "更新时间": now,
                    })
            # 2) 真值里冒出来的新段，落新标记
            for start, seg in truth.items():
                if start not in by_start:
                    store.rows(DEVIATIONS).append({
                        "id": _next_id(store.rows(DEVIATIONS)),
                        "well_id": well_id,
                        "开始时间": start,
                        "结束时间": seg["结束时间"],
                        "偏离天数": seg["偏离天数"],
                        "缺测天数": seg["缺测天数"],
                        "超限天数": seg["超限天数"],
                        "状态": DEV_OPEN,
                        "task_id": None,
                        "创建时间": now,
                        "更新时间": now,
                    })

    def _active_marks(self, well_id: int) -> list[dict[str, Any]]:
        return [m for m in self._well_marks(well_id) if m["状态"] in ACTIVE_DEV_STATUSES]

    def _open_tasks(self, well_id: int | None = None) -> list[dict[str, Any]]:
        rows = [t for t in store.rows(TASKS) if t["状态"] == TASK_PENDING]
        if well_id is not None:
            rows = [t for t in rows if int(t["well_id"]) == well_id]
        return rows

    # ------------------------------------------------------- 台账/看板同步
    def sync_ledger(self) -> None:
        """把偏离结论同步到水文观测台账（hydro 表），一孔一条。"""
        ledger = store.rows(LEDGER)
        now = _now()
        existing = {str(row.get("观测编号")): row for row in ledger}
        for well in store.rows(WELLS):
            well_id = int(well["id"])
            active = self._active_marks(well_id)
            open_tasks = self._open_tasks(well_id)
            latest = self.latest_point(well_id)
            deviation_days = sum(int(m["偏离天数"]) for m in active)
            conclusion = "水位正常"
            if active:
                parts = [f"{m['开始时间']}~{m['结束时间']}偏离{m['偏离天数']}天"
                         f"（{'待补测' if m['状态'] == DEV_PENDING else '待处理'}）"
                         for m in active]
                conclusion = "；".join(parts)
            values = {
                "观测编号": well["台账编号"],
                "观测类型": well["观测类型"],
                "所在钻孔": well["钻孔编号"],
                "静止水位": f"{latest['水位值']:.2f}" if latest and latest["水位值"] is not None else "—",
                "降深": "—",
                "出水量": "—",
                "观测日期": (latest["观测时间"][:10] if latest else well["计划开始日期"]),
                "观测状态": "数据异常" if active else "已观测",
                "偏离天数": deviation_days,
                "待补测数量": len(open_tasks),
                "最新偏离结论": conclusion,
                "结论更新时间": now,
            }
            row = existing.get(str(well["台账编号"]))
            if row is None:
                row = {"id": _next_id(ledger)}
                ledger.append(row)
            abnormal = bool(active)
            row.update(values)
            row["status"] = "数据异常" if abnormal else "已观测"
            row["pending"] = len(open_tasks) > 0
            row["abnormal"] = abnormal

    def borehole_summary(self, borehole_code: str) -> dict[str, Any]:
        """钻孔详情里嵌入的多孔水位对照结论。"""
        wells = [w for w in store.rows(WELLS) if w["钻孔编号"] == borehole_code]
        items: list[dict[str, Any]] = []
        for well in wells:
            active = self._active_marks(int(well["id"]))
            items.append({
                "井号": well["井号"],
                "观测类型": well["观测类型"],
                "基准水位": well["基准水位"],
                "偏离带": [well["允许下限"], well["允许上限"]],
                "偏离天数": sum(int(m["偏离天数"]) for m in active),
                "待补测数量": len(self._open_tasks(int(well["id"]))),
                "偏离段": [self._mark_view(m) for m in self._well_marks(int(well["id"]))],
            })
        return {
            "钻孔编号": borehole_code,
            "观测孔数": len(items),
            "偏离天数合计": sum(int(i["偏离天数"]) for i in items),
            "待补测数量": sum(int(i["待补测数量"]) for i in items),
            "观测孔": items,
        }

    def compare_cards(self) -> list[dict[str, Any]]:
        """概览看板上由对照台驱动重算的卡片。"""
        deviation_days = 0
        for well in store.rows(WELLS):
            deviation_days += sum(int(m["偏离天数"]) for m in self._active_marks(int(well["id"])))
        return [
            {"label": "对照观测孔", "value": len(store.rows(WELLS))},
            {"label": "水位偏离天数", "value": deviation_days},
            {"label": "待补测任务", "value": len(self._open_tasks())},
            {"label": "已补测偏离段",
             "value": sum(1 for m in store.rows(DEVIATIONS) if m["状态"] == DEV_FILLED)},
        ]

    # ----------------------------------------------------------------- 看板
    def board(self, horizon: str | None = None) -> dict[str, Any]:
        """首屏：按观测类型、所在钻孔排列每孔时间轴、偏离带、偏离段与看板指标。"""
        horizon = horizon or date.today().isoformat()
        groups: dict[str, list[dict[str, Any]]] = {}
        for well in sorted(store.rows(WELLS), key=lambda w: (w["观测类型"], w["钻孔编号"], w["井号"])):
            timeline = self._slot_snapshot(well, horizon)
            marks = sorted(self._well_marks(int(well["id"])),
                           key=lambda m: str(m["开始时间"]))
            active = self._active_marks(int(well["id"]))
            latest = self.latest_point(int(well["id"]))
            view = {
                "well_id": well["id"],
                "井号": well["井号"],
                "观测类型": well["观测类型"],
                "所在钻孔": well["钻孔编号"],
                "基准水位": well["基准水位"],
                "偏离带": {"下限": well["允许下限"], "上限": well["允许上限"]},
                "观测周期": well["观测周期"],
                "时间轴": timeline,
                "偏离段": [self._mark_view(m) for m in marks],
                "统计": {
                    "偏离天数": sum(int(m["偏离天数"]) for m in active),
                    "待补测数量": len(self._open_tasks(int(well["id"]))),
                    "序列总数": len([p for p in timeline if p["观测编号"] is not None]),
                    "最新序列": latest,
                },
            }
            groups.setdefault(str(well["观测类型"]), []).append(view)

        type_groups = [{"观测类型": name, "观测孔": items} for name, items in groups.items()]
        return {
            "horizon": horizon,
            "观测类型分组": type_groups,
            "汇总": {card["label"]: card["value"] for card in self.compare_cards()},
        }

    def _mark_view(self, mark: dict[str, Any]) -> dict[str, Any]:
        task = None
        if mark.get("task_id"):
            task = next((t for t in store.rows(TASKS) if int(t["id"]) == int(mark["task_id"])), None)
        return {
            "id": mark["id"],
            "well_id": mark["well_id"],
            "开始时间": mark["开始时间"],
            "结束时间": mark["结束时间"],
            "偏离天数": mark["偏离天数"],
            "缺测天数": mark["缺测天数"],
            "超限天数": mark["超限天数"],
            "状态": mark["状态"],
            "任务编号": task["任务编号"] if task else None,
            "可派发": mark["状态"] == DEV_OPEN,
        }

    def list_deviations(self, well_id: int | None = None) -> list[dict[str, Any]]:
        marks = store.rows(DEVIATIONS) if well_id is None else self._well_marks(well_id)
        return [self._mark_view(m) for m in sorted(marks, key=lambda m: (m["well_id"], m["开始时间"]))]

    def latest_point(self, well_id: int) -> dict[str, Any] | None:
        """最新序列：按观测时间取最新一条，返回有效值（复核优先），供分页与看板对齐总数。"""
        rows = [r for r in store.rows(SERIES) if int(r["well_id"]) == well_id]
        if not rows:
            return None
        raw = max(rows, key=lambda r: str(r["观测时间"]))
        review = _latest_reviews().get(str(raw["观测编号"]))
        return {
            "观测编号": raw["观测编号"],
            "观测时间": raw["观测时间"],
            "原始水位": float(raw["水位值"]),
            "水位值": float(review["复核值"]) if review else float(raw["水位值"]),
            "已复核": review is not None,
        }

    # --------------------------------------------------------- 长序列分页
    def series_page(
        self, well_id: int, page: int = 1, size: int = 30, order: str = "desc"
    ) -> dict[str, Any]:
        """长序列分页：先按观测时间整体归位排序再切片，total 与最新序列始终同源一致。"""
        well = self.get_well(well_id)
        if well is None:
            raise DomainError(f"观测孔 {well_id} 不存在")
        rows = [r for r in store.rows(SERIES) if int(r["well_id"]) == well_id]
        reviews = _latest_reviews()
        items = []
        for raw in rows:
            review = reviews.get(str(raw["观测编号"]))
            items.append({
                "id": raw["id"],
                "观测编号": raw["观测编号"],
                "观测时间": raw["观测时间"],
                "回传时间": raw.get("回传时间"),
                "原始水位": float(raw["水位值"]),
                "水位值": float(review["复核值"]) if review else float(raw["水位值"]),
                "已复核": review is not None,
                "复核人": str(review["复核人"]) if review else None,
                "复核时间": str(review["复核时间"]) if review else None,
            })
        ascending = sorted(items, key=lambda r: str(r["观测时间"]))
        total = len(ascending)
        page = max(page, 1)
        size = max(min(size, 500), 1)
        start = (page - 1) * size
        page_rows = ascending[start:start + size]
        if order == "desc":
            page_rows = list(reversed(page_rows))
        latest = self.latest_point(well_id)
        if latest is not None and total:
            # 总数必须与最新序列对得上：最新点必然包含在全量序列内
            assert any(r["观测编号"] == latest["观测编号"] for r in ascending)
        return {
            "items": page_rows,
            "total": total,
            "page": page,
            "size": size,
            "最新序列": latest,
        }

    # ------------------------------------------------------- 序列幂等写入
    def ingest_observations(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        """乱序批量回传：整批校验 + 按观测编号幂等 + 同一事务落库。

        任一条不合法或落库失败，整批回滚；重发同一观测编号只认第一次（first-write-wins），
        历史曲线不做追溯改写。
        """
        if not observations:
            raise DomainError("观测值批次为空")
        codes = [str(item.get("观测编号") or "").strip() for item in observations]
        if any(not code for code in codes):
            raise DomainError("存在缺少观测编号的观测值")
        if len(set(codes)) != len(codes):
            dup = {code for code in codes if codes.count(code) > 1}
            raise DomainError(f"批次内观测编号重复：{'、'.join(sorted(dup))}")

        with store.transaction():
            existing = {str(r["观测编号"]) for r in store.rows(SERIES)}
            inserted: list[str] = []
            skipped: list[str] = []
            now = _now()
            for item, code in zip(observations, codes):
                if code in existing:
                    skipped.append(code)
                    continue
                well = self._resolve_well(item)
                observed_at = _parse_dt(item.get("观测时间"))
                value = _to_float(item.get("水位值"), f"观测编号 {code} 的水位值")
                store.rows(SERIES).append({
                    "id": _next_id(store.rows(SERIES)),
                    "观测编号": code,
                    "well_id": int(well["id"]),
                    "观测类型": well["观测类型"],
                    "观测时间": observed_at.replace(microsecond=0).isoformat(),
                    "回传时间": now,
                    "水位值": value,
                    "状态": "已观测",
                })
                inserted.append(code)
            if inserted:
                self.recompute()
            self.sync_ledger()
        return {
            "写入条数": len(inserted),
            "幂等跳过": skipped,
            "写入编号": inserted,
            "各孔最新": {w["井号"]: self.latest_point(int(w["id"])) for w in store.rows(WELLS)},
        }

    def _resolve_well(self, item: dict[str, Any]) -> dict[str, Any]:
        well_id = item.get("well_id")
        if well_id is not None:
            well = store.find(WELLS, int(well_id))
            if well is None:
                raise DomainError(f"观测孔 {well_id} 不存在")
            return well
        code = str(item.get("井号") or "").strip()
        well = next((w for w in store.rows(WELLS) if w["井号"] == code), None)
        if well is None:
            raise DomainError(f"井号 {code or '（空）'} 不存在")
        return well

    # ------------------------------------------------------------- 人工复核
    def review_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记人工复核：以复核记录为准参与偏离判定；原始序列行永不改写。"""
        code = str(payload.get("观测编号") or "").strip()
        if not code:
            raise DomainError("缺少观测编号")
        row = next((r for r in store.rows(SERIES) if str(r["观测编号"]) == code), None)
        if row is None:
            raise DomainError(f"观测编号 {code} 不存在，无法复核")
        value = _to_float(payload.get("复核值"), "复核值")
        reviewer = str(payload.get("复核人") or "值班管理员").strip()
        reviewed_at = _parse_dt(payload.get("复核时间") or _now()).replace(microsecond=0).isoformat()

        with store.transaction():
            store.rows(REVIEWS).append({
                "id": _next_id(store.rows(REVIEWS)),
                "观测编号": code,
                "原始水位": float(row["水位值"]),
                "复核值": value,
                "复核人": reviewer,
                "复核时间": reviewed_at,
            })
            # 只追加复核记录，不动 hydro_series 原始行 → 历史曲线不追溯改写
            self.recompute()
            self.sync_ledger()
        return {
            "观测编号": code,
            "观测时间": row["观测时间"],
            "原始水位": float(row["水位值"]),
            "复核值": value,
            "复核人": reviewer,
            "复核时间": reviewed_at,
        }

    # ------------------------------------------------------------- 派发补测
    def dispatch_retest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """点选偏离段派发补测：校验通过后，任务与偏离标记在同一事务里落库。

        任一偏离段不存在/已派发都会整体失败回滚。
        """
        mark_ids = payload.get("偏离段id") or payload.get("deviation_ids") or []
        if not isinstance(mark_ids, list) or not mark_ids:
            raise DomainError("请至少选择一个偏离段")
        mark_ids = [int(x) for x in mark_ids]
        assignee = str(payload.get("指派人员") or "水文补测组").strip()
        due = str(payload.get("要求完成时间") or "").strip() or None

        with store.transaction():
            marks = []
            well_id: int | None = None
            for mid in mark_ids:
                mark = next((m for m in store.rows(DEVIATIONS) if int(m["id"]) == mid), None)
                if mark is None:
                    raise DomainError(f"偏离段 {mid} 不存在")
                if mark["状态"] in FROZEN_DEV_STATUSES:
                    raise DomainError(
                        f"偏离段 {mark['开始时间']}~{mark['结束时间']}已派发补测，不能重复派发"
                    )
                if well_id is None:
                    well_id = int(mark["well_id"])
                elif int(mark["well_id"]) != well_id:
                    raise DomainError("一次派发只能选择同一观测孔的偏离段")
                marks.append(mark)

            well = store.find(WELLS, well_id)  # type: ignore[arg-type]
            today = date.today().isoformat().replace("-", "")
            seq = len([t for t in store.rows(TASKS)
                       if str(t["任务编号"]).startswith(f"RT-{today}")]) + 1
            now = _now()
            task = {
                "id": _next_id(store.rows(TASKS)),
                "任务编号": f"RT-{today}-{seq:03d}",
                "well_id": well_id,
                "偏离段id": [int(m["id"]) for m in marks],
                "状态": TASK_PENDING,
                "指派人员": assignee,
                "要求完成时间": due,
                "派发时间": now,
                "补测时间": None,
            }
            store.rows(TASKS).append(task)
            for mark in marks:
                mark["状态"] = DEV_PENDING
                mark["task_id"] = task["id"]
                mark["更新时间"] = now
            # 段被任务冻结，重算也不会回退；台账同步在事务内，失败整批回滚
            self.recompute()
            self.sync_ledger()
        return {"任务": task, "偏离段": [self._mark_view(m) for m in marks]}

    def complete_task(self, task_id: int) -> dict[str, Any]:
        """补测回填后关闭任务：关联偏离段置「已补测」并冻结，台账同步重算。"""
        with store.transaction():
            task = next((t for t in store.rows(TASKS) if int(t["id"]) == task_id), None)
            if task is None:
                raise DomainError(f"补测任务 {task_id} 不存在")
            if task["状态"] != TASK_PENDING:
                raise DomainError(f"任务 {task['任务编号']} 已关闭，不能重复完成")
            now = _now()
            task["状态"] = TASK_DONE
            task["补测时间"] = now
            for mid in task["偏离段id"]:
                mark = next((m for m in store.rows(DEVIATIONS) if int(m["id"]) == mid), None)
                if mark is not None:
                    mark["状态"] = DEV_FILLED
                    mark["更新时间"] = now
            self.recompute()
            self.sync_ledger()
        return {"任务": task}

    def list_tasks(self, status: str | None = None, well_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(TASKS)
        if status:
            rows = [t for t in rows if t["状态"] == status]
        if well_id is not None:
            rows = [t for t in rows if int(t["well_id"]) == well_id]
        views = []
        for task in sorted(rows, key=lambda t: str(t["派发时间"]), reverse=True):
            well = store.find(WELLS, int(task["well_id"]))
            marks = [self._mark_view(m) for m in store.rows(DEVIATIONS)
                     if int(m["id"]) in [int(x) for x in task["偏离段id"]]]
            views.append({
                "id": task["id"],
                "任务编号": task["任务编号"],
                "井号": well["井号"] if well else "—",
                "观测类型": well["观测类型"] if well else "—",
                "所在钻孔": well["钻孔编号"] if well else "—",
                "状态": task["状态"],
                "指派人员": task["指派人员"],
                "要求完成时间": task.get("要求完成时间"),
                "派发时间": task["派发时间"],
                "补测时间": task.get("补测时间"),
                "偏离段": marks,
            })
        return views

    # ------------------------------------------------------------- 示例数据
    def bootstrap(self) -> None:
        """初始化对照台示例数据并完成首轮偏离重算与台账同步（幂等）。"""
        if store.rows(WELLS):
            return
        self._seed()

    def _seed(self) -> None:
        from app.hydro_seed import build_seed

        wells, series_rows, reviews, frozen = build_seed()
        store.rows(WELLS).extend(wells)
        store.rows(SERIES).extend(series_rows)
        store.rows(REVIEWS).extend(reviews)

        horizon = "2026-09-30"
        # 历史已补测段先落库（真值已恢复）：随后的重算必须让它冻结、不追溯改写
        done_mark, done_task = frozen
        store.rows(DEVIATIONS).append(done_mark)
        store.rows(TASKS).append(done_task)

        # 首轮重算：生成 W1/W2 各开放偏离段
        self.recompute(horizon)

        # W2 09-17~09-18 派发给待办（任务与标记同事务落库）
        w2 = next(w for w in store.rows(WELLS) if w["井号"] == "SW-02")
        gap_mark = next(m for m in self._well_marks(int(w2["id"]))
                        if m["开始时间"] == "2026-09-17")
        self.dispatch_task_seed([gap_mark], assignee="王补测", due="2026-09-20")

        self.sync_ledger()

    def dispatch_task_seed(
        self, marks: list[dict[str, Any]], *, assignee: str, due: str
    ) -> None:
        """种子用：与正式派发同一套冻结语义，但任务号固定为示例日期。"""
        now = "2026-09-18T09:00:00"
        task = {
            "id": _next_id(store.rows(TASKS)),
            "任务编号": "RT-20260918-001",
            "well_id": int(marks[0]["well_id"]),
            "偏离段id": [int(m["id"]) for m in marks],
            "状态": TASK_PENDING,
            "指派人员": assignee,
            "要求完成时间": due,
            "派发时间": now,
            "补测时间": None,
        }
        store.rows(TASKS).append(task)
        for mark in marks:
            mark["状态"] = DEV_PENDING
            mark["task_id"] = task["id"]
            mark["更新时间"] = now


hydro_compare_service = HydroCompareService()

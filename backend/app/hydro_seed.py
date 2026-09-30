"""多孔水位对照台示例数据。

刻意覆盖：乱序回传、连续缺测偏离段、超偏离带、人工复核拉回（原始值保留）、
历史已补测段（冻结不追溯）、待派发段、完全正常的孔。
数据只在内存中生成，确定性可复现，horizon 固定为 2026-09-30。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

HORIZON = "2026-09-30"

# 井号: (所在钻孔, 观测类型, 台账编号, 基准水位, 下限, 上限, 开始日期, 缺测日期, 超限日期)
WELL_SPECS: list[tuple[str, str, str, str, float, float, float, str, set[str], set[str]]] = [
    ("SW-01", "ZK-SW01", "静止水位观测", "HYDR-0001", 12.00, 11.70, 12.30,
     "2026-09-04", {"2026-09-11", "2026-09-12"}, {"2026-09-22"}),
    ("SW-02", "ZK-SW02", "长期水位动态", "HYDR-0002", 48.50, 48.20, 48.80,
     "2026-09-01",
     {"2026-09-17", "2026-09-18", "2026-09-25", "2026-09-26"}, set()),
    ("SW-03", "ZK-SW01", "抽水试验观测", "HYDR-0003", 12.00, 11.60, 12.40,
     "2026-09-08", set(), set()),
]

# SW-02 的 09-10 原始值超带，人工复核拉回带内：曲线保留原始值，判定以复核为准
REVIEWED_OUTLIER = ("SW-02", "2026-09-10", 49.10, 48.55, "李复核", "2026-09-10T18:00:00")


def _daterange(start: str, end: str) -> list[date]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def build_seed() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[dict[str, Any], dict[str, Any]],
]:
    wells: list[dict[str, Any]] = []
    series_rows: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    series_id = 1
    review_id = 1

    for well_seq, (code, borehole, obs_type, ledger_code, base, low, high,
                   start, missing, outliers) in enumerate(WELL_SPECS, start=1):
        well_id = well_seq
        wells.append({
            "id": well_id,
            "井号": code,
            "观测类型": obs_type,
            "所在钻孔": borehole,
            "钻孔编号": borehole,
            "台账编号": ledger_code,
            "基准水位": base,
            "允许下限": low,
            "允许上限": high,
            "观测周期": 1,
            "计划开始日期": start,
        })
        for i, day in enumerate(_daterange(start, HORIZON)):
            day_s = day.isoformat()
            if day_s in missing:
                continue
            # SW-02 的 09-05/09-06 是历史补测回填值（真值已恢复，偏离段按任务冻结）
            if code == "SW-02" and day_s in {"2026-09-05", "2026-09-06"}:
                obs_code = f"RTM-{code[-2:]}-{day.strftime('%m%d')}"
                observed_at = f"{day_s}T16:30:00"
            else:
                obs_code = f"OBS-{code[-2:]}-{day.strftime('%m%d')}"
                observed_at = f"{day_s}T08:00:00"
            value = round(base + 0.05 * math.sin(i * 1.3), 2)
            if day_s in outliers:
                value = 12.55
            # 乱序回传场景：SW-02 的 09-10 先落一条超限原始值
            if code == "SW-02" and day_s == "2026-09-10":
                value = REVIEWED_OUTLIER[2]
            series_rows.append({
                "id": series_id,
                "观测编号": obs_code,
                "well_id": well_id,
                "观测类型": obs_type,
                "观测时间": observed_at,
                "回传时间": f"{day_s}T08:05:00",
                "水位值": value,
                "状态": "已观测",
            })
            series_id += 1

    # 复核记录（只追加，不改原始序列）
    r_code, r_day, raw_v, review_v, reviewer, reviewed_at = REVIEWED_OUTLIER
    target_obs = f"OBS-{r_code[-2:]}-{r_day[5:7]}{r_day[8:10]}"
    reviews.append({
        "id": review_id,
        "观测编号": target_obs,
        "原始水位": raw_v,
        "复核值": review_v,
        "复核人": reviewer,
        "复核时间": reviewed_at,
    })

    # 历史已补测段：SW-02 09-05~09-06，真值已恢复，结论按任务冻结为「已补测」
    done_mark = {
        "id": 1,
        "well_id": 2,
        "开始时间": "2026-09-05",
        "结束时间": "2026-09-06",
        "偏离天数": 2,
        "缺测天数": 2,
        "超限天数": 0,
        "状态": "已补测",
        "task_id": 1,
        "创建时间": "2026-09-07T09:00:00",
        "更新时间": "2026-09-08T15:00:00",
    }
    done_task = {
        "id": 1,
        "任务编号": "RT-20260908-001",
        "well_id": 2,
        "偏离段id": [1],
        "状态": "已补测",
        "指派人员": "李补测",
        "要求完成时间": "2026-09-08",
        "派发时间": "2026-09-07T09:00:00",
        "补测时间": "2026-09-08T15:00:00",
    }

    # 模拟乱序回传：故意打乱落库顺序，服务端按观测时间归位
    series_rows.sort(key=lambda r: (str(r["观测时间"]),), reverse=False)
    # 用确定性置换打散，避免落库顺序等于观测顺序
    shuffled: list[dict[str, Any]] = []
    while series_rows:
        shuffled.append(series_rows.pop(0))
        if len(series_rows) > 2:
            shuffled.append(series_rows.pop(2))
    series_rows = shuffled

    return wells, series_rows, reviews, (done_mark, done_task)

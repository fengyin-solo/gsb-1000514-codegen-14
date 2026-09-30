"""多孔水位对照台接口。

首屏按观测类型与所在钻孔排列各孔时间轴与偏离带；偏离段点选派发补测；
偏离结论的同步、重算、幂等写入与事务回滚全部在 HydroCompareService 内完成，
路由层只做参数解析与错误转译。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    HydroIngestPayload,
    HydroRetestPayload,
    HydroReviewPayload,
)
from app.services.hydro_compare import DomainError, hydro_compare_service

router = APIRouter(prefix="/api/hydro-compare", tags=["多孔水位对照台"])


def _fail(exc: Exception) -> HTTPException:
    # DomainError 触发事务回滚后转成 400；前端能拿到可读原因
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/board")
def board(horizon: str | None = Query(default=None, description="对照截止日期 YYYY-MM-DD")) -> dict:
    """对照台首屏：观测类型分组的各孔时间轴、偏离带、偏离段与看板汇总。"""
    try:
        return hydro_compare_service.board(horizon)
    except ValueError as exc:
        raise _fail(exc)


@router.get("/wells/{well_id}/series")
def well_series(
    well_id: int,
    page: int = 1,
    size: int = 30,
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> dict:
    """长序列分页：total 与最新序列同源，始终对得上。"""
    try:
        return hydro_compare_service.series_page(well_id, page=page, size=size, order=order)
    except DomainError as exc:
        raise _fail(exc)


@router.get("/deviations")
def deviations(well_id: int | None = None) -> dict:
    return {"items": hydro_compare_service.list_deviations(well_id)}


@router.get("/tasks")
def tasks(
    status: str | None = Query(default=None, description="待补测、已补测"),
    well_id: int | None = None,
) -> dict:
    """补测待办清单（偏离结论同步目标之一）。"""
    return {"items": hydro_compare_service.list_tasks(status=status, well_id=well_id)}


@router.post("/observations")
def ingest_observations(payload: HydroIngestPayload) -> dict:
    """乱序批量回传：按观测编号幂等写入，整批失败时事务回滚。"""
    try:
        result = hydro_compare_service.ingest_observations(
            [item.model_dump() for item in payload.observations]
        )
    except DomainError as exc:
        raise _fail(exc)
    except ValueError as exc:
        raise _fail(exc)
    result["ok"] = True
    result["message"] = (
        f"已写入 {result['写入条数']} 条观测值"
        + (f"，幂等跳过 {len(result['幂等跳过'])} 条" if result["幂等跳过"] else "")
    )
    return result


@router.post("/reviews")
def review_observation(payload: HydroReviewPayload) -> dict:
    """人工复核：追加复核记录，以复核记录为准，历史曲线不追溯改写。"""
    try:
        result = hydro_compare_service.review_observation(payload.model_dump())
    except DomainError as exc:
        raise _fail(exc)
    except ValueError as exc:
        raise _fail(exc)
    result["ok"] = True
    result["message"] = f"观测 {result['观测编号']} 已按复核值 {result['复核值']} 生效"
    return result


@router.post("/retests")
def dispatch_retest(payload: HydroRetestPayload) -> dict:
    """点选偏离段派发补测：补测任务与偏离标记同一事务落库。"""
    try:
        result = hydro_compare_service.dispatch_retest(payload.model_dump())
    except DomainError as exc:
        raise _fail(exc)
    result["ok"] = True
    result["message"] = f"补测任务 {result['任务']['任务编号']} 已派发"
    return result


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: int) -> dict:
    """关闭补测任务：关联偏离段冻结为已补测，并同步台账与看板。"""
    try:
        result = hydro_compare_service.complete_task(task_id)
    except DomainError as exc:
        raise _fail(exc)
    result["ok"] = True
    result["message"] = f"补测任务 {result['任务']['任务编号']} 已完成"
    return result

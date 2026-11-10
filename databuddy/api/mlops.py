"""MLOps 接口：特征管理、模型注册与晋级、模型服务（A/B + 弹性伸缩）。

路由前缀 `/api`，与其它 router 一致；认证与工作空间从 `ctx` 依赖注入。
所有业务逻辑在 `databuddy.features` / `databuddy.mlops` 里，这里只做参数校验与序列化。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import features, mlops, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["mlops"])


# ------------------------------------------------------------------ 工具


def _fail(exc: Exception) -> HTTPException:
    """把领域异常翻译成 400，避免前端看到 500 堆栈。"""
    return HTTPException(status_code=400, detail=str(exc))


def _body(body: dict | None) -> dict:
    return body or {}


# ------------------------------------------------------------------ 请求模型


class FeatureIn(BaseModel):
    name: str
    entity: str = ""
    dtype: str = "float"
    expr: str = ""
    table_ref: str = ""
    description: str = ""
    status: str = "published"


class FeaturePatch(BaseModel):
    entity: str | None = None
    dtype: str | None = None
    expr: str | None = None
    table_ref: str | None = None
    description: str | None = None
    status: str | None = None


class ComputeIn(BaseModel):
    names: list[str] = []
    entity_ids: list[str] = []
    limit: int = 20
    materialize: bool = False


class TrainingSetIn(BaseModel):
    feature_names: list[str] = []
    label_expr: str = ""
    table_ref: str = ""
    entity_ids: list[str] = []
    limit: int = 500
    materialize: bool = False
    name: str = ""


class RegisterIn(BaseModel):
    name: str = ""
    source_experiment: str = ""
    framework: str = ""
    description: str = ""
    owner: str = ""
    metrics: dict = {}
    artifact_uri: str = ""
    version_description: str = ""
    stage: str = "staging"


class VersionIn(BaseModel):
    metrics: dict = {}
    artifact_uri: str = ""
    run_id: str = ""
    description: str = ""
    stage: str = "staging"
    experiment: str = ""


class PromoteIn(BaseModel):
    stage: str = "production"


class ServiceIn(BaseModel):
    name: str
    model_ref: str = ""
    model: str = ""
    version: str = ""
    replicas: int = 1
    min_replicas: int = 1
    max_replicas: int = 4
    target_qps: float = 50
    idle_timeout_seconds: int = 0
    traffic: dict = {}
    description: str = ""
    endpoint: str = ""
    start: bool = True


class ServicePatch(BaseModel):
    version: str | None = None
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    target_qps: float | None = None
    idle_timeout_seconds: int | None = None
    traffic: dict | None = None
    description: str | None = None


class TrafficIn(BaseModel):
    traffic: dict = {}


class ActionIn(BaseModel):
    action: str = "start"
    replicas: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    target_qps: float | None = None
    qps: float | None = None


# ------------------------------------------------------------------ 特征


@router.get("/features")
def list_features(
    entity: str = "", keyword: str = "", ctxd: dict = Depends(ctx)
) -> dict:
    items = features.list_features(ctxd["workspace_id"], entity=entity, keyword=keyword)
    return {"features": items, "summary": features.summary(ctxd["workspace_id"])}


@router.get("/features/summary")
def feature_summary(ctxd: dict = Depends(ctx)) -> dict:
    return features.summary(ctxd["workspace_id"])


@router.post("/features")
def create_feature(payload: FeatureIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        row = features.create_feature(ctxd["workspace_id"], payload.model_dump())
    except ValueError as exc:
        raise _fail(exc) from exc
    store.audit(ctxd["workspace_id"], ctxd["user"]["id"], "create_feature", payload.name)
    return {"feature": row}


@router.patch("/features/{feature_id}")
def update_feature(feature_id: str, payload: FeaturePatch, ctxd: dict = Depends(ctx)) -> dict:
    try:
        row = features.update_feature(
            ctxd["workspace_id"], feature_id, payload.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"feature": row}


@router.delete("/features/{feature_id}")
def delete_feature(feature_id: str, ctxd: dict = Depends(ctx)) -> dict:
    try:
        features.delete_feature(ctxd["workspace_id"], feature_id)
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"ok": True}


@router.get("/features/{feature_id}/versions")
def feature_versions(feature_id: str, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return features.list_versions(ctxd["workspace_id"], feature_id)
    except ValueError as exc:
        raise _fail(exc) from exc


@router.get("/features/{feature_id}/lineage")
def feature_lineage(feature_id: str, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return features.feature_lineage(ctxd["workspace_id"], feature_id)
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/features/compute")
def compute_features(payload: ComputeIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return features.compute_features(
            ctxd["workspace_id"],
            names=payload.names or None,
            entity_ids=payload.entity_ids or None,
            limit=max(1, min(payload.limit, 200)),
            materialize=payload.materialize,
        )
    except ValueError as exc:
        raise _fail(exc) from exc


@router.post("/features/training-set")
def training_set(payload: TrainingSetIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        return features.build_training_set(
            ctxd["workspace_id"],
            payload.feature_names,
            label_expr=payload.label_expr,
            table_ref=payload.table_ref,
            entity_ids=payload.entity_ids or None,
            limit=max(1, min(payload.limit, 500)),
            materialize=payload.materialize,
            name=payload.name,
        )
    except ValueError as exc:
        raise _fail(exc) from exc


@router.get("/features/by-table")
def features_by_table(table_ref: str = "", ctxd: dict = Depends(ctx)) -> dict:
    return {"features": features.table_features(ctxd["workspace_id"], table_ref)}


# ------------------------------------------------------------------ 模型注册


@router.get("/registry/models")
def list_models(keyword: str = "", ctxd: dict = Depends(ctx)) -> dict:
    return {
        "models": mlops.list_models(ctxd["workspace_id"], keyword=keyword),
        "summary": mlops.summary(ctxd["workspace_id"]),
    }


@router.get("/registry/summary")
def registry_summary(ctxd: dict = Depends(ctx)) -> dict:
    return mlops.summary(ctxd["workspace_id"])


@router.post("/registry/models")
def register_model(payload: RegisterIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        result = mlops.register_model(ctxd["workspace_id"], payload.model_dump())
    except ValueError as exc:
        raise _fail(exc) from exc
    return result


@router.get("/registry/models/{model_id}")
def get_model(model_id: str, ctxd: dict = Depends(ctx)) -> dict:
    model = mlops.get_model(ctxd["workspace_id"], model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"model": model}


@router.post("/registry/models/{model_id}/versions")
def add_version(model_id: str, payload: VersionIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        version = mlops.add_version(ctxd["workspace_id"], model_id, payload.model_dump())
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"version": version, "versions": mlops.list_versions(ctxd["workspace_id"], model_id)["versions"]}


@router.post("/registry/models/{model_id}/versions/{version_id}/promote")
def promote_version(
    model_id: str, version_id: str, payload: PromoteIn, ctxd: dict = Depends(ctx)
) -> dict:
    try:
        return mlops.promote_version(ctxd["workspace_id"], model_id, version_id, payload.stage)
    except ValueError as exc:
        raise _fail(exc) from exc


@router.get("/registry/models/{model_id}/compare")
def compare_versions(model_id: str, a: str = "", b: str = "", ctxd: dict = Depends(ctx)) -> dict:
    if not a or not b:
        raise HTTPException(status_code=400, detail="需要提供 a 与 b 两个版本")
    try:
        return mlops.compare_versions(ctxd["workspace_id"], model_id, a, b)
    except ValueError as exc:
        raise _fail(exc) from exc


# ------------------------------------------------------------------ 模型服务


@router.get("/serving/services")
def list_services(ctxd: dict = Depends(ctx)) -> dict:
    return {
        "services": mlops.list_services(ctxd["workspace_id"]),
        "summary": mlops.summary(ctxd["workspace_id"]),
    }


@router.post("/serving/services")
def create_service(payload: ServiceIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        service = mlops.create_service(ctxd["workspace_id"], payload.model_dump())
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"service": service}


@router.get("/serving/services/{service_id}")
def get_service(service_id: str, ctxd: dict = Depends(ctx)) -> dict:
    service = mlops.get_service(ctxd["workspace_id"], service_id)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")
    return {"service": service}


@router.patch("/serving/services/{service_id}")
def update_service(service_id: str, payload: ServicePatch, ctxd: dict = Depends(ctx)) -> dict:
    try:
        service = mlops.update_service(
            ctxd["workspace_id"], service_id, payload.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"service": service}


@router.post("/serving/services/{service_id}/action")
def service_action(service_id: str, payload: ActionIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        service = mlops.service_action(
            ctxd["workspace_id"],
            service_id,
            payload.action,
            payload.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"service": service}


@router.post("/serving/services/{service_id}/traffic")
def set_traffic(service_id: str, payload: TrafficIn, ctxd: dict = Depends(ctx)) -> dict:
    try:
        service = mlops.set_traffic(ctxd["workspace_id"], service_id, payload.traffic)
    except ValueError as exc:
        raise _fail(exc) from exc
    return {"service": service}


@router.post("/serving/services/{service_id}/autoscale")
def autoscale(service_id: str, body: dict | None = None, ctxd: dict = Depends(ctx)) -> dict:
    qps = (body or {}).get("qps", 0)
    try:
        return mlops.autoscale_evaluate(ctxd["workspace_id"], service_id, float(qps or 0))
    except (ValueError, TypeError) as exc:
        raise _fail(exc) from exc


@router.post("/serving/auto-stop-check")
def auto_stop_check(ctxd: dict = Depends(ctx)) -> dict:
    return mlops.auto_stop_check(ctxd["workspace_id"])

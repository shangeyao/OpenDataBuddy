"""CI/CD：bundle 配置即代码的 HTTP 接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import bundle as engine
from .auth import ctx

router = APIRouter(prefix="/api", tags=["cicd"])


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class InitIn(BaseModel):
    name: str = "my-bundle"


class YamlIn(BaseModel):
    yaml: str = ""


class EnvIn(BaseModel):
    env: str = "dev"


class RunIn(BaseModel):
    env: str = "dev"
    resource: str | None = None


def _owned(bundle_id: str, workspace_id: str) -> dict:
    row = engine.get_bundle(bundle_id)
    if not row or row["workspace_id"] != workspace_id:
        raise HTTPException(status_code=404, detail="Bundle 不存在")
    return row


@router.get("/bundles")
def list_bundles(ctxd: dict = Depends(ctx)) -> dict:
    rows = engine.list_bundles(ctxd["workspace_id"])
    return {
        "bundles": rows,
        "envs": engine.ENVS,
        "kinds": [
            {"key": k, "label": v["label"], "key_field": v["key"]} for k, v in engine.RESOURCE_KINDS.items()
        ],
    }


@router.post("/bundles")
def create_bundle(payload: InitIn, ctxd: dict = Depends(ctx)) -> dict:
    row = _guard(engine.init, ctxd["workspace_id"], payload.name)
    return {"bundle": row}


@router.get("/bundles/template")
def bundle_template() -> dict:
    """返回 init 模板，前端可直接预填编辑器。"""
    return {"yaml": engine.dump_yaml(engine.TEMPLATE), "envs": engine.ENVS}


@router.get("/bundles/{bundle_id}")
def get_bundle(bundle_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = _owned(bundle_id, ctxd["workspace_id"])
    return {"bundle": row}


@router.patch("/bundles/{bundle_id}")
def save_bundle(bundle_id: str, payload: YamlIn, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    row = _guard(engine.save_yaml, bundle_id, payload.yaml)
    return {"bundle": row}


@router.post("/bundles/{bundle_id}/validate")
def validate_bundle(bundle_id: str, ctxd: dict = Depends(ctx)) -> dict:
    row = _owned(bundle_id, ctxd["workspace_id"])
    return engine.validate(row["yaml"], ctxd["workspace_id"])


@router.post("/bundles/{bundle_id}/deploy")
def deploy_bundle(bundle_id: str, payload: EnvIn, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    return _guard(engine.deploy, bundle_id, payload.env)


@router.post("/bundles/{bundle_id}/run")
def run_bundle(bundle_id: str, payload: RunIn, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    return _guard(engine.run, bundle_id, payload.env, payload.resource)


@router.get("/bundles/{bundle_id}/summary")
def bundle_summary(bundle_id: str, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    return _guard(engine.summary, bundle_id)


@router.post("/bundles/{bundle_id}/destroy")
def destroy_bundle(bundle_id: str, payload: EnvIn, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    return _guard(engine.destroy, bundle_id, payload.env)


@router.get("/bundles/{bundle_id}/deployments")
def bundle_deployments(bundle_id: str, ctxd: dict = Depends(ctx)) -> dict:
    _owned(bundle_id, ctxd["workspace_id"])
    return {"deployments": engine.deployments(bundle_id)}

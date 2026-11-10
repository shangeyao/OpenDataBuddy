"""语义层扩展接口：本体实体 / 关系 / 术语表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import ontology, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["semantic"])


class EntityIn(BaseModel):
    name: str
    display_name: str | None = None
    kind: str | None = None
    description: str | None = None
    table_refs: list[str] | None = None
    key_column: str | None = None
    owner: str | None = None
    color: str | None = None


class RelationIn(BaseModel):
    from_entity: str
    to_entity: str
    verb: str | None = None
    cardinality: str | None = None
    description: str | None = None
    join_hint: str | None = None


class TermIn(BaseModel):
    term: str
    definition: str | None = None
    synonyms: list[str] | None = None
    asset_type: str | None = None
    asset_ref: str | None = None
    entity_id: str | None = None
    owner: str | None = None
    status: str | None = None


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ontology/overview")
def ontology_overview(ctxd: dict = Depends(ctx)) -> dict:
    return ontology.overview(ctxd["workspace_id"])


@router.get("/ontology/entities")
def list_entities(ctxd: dict = Depends(ctx)) -> dict:
    return {"entities": ontology.list_entities(ctxd["workspace_id"])}


@router.post("/ontology/entities")
def create_entity(payload: EntityIn, ctxd: dict = Depends(ctx)) -> dict:
    row = _guard(ontology.create_entity, ctxd["workspace_id"], payload.model_dump(exclude_none=True))
    return {"entity": row}


@router.patch("/ontology/entities/{entity_id}")
def update_entity(entity_id: str, payload: EntityIn, ctxd: dict = Depends(ctx)) -> dict:
    _guard(ontology.update_entity, entity_id, payload.model_dump(exclude_none=True))
    return {"ok": True}


@router.delete("/ontology/entities/{entity_id}")
def delete_entity(entity_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ontology.delete_entity(entity_id)
    return {"ok": True}


@router.get("/ontology/relations")
def list_relations(ctxd: dict = Depends(ctx)) -> dict:
    return {"relations": ontology.list_relations(ctxd["workspace_id"])}


@router.post("/ontology/relations")
def create_relation(payload: RelationIn, ctxd: dict = Depends(ctx)) -> dict:
    row = _guard(ontology.create_relation, ctxd["workspace_id"], payload.model_dump(exclude_none=True))
    return {"relation": row}


@router.delete("/ontology/relations/{relation_id}")
def delete_relation(relation_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ontology.delete_relation(relation_id)
    return {"ok": True}


@router.get("/ontology/graph")
def ontology_graph(ctxd: dict = Depends(ctx)) -> dict:
    return ontology.relation_graph(ctxd["workspace_id"])


@router.get("/glossary")
def list_terms(keyword: str = "", ctxd: dict = Depends(ctx)) -> dict:
    return {"terms": ontology.list_terms(ctxd["workspace_id"], keyword)}


@router.post("/glossary")
def create_term(payload: TermIn, ctxd: dict = Depends(ctx)) -> dict:
    row = _guard(ontology.create_term, ctxd["workspace_id"], payload.model_dump(exclude_none=True))
    return {"term": row}


@router.patch("/glossary/{term_id}")
def update_term(term_id: str, payload: TermIn, ctxd: dict = Depends(ctx)) -> dict:
    _guard(ontology.update_term, term_id, payload.model_dump(exclude_none=True))
    return {"ok": True}


@router.delete("/glossary/{term_id}")
def delete_term(term_id: str, ctxd: dict = Depends(ctx)) -> dict:
    ontology.delete_term(term_id)
    return {"ok": True}


@router.get("/glossary/resolve")
def resolve_term(q: str, ctxd: dict = Depends(ctx)) -> dict:
    return {"match": ontology.resolve_term(ctxd["workspace_id"], q)}

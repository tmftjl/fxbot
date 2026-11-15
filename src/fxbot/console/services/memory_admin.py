"""长期记忆管理用例；不接受 HTTP 请求或返回 HTTP 响应。"""

from __future__ import annotations

import uuid
import base64
import binascii
from typing import Any
from pathlib import Path

from ..queries.memory import (
    _RECORD_TYPES,
    sql_like,
    chat_name,
    fact_view,
    count_rows,
    detail_row,
    label_text,
    mode_label,
    query_rows,
    decode_json,
    limit_value,
    record_view,
    episode_view,
    item_preview,
    list_persons,
    record_types,
    scope_filter,
    source_label,
    tenant_clause,
    tenant_hashes,
    optional_float,
    safe_file_name,
    operation_items,
    validate_tenant,
    format_timestamp,
    tenant_arguments,
    tenant_owns_plan,
    correction_targets,
    related_paragraphs,
    resolve_person_names,
)
from ..services.errors import Download, AdminFailure, MemoryAdminError
from ...core.application import Application
from ...chat.amemorix.runtime import amemorix_runtime
from ...chat.amemorix._host.host_config import save_amemorix_config


def _unavailable() -> MemoryAdminError:
    detail = amemorix_runtime.error or "A_Memorix 未启用或内核尚未就绪"
    return MemoryAdminError(kind=AdminFailure.UNAVAILABLE, detail=detail)


async def _kernel() -> Any:
    kernel = await amemorix_runtime.get_kernel()
    if kernel is None or not amemorix_runtime.available:
        raise _unavailable()
    return kernel


def _metadata(kernel: Any) -> Any:
    store = getattr(kernel, "metadata_store", None)
    if store is None:
        raise _unavailable()
    return store


def _decode_base64(value: Any, *, field: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"{field} 不能为空")
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"{field} 不是合法 Base64") from exc


def _kernel_path(kernel: Any, *parts: str) -> Path:
    path = Path(kernel.data_dir).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def status(tenant: str) -> dict[str, Any]:
    """内核状态与当前租户的记忆计数。"""
    tenant = validate_tenant(tenant)
    persons = await list_persons(tenant)
    payload: dict[str, Any] = {
        "available": amemorix_runtime.available,
        "enabled": bool(amemorix_runtime.config().plugin.enabled),
        "error": amemorix_runtime.error,
        "counts": {
            "paragraphs": 0,
            "entities": 0,
            "relations": 0,
            "episodes": 0,
            "facts": 0,
            "persons": len(persons),
            "profiles": 0,
        },
        "runtime": {},
    }
    integration = amemorix_runtime.config().integration
    payload["integration"] = {
        "enable_memory_query_tool": integration.enable_memory_query_tool,
        "enable_person_profile_query_tool": integration.enable_person_profile_query_tool,
        "enable_person_profile_injection": integration.enable_person_profile_injection,
        "heuristic_memory_recall_enabled": integration.heuristic_memory_recall_enabled,
    }
    if not amemorix_runtime.available:
        return payload
    kernel = await _kernel()
    store = _metadata(kernel)
    clause = tenant_clause("p")
    tenant_params = tuple(tenant_arguments(tenant))
    person_ids = [item["person_id"] for item in persons]
    scope_sql, scope_params = scope_filter(tenant, person_ids)

    counts = payload["counts"]
    counts["paragraphs"] = count_rows(
        store,
        f"SELECT COUNT(*) AS n FROM paragraphs p WHERE COALESCE(p.is_deleted,0)=0 AND {clause}",
        tenant_params,
    )
    counts["entities"] = count_rows(
        store,
        "SELECT COUNT(*) AS n FROM entities e WHERE COALESCE(e.is_deleted,0)=0 AND EXISTS ("
        f"SELECT 1 FROM paragraph_entities pe JOIN paragraphs p ON p.hash=pe.paragraph_hash "
        f"WHERE pe.entity_hash=e.hash AND COALESCE(p.is_deleted,0)=0 AND {clause})",
        tenant_params,
    )
    counts["relations"] = count_rows(
        store,
        "SELECT COUNT(*) AS n FROM relations r WHERE COALESCE(r.is_inactive,0)=0 AND EXISTS ("
        f"SELECT 1 FROM paragraph_relations pr JOIN paragraphs p ON p.hash=pr.paragraph_hash "
        f"WHERE pr.relation_hash=r.hash AND COALESCE(p.is_deleted,0)=0 AND {clause})",
        tenant_params,
    )
    counts["episodes"] = count_rows(
        store,
        "SELECT COUNT(*) AS n FROM episodes e WHERE EXISTS ("
        f"SELECT 1 FROM episode_paragraphs ep JOIN paragraphs p ON p.hash=ep.paragraph_hash "
        f"WHERE ep.episode_id=e.episode_id AND COALESCE(p.is_deleted,0)=0 AND {clause})",
        tenant_params,
    )
    counts["facts"] = count_rows(
        store,
        f"SELECT COUNT(*) AS n FROM fact_claims f WHERE LOWER(COALESCE(f.status,'active'))='active' AND {scope_sql}",
        tuple(scope_params),
    )
    if person_ids:
        placeholders = ",".join("?" for _ in person_ids)
        counts["profiles"] = count_rows(
            store,
            f"SELECT COUNT(DISTINCT person_id) AS n FROM person_profile_snapshots WHERE person_id IN ({placeholders})",
            tuple(person_ids),
        )
    try:
        runtime = await kernel.memory_runtime_admin(action="get_config")
        keys = (
            "data_dir",
            "embedding_dimension",
            "stored_vector_dimension",
            "vector_rebuild_required",
            "vector_rebuild_message",
            "embedding_degraded",
            "embedding_degraded_reason",
            "relation_vectors_enabled",
            "vector_pools_ready",
            "vector_pools_effective_mode",
            "paragraph_vector_backfill_pending",
            "paragraph_vector_backfill_failed",
            "auto_save",
        )
        payload["runtime"] = {key: runtime.get(key) for key in keys if key in runtime}
    except (ValueError, OSError) as exc:
        payload["runtime"] = {"error": f"{type(exc).__name__}: {exc}"}
    return payload


async def search_records(
    tenant: str,
    query: str = "",
    types: str = "",
    limit: int = 50,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """检索当前租户的段落、实体、关系、片段与事实。"""
    tenant = validate_tenant(tenant)
    selected = record_types(types)
    keyword = str(query or "").strip()
    pattern = sql_like(keyword)
    size = max(1, min(200, int(limit)))
    kernel = await _kernel()
    store = _metadata(kernel)
    persons = await list_persons(tenant)
    names = resolve_person_names(persons)
    rows_by_type: dict[str, list[dict[str, Any]]] = {}
    clause = tenant_clause("p")
    scope_sql, scope_params = scope_filter(tenant, [item["person_id"] for item in persons])
    inactive = 1 if include_inactive else 0

    if "paragraph" in selected:
        rows_by_type["paragraph"] = query_rows(
            store,
            "SELECT p.hash, p.content, p.source, p.knowledge_type, p.word_count, p.vector_index, "
            "p.created_at, p.updated_at, p.metadata, p.is_deleted FROM paragraphs p "
            f"WHERE (? = 1 OR COALESCE(p.is_deleted,0)=0) AND {clause} AND ("
            "? = '' OR LOWER(COALESCE(p.content,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(p.hash,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(p.source,'')) LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(p.updated_at, p.created_at, 0) DESC LIMIT ?",
            (inactive, *tenant_arguments(tenant), keyword, pattern, pattern, pattern, size),
        )
    if "entity" in selected:
        rows_by_type["entity"] = query_rows(
            store,
            "SELECT e.hash, e.name, e.appearance_count, e.vector_index, e.created_at, e.metadata, e.is_deleted, "
            "(SELECT COUNT(DISTINCT pe2.paragraph_hash) FROM paragraph_entities pe2 "
            " JOIN paragraphs p2 ON p2.hash=pe2.paragraph_hash WHERE pe2.entity_hash=e.hash "
            f" AND COALESCE(p2.is_deleted,0)=0 AND {clause.replace('p.', 'p2.')}) AS active_evidence_count "
            "FROM entities e WHERE (? = 1 OR COALESCE(e.is_deleted,0)=0) AND EXISTS ("
            "SELECT 1 FROM paragraph_entities pe JOIN paragraphs p ON p.hash=pe.paragraph_hash "
            f"WHERE pe.entity_hash=e.hash AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
            "AND (? = '' OR LOWER(COALESCE(e.name,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.hash,'')) LIKE ? ESCAPE '\\') "
            "ORDER BY e.appearance_count DESC, e.created_at DESC LIMIT ?",
            (*tenant_arguments(tenant), inactive, *tenant_arguments(tenant), keyword, pattern, pattern, size),
        )
    if "relation" in selected:
        rows_by_type["relation"] = query_rows(
            store,
            "SELECT r.hash, r.subject, r.predicate, r.object, r.confidence, r.source_paragraph, r.vector_state, "
            "r.created_at, r.last_reinforced, r.is_inactive, r.is_pinned, r.metadata, p.source AS origin_source "
            "FROM relations r LEFT JOIN paragraphs p ON p.hash=r.source_paragraph "
            "WHERE (? = 1 OR COALESCE(r.is_inactive,0)=0) AND EXISTS ("
            "SELECT 1 FROM paragraph_relations pr JOIN paragraphs p2 ON p2.hash=pr.paragraph_hash "
            f"WHERE pr.relation_hash=r.hash AND COALESCE(p2.is_deleted,0)=0 AND {clause.replace('p.', 'p2.')}) "
            "AND (? = '' OR LOWER(COALESCE(r.subject,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(r.predicate,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(r.object,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(r.hash,'')) LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(r.last_reinforced, r.created_at, 0) DESC LIMIT ?",
            (inactive, *tenant_arguments(tenant), keyword, pattern, pattern, pattern, pattern, size),
        )
    if "episode" in selected:
        rows_by_type["episode"] = query_rows(
            store,
            "SELECT e.episode_id, e.source, e.title, e.summary, e.paragraph_count, e.event_time_start, "
            "e.event_time_end, e.participants_json, e.keywords_json, e.created_at, e.updated_at FROM episodes e "
            "WHERE EXISTS (SELECT 1 FROM episode_paragraphs ep JOIN paragraphs p ON p.hash=ep.paragraph_hash "
            f"WHERE ep.episode_id=e.episode_id AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
            "AND (? = '' OR LOWER(COALESCE(e.title,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.summary,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.keywords_json,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.participants_json,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.episode_id,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(e.source,'')) LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(e.updated_at, e.event_time_end, e.created_at, 0) DESC LIMIT ?",
            (*tenant_arguments(tenant), keyword, pattern, pattern, pattern, pattern, pattern, pattern, size),
        )
    if "fact" in selected:
        rows_by_type["fact"] = query_rows(
            store,
            "SELECT f.* FROM fact_claims f "
            f"WHERE (? = 1 OR LOWER(COALESCE(f.status,'active'))='active') AND {scope_sql} "
            "AND (? = '' OR LOWER(COALESCE(f.fact_key,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(f.value_text,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(f.scope_id,'')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(f.claim_id,'')) LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(f.updated_at, f.last_confirmed_at, f.created_at, 0) DESC LIMIT ?",
            (inactive, *scope_params, keyword, pattern, pattern, pattern, pattern, size),
        )

    items = [record_view(kind, row, tenant, names) for kind, rows in rows_by_type.items() for row in rows]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    items = items[:size]
    return {
        "success": True,
        "query": keyword,
        "types": selected,
        "include_inactive": include_inactive,
        "limit": size,
        "count": len(items),
        "counts": {kind: sum(1 for item in items if item["type"] == kind) for kind in selected},
        "items": items,
    }


async def record_detail(record_type: str, record_id: str, tenant: str) -> dict[str, Any]:
    """单条记忆详情与相关原文。"""
    tenant = validate_tenant(tenant)
    kind = record_type.strip().lower()
    if kind not in _RECORD_TYPES:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"不支持的记忆类型: {record_type}")
    kernel = await _kernel()
    store = _metadata(kernel)
    row = detail_row(store, kind, record_id)
    persons = await list_persons(tenant)
    names = resolve_person_names(persons)
    return {
        "success": True,
        "record": record_view(kind, row, tenant, names),
        "detail": row,
        "paragraphs": related_paragraphs(store, kind, row, tenant),
    }


async def delete_record(record_type: str, record_id: str, tenant: str) -> dict[str, Any]:
    """删除段落/实体/关系，或撤回事实。"""
    tenant = validate_tenant(tenant)
    kind = record_type.strip().lower()
    token = str(record_id or "").strip()
    kernel = await _kernel()
    if kind == "episode":
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="Episode 由段落自动生成，不能单独删除")
    if kind == "fact":
        result = await kernel.memory_fact_admin(action="retract", claim_id=token, reason="console_delete")
        if not result.get("success"):
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail=str(result.get("error") or "事实不存在"))
        return {"deleted": True, "result": result}
    mode = {"paragraph": "paragraph", "entity": "entity", "relation": "relation"}.get(kind)
    if mode is None:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"不支持的记忆类型: {record_type}")
    result = await kernel.memory_delete_admin(
        action="execute",
        mode=mode,
        selector={"hashes": [token]},
        requested_by="fxbot_console",
        reason="console_delete",
    )
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "删除失败"))
    return {"deleted": True, "result": result}


async def list_sources(tenant: str) -> dict[str, Any]:
    """当前租户的记忆来源。"""
    tenant = validate_tenant(tenant)
    kernel = await _kernel()
    store = _metadata(kernel)
    clause = tenant_clause("p")
    rows = query_rows(
        store,
        "SELECT p.source, COUNT(*) AS count, MAX(p.created_at) AS last_updated FROM paragraphs p "
        f"WHERE COALESCE(p.is_deleted,0)=0 AND {clause} AND p.source IS NOT NULL AND p.source != '' "
        "GROUP BY p.source ORDER BY last_updated DESC",
        tuple(tenant_arguments(tenant)),
    )
    names = resolve_person_names(await list_persons(tenant))
    return {
        "items": [
            {
                "source": str(row.get("source") or ""),
                "source_label": source_label(str(row.get("source") or ""), tenant, names),
                "count": int(row.get("count") or 0),
                "last_updated": format_timestamp(row.get("last_updated")),
            }
            for row in rows
        ]
    }


async def list_episodes(tenant: str, query: str = "", limit: int = 50) -> dict[str, Any]:
    """当前租户的 Episode 列表。"""
    tenant = validate_tenant(tenant)
    size = max(1, min(200, int(limit)))
    keyword = str(query or "").strip()
    pattern = sql_like(keyword)
    kernel = await _kernel()
    store = _metadata(kernel)
    clause = tenant_clause("p")
    rows = query_rows(
        store,
        "SELECT e.episode_id, e.source, e.title, e.summary, e.paragraph_count, e.event_time_start, "
        "e.event_time_end, e.participants_json, e.keywords_json, e.created_at, e.updated_at FROM episodes e "
        "WHERE EXISTS (SELECT 1 FROM episode_paragraphs ep JOIN paragraphs p ON p.hash=ep.paragraph_hash "
        f"WHERE ep.episode_id=e.episode_id AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
        "AND (? = '' OR LOWER(COALESCE(e.title,'')) LIKE ? ESCAPE '\\' "
        "OR LOWER(COALESCE(e.summary,'')) LIKE ? ESCAPE '\\' "
        "OR LOWER(COALESCE(e.keywords_json,'')) LIKE ? ESCAPE '\\' "
        "OR LOWER(COALESCE(e.participants_json,'')) LIKE ? ESCAPE '\\') "
        "ORDER BY COALESCE(e.event_time_end, e.created_at, 0) DESC LIMIT ?",
        (*tenant_arguments(tenant), keyword, pattern, pattern, pattern, pattern, size),
    )
    names = resolve_person_names(await list_persons(tenant))
    return {"items": [episode_view(row, tenant, names) for row in rows]}


async def list_profiles(tenant: str, query: str = "", limit: int = 50) -> dict[str, Any]:
    """当前租户的人物画像。"""
    tenant = validate_tenant(tenant)
    persons = await list_persons(tenant)
    if not persons:
        return {"items": []}
    kernel = await _kernel()
    store = _metadata(kernel)
    person_ids = [item["person_id"] for item in persons]
    placeholders = ",".join("?" for _ in person_ids)
    snapshots = query_rows(
        store,
        "SELECT s.person_id, s.profile_version, s.profile_text, s.updated_at, s.expires_at, s.source_note "
        "FROM person_profile_snapshots s JOIN (SELECT person_id, MAX(profile_version) AS max_version "
        "FROM person_profile_snapshots GROUP BY person_id) latest "
        "ON latest.person_id=s.person_id AND latest.max_version=s.profile_version "
        f"WHERE s.person_id IN ({placeholders}) ORDER BY s.updated_at DESC",
        tuple(person_ids),
    )
    fact_counts = {
        str(row.get("scope_id") or ""): int(row.get("count") or 0)
        for row in query_rows(
            store,
            f"SELECT scope_id, COUNT(*) AS count FROM fact_claims WHERE scope_type='person' "
            f"AND LOWER(COALESCE(status,'active'))='active' AND scope_id IN ({placeholders}) GROUP BY scope_id",
            tuple(person_ids),
        )
    }
    by_id = {item["person_id"]: item for item in persons}
    keyword = str(query or "").strip().lower()
    items = []
    for row in snapshots:
        person_id = str(row.get("person_id") or "")
        person = by_id.get(person_id, {})
        text = str(row.get("profile_text") or "")
        name = str(person.get("person_name") or person.get("user_nickname") or person.get("user_id") or person_id)
        if keyword and keyword not in name.lower() and keyword not in person_id.lower() and keyword not in text.lower():
            continue
        items.append(
            {
                "person_id": person_id,
                "name": name,
                "user_id": str(person.get("user_id") or ""),
                "platform": str(person.get("platform") or ""),
                "profile_text": text,
                "profile_version": int(row.get("profile_version") or 0),
                "updated_at": format_timestamp(row.get("updated_at")),
                "expires_at": format_timestamp(row.get("expires_at")),
                "source_note": str(row.get("source_note") or ""),
                "fact_count": fact_counts.get(person_id, 0),
            }
        )
        if len(items) >= max(1, min(200, int(limit))):
            break
    return {"items": items}


async def profile_detail(person_id: str, tenant: str) -> dict[str, Any]:
    """人物画像详情：最新快照、别名与事实。"""
    tenant = validate_tenant(tenant)
    persons = await list_persons(tenant)
    by_id = {item["person_id"]: item for item in persons}
    person = by_id.get(str(person_id or "").strip())
    if person is None:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="人物不属于当前租户")
    kernel = await _kernel()
    store = _metadata(kernel)
    snapshots = query_rows(
        store,
        "SELECT person_id, profile_version, profile_text, aliases_json, evidence_ids_json, fact_claim_ids_json, "
        "updated_at, expires_at, source_note FROM person_profile_snapshots WHERE person_id = ? "
        "ORDER BY profile_version DESC LIMIT 1",
        (person["person_id"],),
    )
    override = query_rows(
        store,
        "SELECT aliases_json, updated_at, updated_by, source FROM person_profile_alias_overrides "
        "WHERE person_id = ? LIMIT 1",
        (person["person_id"],),
    )
    facts = query_rows(
        store,
        "SELECT * FROM fact_claims WHERE scope_type='person' AND scope_id = ? "
        "ORDER BY COALESCE(updated_at, last_confirmed_at, created_at, 0) DESC LIMIT 100",
        (person["person_id"],),
    )
    snapshot = snapshots[0] if snapshots else {}
    aliases = decode_json(snapshot.get("aliases_json"), [])
    if override:
        aliases = decode_json(override[0].get("aliases_json"), aliases)
    names = resolve_person_names(persons)
    return {
        "person": person,
        "profile": {
            "profile_version": int(snapshot.get("profile_version") or 0),
            "profile_text": str(snapshot.get("profile_text") or ""),
            "updated_at": format_timestamp(snapshot.get("updated_at")),
            "expires_at": format_timestamp(snapshot.get("expires_at")),
            "source_note": str(snapshot.get("source_note") or ""),
            "aliases": aliases if isinstance(aliases, list) else [],
            "alias_override": bool(override),
        },
        "facts": [fact_view(row, tenant, names) for row in facts],
    }


async def get_graph(tenant: str, limit: int = 200) -> dict[str, Any]:
    """当前租户的实体关系图。"""
    tenant = validate_tenant(tenant)
    size = max(1, min(500, int(limit)))
    kernel = await _kernel()
    store = _metadata(kernel)
    clause = tenant_clause("p")
    tenant_params = tuple(tenant_arguments(tenant))
    entities = query_rows(
        store,
        "SELECT e.hash, e.name, e.appearance_count FROM entities e WHERE COALESCE(e.is_deleted,0)=0 AND EXISTS ("
        "SELECT 1 FROM paragraph_entities pe JOIN paragraphs p ON p.hash=pe.paragraph_hash "
        f"WHERE pe.entity_hash=e.hash AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
        "ORDER BY e.appearance_count DESC LIMIT ?",
        (*tenant_params, size),
    )
    relations = query_rows(
        store,
        "SELECT r.hash, r.subject, r.predicate, r.object, r.confidence, r.last_reinforced FROM relations r "
        "WHERE COALESCE(r.is_inactive,0)=0 AND EXISTS ("
        "SELECT 1 FROM paragraph_relations pr JOIN paragraphs p ON p.hash=pr.paragraph_hash "
        f"WHERE pr.relation_hash=r.hash AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
        "ORDER BY COALESCE(r.last_reinforced, r.created_at, 0) DESC LIMIT ?",
        (*tenant_params, size * 4),
    )
    nodes: dict[str, dict[str, Any]] = {}
    for row in entities:
        name = str(row.get("name") or "").strip()
        if name:
            nodes[name] = {
                "id": name,
                "name": name,
                "hash": str(row.get("hash") or ""),
                "appearance_count": int(row.get("appearance_count") or 0),
            }
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for row in relations:
        subject = str(row.get("subject") or "").strip()
        obj = str(row.get("object") or "").strip()
        if not subject or not obj:
            continue
        nodes.setdefault(subject, {"id": subject, "name": subject, "hash": "", "appearance_count": 0})
        nodes.setdefault(obj, {"id": obj, "name": obj, "hash": "", "appearance_count": 0})
        key = (subject, obj)
        edge = edges.setdefault(
            key,
            {"source": subject, "target": obj, "predicates": [], "relation_hashes": [], "confidence": 0.0},
        )
        predicate = str(row.get("predicate") or "").strip()
        if predicate and predicate not in edge["predicates"]:
            edge["predicates"].append(predicate)
        edge["relation_hashes"].append(str(row.get("hash") or ""))
        edge["confidence"] = max(edge["confidence"], float(row.get("confidence") or 0.0))
    for edge in edges.values():
        edge["label"] = " / ".join(edge["predicates"])
        edge["relation_count"] = len(edge["relation_hashes"])
    return {
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "total_nodes": len(nodes),
        "total_edges": len(edges),
    }


async def graph_node(tenant: str, node: str) -> dict[str, Any]:
    """实体节点详情：相关关系与证据段落。"""
    tenant = validate_tenant(tenant)
    name = str(node or "").strip()
    if not name:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="node 不能为空")
    kernel = await _kernel()
    store = _metadata(kernel)
    clause = tenant_clause("p")
    tenant_params = tuple(tenant_arguments(tenant))
    entities = query_rows(
        store,
        "SELECT e.hash, e.name, e.appearance_count, e.metadata, e.created_at FROM entities e "
        "WHERE e.name = ? OR e.hash = ? LIMIT 1",
        (name, name),
    )
    relations = query_rows(
        store,
        "SELECT r.hash, r.subject, r.predicate, r.object, r.confidence, r.is_pinned FROM relations r "
        "WHERE (r.subject = ? OR r.object = ?) AND EXISTS ("
        "SELECT 1 FROM paragraph_relations pr JOIN paragraphs p ON p.hash=pr.paragraph_hash "
        f"WHERE pr.relation_hash=r.hash AND COALESCE(p.is_deleted,0)=0 AND {clause}) "
        "ORDER BY COALESCE(r.last_reinforced, r.created_at, 0) DESC LIMIT 50",
        (name, name, *tenant_params),
    )
    paragraphs = query_rows(
        store,
        "SELECT DISTINCT p.hash, p.content, p.source, p.created_at FROM paragraphs p "
        "JOIN paragraph_entities pe ON pe.paragraph_hash=p.hash "
        "JOIN entities e ON e.hash=pe.entity_hash "
        f"WHERE (e.name = ? OR e.hash = ?) AND COALESCE(p.is_deleted,0)=0 AND {clause} "
        "ORDER BY COALESCE(p.created_at, 0) DESC LIMIT 20",
        (name, name, *tenant_params),
    )
    return {
        "node": entities[0] if entities else {"name": name},
        "relations": relations,
        "paragraphs": [
            {
                "hash": str(row.get("hash") or ""),
                "content": str(row.get("content") or ""),
                "source": str(row.get("source") or ""),
                "created_at": format_timestamp(row.get("created_at")),
            }
            for row in paragraphs
        ],
    }


async def timeline(tenant: str, limit: int = 100) -> dict[str, Any]:
    """当前租户的记忆变更时间线，按时间倒序合并各来源。"""
    tenant = validate_tenant(tenant)
    size = limit_value(limit, 100, 300)
    kernel = await _kernel()
    store = _metadata(kernel)
    persons = await list_persons(tenant)
    names = resolve_person_names(persons)
    person_ids = [item["person_id"] for item in persons]
    clause = tenant_clause("p")
    tenant_params = tuple(tenant_arguments(tenant))
    per_source = min(150, max(30, size))
    events: list[dict[str, Any]] = []

    def add(
        category: str,
        event_type: str,
        title: str,
        summary: str,
        timestamp: Any,
        target_type: str,
        target_id: str,
    ) -> None:
        stamp = optional_float(timestamp)
        if stamp is None or stamp <= 0:
            return
        events.append(
            {
                "_ts": stamp,
                "id": f"{event_type}:{target_id}:{int(stamp)}",
                "category": category,
                "event_type": event_type,
                "title": label_text(title, 120),
                "summary": label_text(summary, 200),
                "timestamp": format_timestamp(stamp),
                "target": {"type": target_type, "id": target_id},
            }
        )

    for row in query_rows(
        store,
        "SELECT p.hash, p.content, p.source, p.created_at, p.updated_at, p.is_deleted FROM paragraphs p "
        f"WHERE {clause} ORDER BY COALESCE(p.updated_at, p.created_at, 0) DESC LIMIT ?",
        (*tenant_params, per_source),
    ):
        source = source_label(str(row.get("source") or ""), tenant, names)
        add(
            "paragraph",
            "paragraph_deleted" if int(row.get("is_deleted") or 0) else "paragraph_created",
            label_text(row.get("content"), 120) or "未命名段落",
            source,
            row.get("created_at") or row.get("updated_at"),
            "paragraph",
            str(row.get("hash") or ""),
        )
        created = optional_float(row.get("created_at"))
        updated = optional_float(row.get("updated_at"))
        if created and updated and updated > created + 1:
            add(
                "paragraph",
                "paragraph_updated",
                label_text(row.get("content"), 120) or "未命名段落",
                f"{source} · 内容更新",
                updated,
                "paragraph",
                str(row.get("hash") or ""),
            )

    for row in query_rows(
        store,
        "SELECT e.episode_id, e.title, e.summary, e.source, e.created_at, e.updated_at FROM episodes e "
        "WHERE EXISTS (SELECT 1 FROM episode_paragraphs ep JOIN paragraphs p ON p.hash=ep.paragraph_hash "
        f"WHERE ep.episode_id=e.episode_id AND {clause}) "
        "ORDER BY COALESCE(e.updated_at, e.created_at, 0) DESC LIMIT ?",
        (*tenant_params, per_source),
    ):
        add(
            "episode",
            "episode_created",
            str(row.get("title") or "未命名情景"),
            label_text(row.get("summary"), 180),
            row.get("created_at") or row.get("updated_at"),
            "episode",
            str(row.get("episode_id") or ""),
        )

    scope_sql, scope_params = scope_filter(tenant, person_ids)
    for row in query_rows(
        store,
        "SELECT f.claim_id, f.fact_key, f.value_text, f.status, f.created_at, f.updated_at FROM fact_claims f "
        f"WHERE {scope_sql} ORDER BY f.updated_at DESC LIMIT ?",
        (*scope_params, per_source),
    ):
        title = " ".join(
            item for item in (str(row.get("fact_key") or "").strip(), str(row.get("value_text") or "").strip()) if item
        )
        status = str(row.get("status") or "").strip().lower()
        add(
            "fact",
            "fact_retracted" if status == "retracted" else "fact_created",
            title or "人物事实",
            status,
            row.get("created_at") or row.get("updated_at"),
            "fact",
            str(row.get("claim_id") or ""),
        )

    hashes = tenant_hashes(store, tenant)
    operations = await kernel.memory_delete_admin(action="list_operations", limit=min(60, per_source))
    for summary in operations.get("items") or []:
        operation_id = str(summary.get("operation_id") or "")
        detail = await kernel.memory_delete_admin(action="get_operation", operation_id=operation_id)
        operation = detail.get("operation") if isinstance(detail.get("operation"), dict) else {}
        matched = operation_items(operation, hashes)
        if not matched:
            continue
        add(
            "delete",
            "memory_deleted",
            f"删除 {len(matched)} 条{mode_label(operation.get('mode'))}",
            str(operation.get("reason") or ""),
            operation.get("created_at"),
            "delete_operation",
            operation_id,
        )
        if operation.get("restored_at"):
            add(
                "delete",
                "memory_restored",
                "恢复删除操作",
                str(operation.get("reason") or ""),
                operation.get("restored_at"),
                "delete_operation",
                operation_id,
            )

    feedback = await kernel.memory_feedback_admin(action="list", limit=per_source)
    for task in feedback.get("items") or []:
        if str(task.get("session_id") or "") != tenant:
            continue
        add(
            "feedback",
            "feedback_correction",
            str(task.get("query_text") or "反馈纠错"),
            f"{task.get('task_status') or ''} · {task.get('decision') or ''}".strip(" ·"),
            task.get("created_at") or task.get("updated_at"),
            "feedback",
            str(task.get("task_id") or ""),
        )

    corrections = await kernel.memory_correction_admin(action="list", limit=per_source)
    for plan in corrections.get("items") or []:
        if (
            str(plan.get("target_chat_id") or "") != tenant
            and str(plan.get("target_person_id") or "") not in person_ids
        ):
            continue
        add(
            "correction",
            "memory_correction",
            str(plan.get("request_text") or "记忆修正"),
            f"{plan.get('scope') or ''} · {plan.get('status') or ''}".strip(" ·"),
            plan.get("created_at") or plan.get("updated_at"),
            "correction",
            str(plan.get("plan_id") or ""),
        )

    events.sort(key=lambda item: item["_ts"], reverse=True)
    return {"items": [{key: value for key, value in item.items() if key != "_ts"} for item in events[:size]]}


async def recycle_bin(tenant: str, limit: int = 50) -> dict[str, Any]:
    """当前租户可恢复的删除操作与已撤回事实。"""
    tenant = validate_tenant(tenant)
    size = limit_value(limit, 50, 200)
    kernel = await _kernel()
    store = _metadata(kernel)
    hashes = tenant_hashes(store, tenant)
    persons = await list_persons(tenant)
    scope_sql, scope_params = scope_filter(tenant, [item["person_id"] for item in persons])
    items: list[dict[str, Any]] = []

    operations = await kernel.memory_delete_admin(action="list_operations", limit=max(size * 4, 100))
    for summary in operations.get("items") or []:
        if len(items) >= size:
            break
        operation_id = str(summary.get("operation_id") or "")
        detail = await kernel.memory_delete_admin(action="get_operation", operation_id=operation_id)
        operation = detail.get("operation") if isinstance(detail.get("operation"), dict) else {}
        matched = operation_items(operation, hashes)
        if not matched:
            continue
        items.append(
            {
                "id": operation_id,
                "kind": "operation",
                "mode": str(operation.get("mode") or ""),
                "title": f"{mode_label(operation.get('mode'))}删除",
                "summary": str(operation.get("reason") or ""),
                "count": len(matched),
                "status": str(operation.get("status") or ""),
                "created_at": format_timestamp(operation.get("created_at")),
                "restored_at": format_timestamp(operation.get("restored_at")),
                "preview": [item_preview(item) for item in matched[:8]],
            }
        )

    for row in query_rows(
        store,
        "SELECT f.claim_id, f.fact_key, f.value_text, f.updated_at FROM fact_claims f "
        f"WHERE LOWER(COALESCE(f.status,'active'))='retracted' AND {scope_sql} "
        "ORDER BY f.updated_at DESC LIMIT ?",
        (*scope_params, size),
    ):
        items.append(
            {
                "id": str(row.get("claim_id") or ""),
                "kind": "fact",
                "mode": "fact",
                "title": label_text(
                    " ".join(
                        item
                        for item in (str(row.get("fact_key") or "").strip(), str(row.get("value_text") or "").strip())
                        if item
                    ),
                    120,
                )
                or "人物事实",
                "summary": "已撤回",
                "count": 1,
                "status": "retracted",
                "created_at": format_timestamp(row.get("updated_at")),
                "restored_at": None,
                "preview": [],
            }
        )

    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items[:size]}


async def restore_deleted(payload: dict[str, Any]) -> dict[str, Any]:
    """恢复删除操作或已撤回事实。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    kind = str(payload.get("kind") or "").strip().lower()
    token = str(payload.get("id") or "").strip()
    if not token:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="id 不能为空")
    kernel = await _kernel()
    if kind == "fact":
        store = _metadata(kernel)
        persons = await list_persons(tenant)
        scope_sql, scope_params = scope_filter(tenant, [item["person_id"] for item in persons])
        owned = query_rows(
            store,
            f"SELECT f.claim_id FROM fact_claims f WHERE f.claim_id = ? AND {scope_sql}",
            (token, *scope_params),
        )
        if not owned:
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="事实不属于当前会话")
        result = await kernel.memory_fact_admin(action="restore", claim_id=token, reason="console_restore")
    else:
        store = _metadata(kernel)
        detail = await kernel.memory_delete_admin(action="get_operation", operation_id=token)
        operation = detail.get("operation") if isinstance(detail.get("operation"), dict) else {}
        if not operation_items(operation, tenant_hashes(store, tenant)):
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="删除记录不属于当前会话")
        result = await kernel.memory_delete_admin(
            action="restore",
            operation_id=token,
            requested_by="fxbot_console",
            reason="console_restore",
        )
    if not result.get("success"):
        raise MemoryAdminError(
            kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or result.get("detail") or "恢复失败")
        )
    return {"success": True, "result": result}


async def purge_deleted(payload: dict[str, Any]) -> dict[str, Any]:
    """清理超过保留期的已删除记忆。"""
    validate_tenant(str(payload.get("tenant") or ""))
    try:
        grace_hours = float(payload.get("grace_hours", 24) or 24)
    except (TypeError, ValueError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="grace_hours 必须是数字") from exc
    kernel = await _kernel()
    result = await kernel.memory_delete_admin(action="purge", grace_hours=grace_hours, limit=1000)
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "清理失败"))
    return {"success": True, "result": result}


async def corrections(tenant: str, kind: str = "", limit: int = 50) -> dict[str, Any]:
    """反馈纠错任务与模糊修正计划。"""
    tenant = validate_tenant(tenant)
    size = limit_value(limit, 50, 200)
    kernel = await _kernel()
    persons = await list_persons(tenant)
    person_ids = [item["person_id"] for item in persons]
    selected = str(kind or "").strip().lower()
    items: list[dict[str, Any]] = []

    if selected in {"", "feedback"}:
        feedback = await kernel.memory_feedback_admin(action="list", limit=max(size * 2, 100))
        for task in feedback.get("items") or []:
            if str(task.get("session_id") or "") != tenant:
                continue
            items.append(
                {
                    "id": str(task.get("task_id") or ""),
                    "kind": "feedback",
                    "title": label_text(task.get("query_text"), 120) or "反馈纠错",
                    "status": str(task.get("task_status") or ""),
                    "decision": str(task.get("decision") or ""),
                    "rollback_status": str(task.get("rollback_status") or "none"),
                    "created_at": format_timestamp(task.get("created_at")),
                    "updated_at": format_timestamp(task.get("updated_at")),
                    "detail": task,
                }
            )

    if selected in {"", "plan"}:
        hashes = tenant_hashes(_metadata(kernel), tenant)
        plans = await kernel.memory_correction_admin(action="list", limit=max(size * 2, 100))
        for plan in plans.get("items") or []:
            owns_plan = tenant_owns_plan(plan, tenant, hashes)
            if not owns_plan and str(plan.get("target_person_id") or "") not in person_ids:
                continue
            items.append(
                {
                    "id": str(plan.get("plan_id") or ""),
                    "kind": "plan",
                    "title": label_text(plan.get("request_text"), 120) or "记忆修正",
                    "status": str(plan.get("status") or ""),
                    "decision": str(plan.get("scope") or ""),
                    "rollback_status": "-",
                    "created_at": format_timestamp(plan.get("created_at")),
                    "updated_at": format_timestamp(plan.get("updated_at")),
                    "detail": plan,
                }
            )

    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return {"items": items[:size]}


async def preview_correction(payload: dict[str, Any]) -> dict[str, Any]:
    """按勾选的记忆生成修改计划，等待确认后执行。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    request_text = str(payload.get("request_text") or "").strip()
    if not request_text:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="请填写修改内容")
    targets = correction_targets(payload.get("targets"))
    kernel = await _kernel()
    paragraphs, _, relations = tenant_hashes(_metadata(kernel), tenant)
    for target in targets:
        owned = paragraphs if target["type"] == "paragraph" else relations
        if target["id"] not in owned:
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="所选记忆不属于当前会话")
    result = await kernel.memory_correction_admin(
        action="preview",
        request_text=request_text,
        scope="memory",
        targets=targets,
        requested_by="fxbot_console",
        reason="console_batch_correction",
    )
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "生成修正计划失败"))
    return result


async def execute_correction(payload: dict[str, Any]) -> dict[str, Any]:
    """确认并执行修正计划。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="plan_id 不能为空")
    kernel = await _kernel()
    detail = await kernel.memory_correction_admin(action="get", plan_id=plan_id)
    plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
    if not plan or not tenant_owns_plan(plan, tenant, tenant_hashes(_metadata(kernel), tenant)):
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="修正计划不属于当前会话")
    result = await kernel.memory_correction_admin(
        action="execute",
        plan_id=plan_id,
        confirmed=True,
        requested_by="fxbot_console",
        reason="console_batch_correction",
    )
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "执行修正失败"))
    return result


async def rollback_correction(payload: dict[str, Any]) -> dict[str, Any]:
    """回滚一次反馈纠错或模糊修正。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    kind = str(payload.get("kind") or "").strip().lower()
    token = str(payload.get("id") or "").strip()
    if not token:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="id 不能为空")
    kernel = await _kernel()
    if kind == "feedback":
        try:
            task_id = int(token)
        except ValueError as exc:
            raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="反馈任务 id 无效") from exc
        detail = await kernel.memory_feedback_admin(action="get", task_id=task_id)
        task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
        if str(task.get("session_id") or "") != tenant:
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="纠正记录不属于当前会话")
        result = await kernel.memory_feedback_admin(
            action="rollback",
            task_id=task_id,
            requested_by="fxbot_console",
            reason=str(payload.get("reason") or "console_rollback"),
        )
    elif kind == "plan":
        detail = await kernel.memory_correction_admin(action="get", plan_id=token)
        plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
        persons = await list_persons(tenant)
        if str(plan.get("target_chat_id") or "") != tenant and str(plan.get("target_person_id") or "") not in {
            item["person_id"] for item in persons
        }:
            raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="纠正记录不属于当前会话")
        result = await kernel.memory_correction_admin(
            action="rollback",
            plan_id=token,
            requested_by="fxbot_console",
            reason=str(payload.get("reason") or "console_rollback"),
        )
    else:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="kind 必须是 feedback 或 plan")
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "回滚失败"))
    return {"success": True, "result": result}


async def list_bundles(limit: int = 50) -> dict[str, Any]:
    """已安装记忆包。"""
    kernel = await _kernel()
    result = await kernel.memory_bundle_admin(action="list", limit=limit_value(limit, 50, 200))
    for item in result.get("items") or []:
        item["installed_at"] = format_timestamp(item.get("installed_at"))
        item["updated_at"] = format_timestamp(item.get("updated_at"))
    return result


async def inspect_bundle(path: str) -> dict[str, Any]:
    """检查记忆包文件。"""
    kernel = await _kernel()
    try:
        result = await kernel.memory_bundle_admin(action="inspect", path=path)
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    return result


async def export_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """导出当前会话或全库记忆包。"""
    tenant = str(payload.get("tenant") or "").strip()
    selector = {"type": "chat", "chat_id": validate_tenant(tenant)} if tenant else {"type": "all"}
    kernel = await _kernel()
    try:
        result = await kernel.memory_bundle_admin(
            action="export",
            selector=selector,
            content_level=str(payload.get("content_level") or "knowledge"),
            include_images=True,
        )
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "导出失败"))
    return result


async def import_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """上传并安装记忆包。"""
    kernel = await _kernel()
    file_name = safe_file_name(payload.get("file_name") or "memory", ".amembundle")
    content = _decode_base64(payload.get("content_base64"), field="content_base64")
    target = _kernel_path(kernel, "imports", "bundles") / file_name
    target.write_bytes(content)
    tenant = str(payload.get("tenant") or "").strip()
    try:
        result = await kernel.memory_bundle_admin(
            action="import",
            path=str(target),
            mode=str(payload.get("mode") or "merge"),
            scope_type="chat" if tenant else "global",
            chat_id=validate_tenant(tenant) if tenant else "",
        )
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "安装失败"))
    return result


async def uninstall_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """卸载记忆包。"""
    installation_id = str(payload.get("installation_id") or "").strip()
    if not installation_id:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="installation_id 不能为空")
    kernel = await _kernel()
    try:
        result = await kernel.memory_bundle_admin(action="uninstall", installation_id=installation_id)
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "卸载失败"))
    return result


async def download_bundle(file_name: str) -> Download:
    """下载已导出的记忆包。"""
    kernel = await _kernel()
    try:
        result = await kernel.memory_bundle_admin(action="resolve_file", file_name=file_name)
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail=str(exc)) from exc
    path = Path(str(result.get("path") or ""))
    if not result.get("success") or not path.is_file():
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="记忆包不存在")
    return Download(path, media_type="application/zip", filename=path.name)


async def list_imports(limit: int = 50) -> dict[str, Any]:
    """导入任务列表与运行时限制。"""
    kernel = await _kernel()
    result = await kernel.memory_import_admin(action="list", limit=limit_value(limit, 50, 200))
    settings = await kernel.memory_import_admin(action="get_settings")
    return {**result, "settings": settings.get("settings") or {}}


async def import_settings() -> dict[str, Any]:
    """导入运行时限制。"""
    kernel = await _kernel()
    return await kernel.memory_import_admin(action="get_settings")


async def import_task(task_id: str, chunks: bool = False) -> dict[str, Any]:
    """导入任务详情。"""
    kernel = await _kernel()
    return await kernel.memory_import_admin(action="get", task_id=task_id, include_chunks=chunks)


async def paste_import(payload: dict[str, Any]) -> dict[str, Any]:
    """粘贴文本导入。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    content = str(payload.get("content") or "")
    if not content.strip():
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="content 不能为空")
    kernel = await _kernel()
    try:
        result = await kernel.memory_import_admin(
            action="create_paste",
            name=str(payload.get("name") or "paste.txt"),
            content=content,
            chat_id=tenant,
            scope_type="chat",
            input_mode=str(payload.get("input_mode") or "text"),
            llm_enabled=bool(payload.get("llm_enabled", True)),
        )
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    return result


async def upload_import(payload: dict[str, Any]) -> dict[str, Any]:
    """上传 txt/md/json 文本导入。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    raw_name = str(payload.get("file_name") or "upload.txt")
    file_name = safe_file_name(raw_name, Path(raw_name).suffix.lower())
    if Path(file_name).suffix.lower() not in {".txt", ".md", ".json"}:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="只支持 txt、md、json")
    content = _decode_base64(payload.get("content_base64"), field="content_base64")
    kernel = await _kernel()
    staging = _kernel_path(kernel, "imports", "staging")
    path = staging / f"{uuid.uuid4().hex}_{file_name}"
    path.write_bytes(content)
    try:
        result = await kernel.memory_import_admin(
            action="create_upload",
            staged_files=[{"path": str(path), "name": file_name}],
            chat_id=tenant,
            scope_type="chat",
            input_mode=str(payload.get("input_mode") or "text"),
            llm_enabled=bool(payload.get("llm_enabled", True)),
        )
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)
    return result


async def cancel_import(task_id: str) -> dict[str, Any]:
    """取消导入任务。"""
    kernel = await _kernel()
    return await kernel.memory_import_admin(action="cancel", task_id=task_id)


async def retry_import(task_id: str) -> dict[str, Any]:
    """重试导入任务中失败的片段。"""
    kernel = await _kernel()
    return await kernel.memory_import_admin(action="retry_failed", task_id=task_id)


async def list_images(tenant: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """当前会话的图片记忆资产。"""
    tenant = validate_tenant(tenant)
    kernel = await _kernel()
    try:
        result = await kernel.image_memory(
            action="list", limit=limit_value(limit, 50, 200), offset=max(0, int(offset)), chat_id=tenant
        )
    except RuntimeError as exc:
        return {"success": False, "error": str(exc), "items": []}
    runtime = getattr(kernel, "image_memory_runtime", None)
    store = getattr(runtime, "metadata_store", None)
    if store is not None:
        for item in result.get("items") or []:
            occurrences = store.list_image_occurrences_for_asset(str(item.get("asset_id") or ""))
            mine = [
                row
                for row in occurrences
                if str(row.get("chat_id") or "") == tenant and str(row.get("status") or "") == "active"
            ]
            item["occurrence_id"] = str(mine[0].get("occurrence_id") or "") if mine else ""
    return result


async def image_status() -> dict[str, Any]:
    """图片记忆运行时状态。"""
    kernel = await _kernel()
    try:
        return await kernel.image_memory(action="status")
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}


async def retry_images() -> dict[str, Any]:
    """重试失败的图片嵌入任务。"""
    kernel = await _kernel()
    try:
        return await kernel.image_memory(action="retry_failed_jobs")
    except RuntimeError as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc


async def upload_image(payload: dict[str, Any]) -> dict[str, Any]:
    """手动写入一张图片记忆。"""
    tenant = validate_tenant(str(payload.get("tenant") or ""))
    image = _decode_base64(payload.get("content_base64"), field="content_base64")
    kernel = await _kernel()
    try:
        result = await kernel.image_memory(
            action="ingest",
            image_bytes=image,
            source_kind="manual",
            external_ref=f"manual:{tenant}:{uuid.uuid4().hex}",
            scope_type="chat",
            chat_id=tenant,
            user_statement=str(payload.get("description") or ""),
        )
    except (RuntimeError, ValueError, TypeError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    return result


async def image_detail(asset_id: str, tenant: str, application: Application) -> dict[str, Any]:
    """单张图片的文件信息、来源记录、认知说明与关联记忆。"""
    tenant = validate_tenant(tenant)
    kernel = await _kernel()
    try:
        result = await kernel.image_memory(action="get", asset_id=asset_id, chat_ids=[tenant])
    except RuntimeError as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail=str(result.get("error") or "图片不存在"))
    occurrences = result.get("occurrences") or []
    for chat_id in {str(item.get("chat_id") or "") for item in occurrences}:
        if chat_id:
            name = await chat_name(chat_id, application)
            for item in occurrences:
                if str(item.get("chat_id") or "") == chat_id:
                    item["chat_name"] = name
    return result


async def delete_image(occurrence_id: str, tenant: str) -> dict[str, Any]:
    """删除一张图片的当前出现记录。"""
    tenant = validate_tenant(tenant)
    kernel = await _kernel()
    runtime = getattr(kernel, "image_memory_runtime", None)
    store = getattr(runtime, "metadata_store", None)
    occurrence = store.get_image_occurrence(occurrence_id) if store is not None else None
    if occurrence is None or str(occurrence.get("chat_id") or "") != tenant:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="图片出现记录不存在")
    try:
        result = await kernel.image_memory(action="delete_occurrence", occurrence_id=occurrence_id)
    except RuntimeError as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(exc)) from exc
    if not result.get("success"):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=str(result.get("error") or "删除失败"))
    return result


async def image_file(occurrence_id: str, tenant: str) -> Download:
    """返回图片原图，供控制台预览；支持查询参数 token。"""
    tenant = validate_tenant(tenant)
    kernel = await _kernel()
    runtime = getattr(kernel, "image_memory_runtime", None)
    store = getattr(runtime, "metadata_store", None)
    occurrence = store.get_image_occurrence(occurrence_id) if store is not None else None
    if occurrence is None or str(occurrence.get("chat_id") or "") != tenant:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="图片不存在")
    asset = store.get_image_asset(str(occurrence.get("asset_id") or ""))
    if asset is None:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="图片资产不存在")
    path = runtime.asset_store.path_for_read(str(asset.get("storage_key") or ""))
    if not path.is_file():
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="图片文件不存在")
    return Download(path, media_type=str(asset.get("mime_type") or "application/octet-stream"))


async def runtime_status() -> dict[str, Any]:
    """内核运行状态与向量能力。"""
    if not amemorix_runtime.available:
        return {"available": False, "error": amemorix_runtime.error, "config": await _config_payload()}
    kernel = await _kernel()
    result = await kernel.memory_runtime_admin(action="get_config")
    result.pop("config", None)
    return {**result, "available": True}


async def runtime_self_check() -> dict[str, Any]:
    """重新探测嵌入与向量通道。"""
    kernel = await _kernel()
    return await kernel.memory_runtime_admin(action="self_check")


async def _config_payload() -> dict[str, Any]:
    return amemorix_runtime.config().model_dump()


async def get_config() -> dict[str, Any]:
    """原始 A_Memorix 配置，字段与 MaiBot 一致。"""
    return {"config": await _config_payload()}


async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并保存配置；内核重启后生效。"""
    raw = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(raw, dict):
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="config 必须是对象")
    try:
        config = save_amemorix_config(raw)
    except (ValueError, OSError) as exc:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"配置校验失败: {exc}") from exc
    return {"config": config.model_dump(), "restart_required": True}

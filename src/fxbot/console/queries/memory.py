"""长期记忆的只读查询、缓存与展示投影，不依赖 HTTP。"""

from __future__ import annotations

import re
import json
import time
import asyncio
from typing import Any
from pathlib import Path
from weakref import WeakKeyDictionary
from datetime import datetime, timezone

from loguru import logger
from sqlmodel import select

from fxbot.core.errors import ActionFailed

from ...db import base_models
from ...core import Capability, UnsupportedCapability
from ...utils.tz import SHANGHAI_TZ
from ..services.errors import AdminFailure, MemoryAdminError
from ...core.application import Application
from ...chat.session_hooks import session_target
from ...chat.amemorix.models import ChatPerson

_RECORD_TYPES = ("paragraph", "entity", "relation", "episode", "fact")

_SOURCE_KIND_PREFIXES = (
    ("chat_summary:", "聊天摘要"),
    ("person_fact:", "人物事实"),
    ("chat_stream:", "聊天流"),
)

_chat_names: WeakKeyDictionary[Application, dict[str, tuple[float, str]]] = WeakKeyDictionary()


def validate_tenant(value: str) -> str:
    tenant = str(value or "").strip()
    if not tenant:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="tenant 不能为空")
    return tenant


async def chat_name(chat_id: str, application: Application) -> str:
    """用在线 Bot 查聊天显示名；离线或平台不支持时返回空串。"""
    try:
        target = session_target(chat_id)
    except ValueError:
        return ""
    names = _chat_names.setdefault(application, {})
    cached = names.get(chat_id)
    if cached and time.time() - cached[0] < (600 if cached[1] else 60):
        return cached[1]
    bot = application.get_bot(target.platform, target.bot_id)
    name = ""
    if bot is not None and Capability.TARGET_LOOKUP in bot.capabilities:
        try:
            name = await asyncio.wait_for(
                bot.capabilities.require(Capability.TARGET_LOOKUP).target_name(target), timeout=3
            )
        except (ActionFailed, UnsupportedCapability, TimeoutError, OSError):
            name = ""
    name = name.strip()
    names[chat_id] = (time.time(), name)
    return name


def format_timestamp(value: Any) -> str | None:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(SHANGHAI_TZ).isoformat()


def optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decode_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return fallback
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return fallback


def trim_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def sql_like(value: str) -> str:
    escaped = str(value or "").strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def tenant_clause(alias: str) -> str:
    """段落归属租户：metadata.chat_id 或 chat_summary 来源。"""
    return (
        f"((CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.chat_id') END) = ?"
        f" OR {alias}.source = ?)"
    )


def tenant_arguments(tenant: str) -> list[Any]:
    return [tenant, f"chat_summary:{tenant}"]


def source_label(source: str, tenant: str, person_names: dict[str, str]) -> str:
    token = str(source or "").strip()
    if not token:
        return ""
    if token == f"chat_summary:{tenant}":
        return "聊天摘要"
    if token.startswith("person_fact:"):
        return person_names.get(token.removeprefix("person_fact:").strip()) or "人物事实"
    for prefix, label in _SOURCE_KIND_PREFIXES:
        if token.startswith(prefix):
            return label
    return token


def resolve_person_names(persons: list[dict[str, Any]]) -> dict[str, str]:
    return {item["person_id"]: item["person_name"] or item["user_nickname"] or item["user_id"] for item in persons}


async def list_persons(tenant: str) -> list[dict[str, Any]]:
    if base_models.async_maker is None:
        return []
    statement = select(ChatPerson).where(ChatPerson.tenant == tenant).order_by(ChatPerson.updated_at.desc())
    async with base_models.async_maker() as session:
        rows = (await session.execute(statement)).scalars().all()
    return [
        {
            "person_id": row.person_id,
            "user_id": row.user_id,
            "platform": row.platform,
            "person_name": row.person_name,
            "user_nickname": row.user_nickname,
            "group_cardname": decode_json(row.group_cardname, []),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


def paragraph_view(row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    content = str(row.get("content") or "").strip()
    source = str(row.get("source") or "").strip()
    metadata = decode_json(row.get("metadata"), {})
    return {
        "type": "paragraph",
        "id": str(row.get("hash") or ""),
        "title": trim_text(content, 88) or source or "未命名段落",
        "summary": trim_text(content, 240),
        "source": source,
        "source_label": source_label(source, tenant, person_names),
        "status": "deleted" if int(row.get("is_deleted") or 0) else "active",
        "created_at": format_timestamp(row.get("created_at")),
        "updated_at": format_timestamp(row.get("updated_at")),
        "metadata": {
            "knowledge_type": str(row.get("knowledge_type") or "mixed"),
            "word_count": int(row.get("word_count") or 0),
            "vector_indexed": row.get("vector_index") is not None,
            "raw": metadata if isinstance(metadata, dict) else {},
        },
    }


def entity_view(row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    name = str(row.get("name") or row.get("hash") or "").strip()
    count = int(row.get("active_evidence_count") or 0)
    return {
        "type": "entity",
        "id": str(row.get("hash") or ""),
        "title": name,
        "summary": f"由 {count} 条段落支撑",
        "source": "",
        "source_label": "",
        "status": "deleted" if int(row.get("is_deleted") or 0) else "active",
        "created_at": format_timestamp(row.get("created_at")),
        "updated_at": None,
        "metadata": {
            "name": name,
            "appearance_count": int(row.get("appearance_count") or 0),
            "active_evidence_count": count,
            "vector_indexed": row.get("vector_index") is not None,
        },
    }


def relation_view(row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    subject = str(row.get("subject") or "").strip()
    predicate = str(row.get("predicate") or "").strip()
    obj = str(row.get("object") or "").strip()
    origin = str(row.get("origin_source") or "").strip()
    return {
        "type": "relation",
        "id": str(row.get("hash") or ""),
        "title": " ".join(item for item in (subject, predicate, obj) if item),
        "summary": f"置信度 {float(row.get('confidence') or 0.0):.2f}",
        "source": origin,
        "source_label": source_label(origin, tenant, person_names),
        "status": "inactive" if int(row.get("is_inactive") or 0) else "active",
        "created_at": format_timestamp(row.get("created_at")),
        "updated_at": format_timestamp(row.get("last_reinforced")),
        "metadata": {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "confidence": float(row.get("confidence") or 0.0),
            "vector_state": str(row.get("vector_state") or "none"),
            "is_pinned": bool(int(row.get("is_pinned") or 0)),
        },
    }


def episode_view(row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    source = str(row.get("source") or "").strip()
    return {
        "type": "episode",
        "id": str(row.get("episode_id") or ""),
        "title": str(row.get("title") or "").strip() or "未命名 Episode",
        "summary": trim_text(row.get("summary"), 240),
        "source": source,
        "source_label": source_label(source, tenant, person_names),
        "status": "active",
        "created_at": format_timestamp(row.get("created_at")),
        "updated_at": format_timestamp(row.get("updated_at")),
        "metadata": {
            "event_time_start": optional_float(row.get("event_time_start")),
            "event_time_end": optional_float(row.get("event_time_end")),
            "paragraph_count": int(row.get("paragraph_count") or 0),
            "keywords": decode_json(row.get("keywords_json"), []),
            "participants": decode_json(row.get("participants_json"), []),
        },
    }


def fact_view(row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    fact_key = str(row.get("fact_key") or "").strip()
    value_text = str(row.get("value_text") or "").strip()
    readable_key = "" if not fact_key or fact_key.startswith("statement:") else fact_key
    scope_type = str(row.get("scope_type") or "").strip()
    scope_id = str(row.get("scope_id") or "").strip()
    scope_label = person_names.get(scope_id, "") if scope_type == "person" else tenant
    scope_type_label = {"person": "人物", "chat": "聊天流"}.get(scope_type, scope_type)
    if readable_key and value_text:
        title = f"{readable_key}: {value_text}"
    else:
        title = value_text or readable_key or "未命名事实"
    return {
        "type": "fact",
        "id": str(row.get("claim_id") or ""),
        "title": title,
        "summary": f"{scope_type_label} · {scope_label}" if scope_label else scope_type_label,
        "source": "",
        "source_label": "",
        "status": str(row.get("status") or "active"),
        "created_at": format_timestamp(row.get("created_at")),
        "updated_at": format_timestamp(row.get("updated_at") or row.get("last_confirmed_at")),
        "metadata": {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "fact_key": fact_key,
            "value_text": value_text,
            "polarity": str(row.get("polarity") or ""),
            "cardinality": str(row.get("cardinality") or ""),
            "stability": str(row.get("stability") or ""),
            "profile_section": str(row.get("profile_section") or ""),
            "authority": str(row.get("authority") or ""),
            "confidence": float(row.get("confidence") or 0.0),
            "valid_from": format_timestamp(row.get("valid_from")),
            "valid_to": format_timestamp(row.get("valid_to")),
        },
    }


def record_view(record_type: str, row: dict[str, Any], tenant: str, person_names: dict[str, str]) -> dict[str, Any]:
    builders = {
        "paragraph": paragraph_view,
        "entity": entity_view,
        "relation": relation_view,
        "episode": episode_view,
        "fact": fact_view,
    }
    return builders[record_type](row, tenant, person_names)


def record_types(value: str) -> list[str]:
    requested = [item.strip().lower() for item in str(value or "").split(",") if item.strip()]
    if not requested:
        return list(_RECORD_TYPES)
    unknown = sorted(set(requested) - set(_RECORD_TYPES))
    if unknown:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail=f"不支持的记忆类型: {', '.join(unknown)}")
    return list(dict.fromkeys(requested))


def scope_filter(tenant: str, person_ids: list[str]) -> tuple[str, list[Any]]:
    sql = "(f.scope_type = 'chat' AND f.scope_id = ?"
    params: list[Any] = [tenant]
    if person_ids:
        sql += " OR (f.scope_type = 'person' AND f.scope_id IN (" + ",".join("?" for _ in person_ids) + "))"
        params.extend(person_ids)
    return sql + ")", params


def query_rows(store: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in store.query(sql, params)]
    except (ValueError, OSError) as exc:
        logger.opt(exception=True).warning("[A_Memorix] 控制台查询失败")
        raise MemoryAdminError(kind=AdminFailure.FAILED, detail="长期记忆查询失败") from exc


def limit_value(value: Any, default: int = 50, maximum: int = 300) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = default
    return max(1, min(maximum, size))


def label_text(value: Any, limit: int = 100) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def tenant_hashes(store: Any, tenant: str) -> tuple[set[str], set[str], set[str]]:
    """当前租户的段落、实体、关系哈希，用于判断全局操作是否触及本租户。"""
    clause = tenant_clause("p")
    params = tuple(tenant_arguments(tenant))
    paragraphs = {
        str(row.get("hash") or "")
        for row in query_rows(store, f"SELECT p.hash FROM paragraphs p WHERE {clause}", params)
    }
    entities = {
        str(row.get("entity_hash") or "")
        for row in query_rows(
            store,
            "SELECT DISTINCT pe.entity_hash FROM paragraph_entities pe "
            f"JOIN paragraphs p ON p.hash=pe.paragraph_hash WHERE {clause}",
            params,
        )
    }
    relations = {
        str(row.get("relation_hash") or "")
        for row in query_rows(
            store,
            "SELECT DISTINCT pr.relation_hash FROM paragraph_relations pr "
            f"JOIN paragraphs p ON p.hash=pr.paragraph_hash WHERE {clause}",
            params,
        )
    }
    return paragraphs, entities, relations


def operation_items(operation: dict[str, Any], hashes: tuple[set[str], set[str], set[str]]) -> list[dict[str, Any]]:
    paragraphs, entities, relations = hashes
    matched = []
    for item in operation.get("items") or []:
        kind = str(item.get("item_type") or "").strip()
        token = str(item.get("item_hash") or "").strip()
        if (
            (kind == "paragraph" and token in paragraphs)
            or (kind == "entity" and token in entities)
            or (kind == "relation" and token in relations)
        ):
            matched.append(item)
    return matched


def plan_candidates(plan: dict[str, Any]) -> list[dict[str, str]]:
    """修正计划里由控制台勾选产生的候选记录。"""
    preview = plan.get("preview") if isinstance(plan.get("preview"), dict) else {}
    items: list[dict[str, str]] = []
    for candidate in preview.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        kind = str(candidate.get("target_type") or "").strip()
        token = str(candidate.get("hash") or "").strip()
        if kind in {"paragraph", "relation"} and token:
            items.append({"type": kind, "id": token})
    return items


def tenant_owns_plan(plan: dict[str, Any], tenant: str, hashes: tuple[set[str], set[str], set[str]]) -> bool:
    """计划按聊天流归属，或全部候选记录都属于当前租户。"""
    if str(plan.get("target_chat_id") or "") == tenant:
        return True
    candidates = plan_candidates(plan)
    if not candidates:
        return False
    paragraphs, _, relations = hashes
    return all(item["id"] in (paragraphs if item["type"] == "paragraph" else relations) for item in candidates)


def item_preview(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("item_type") or "").strip()
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if kind == "paragraph":
        paragraph = payload.get("paragraph") if isinstance(payload.get("paragraph"), dict) else {}
        return {"type": kind, "label": label_text(paragraph.get("content"), 120)}
    if kind == "entity":
        entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
        return {"type": kind, "label": label_text(entity.get("name"), 120)}
    if kind == "relation":
        relation = payload.get("relation") if isinstance(payload.get("relation"), dict) else {}
        label = " ".join(str(relation.get(key) or "").strip() for key in ("subject", "predicate", "object"))
        return {"type": kind, "label": label_text(label, 120)}
    return {"type": kind, "label": str(item.get("item_key") or item.get("item_hash") or "")}


def mode_label(mode: Any) -> str:
    return {"paragraph": "段落", "entity": "实体", "relation": "关系"}.get(str(mode or ""), "记忆")


def safe_file_name(value: Any, suffix: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z._\-一-鿿]+", "-", Path(str(value or "")).stem).strip(".-")
    return f"{(stem or 'upload')[:80]}{suffix}"


def count_rows(store: Any, sql: str, params: tuple[Any, ...]) -> int:
    rows = query_rows(store, sql, params)
    return int(rows[0].get("n") or 0) if rows else 0


def detail_row(store: Any, record_type: str, record_id: str) -> dict[str, Any]:
    token = str(record_id or "").strip()
    if record_type == "paragraph":
        sql = "SELECT * FROM paragraphs WHERE hash = ? LIMIT 1"
        params: tuple[Any, ...] = (token,)
    elif record_type == "entity":
        sql = "SELECT * FROM entities WHERE hash = ? OR LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1"
        params = (token, token)
    elif record_type == "relation":
        sql = "SELECT * FROM relations WHERE hash = ? LIMIT 1"
        params = (token,)
    elif record_type == "episode":
        sql = "SELECT * FROM episodes WHERE episode_id = ? LIMIT 1"
        params = (token,)
    else:
        sql = "SELECT * FROM fact_claims WHERE claim_id = ? LIMIT 1"
        params = (token,)
    rows = query_rows(store, sql, params)
    if not rows:
        raise MemoryAdminError(kind=AdminFailure.NOT_FOUND, detail="记忆记录不存在")
    return rows[0]


def related_paragraphs(
    store: Any,
    record_type: str,
    row: dict[str, Any],
    tenant: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clause = tenant_clause("p")
    base = (
        "SELECT DISTINCT p.hash, p.content, p.source, p.created_at FROM paragraphs p "
        f"WHERE COALESCE(p.is_deleted,0)=0 AND {clause}"
    )
    tenant_params = tuple(tenant_arguments(tenant))
    if record_type == "paragraph":
        rows = query_rows(store, base + " AND p.hash = ? LIMIT ?", (*tenant_params, str(row.get("hash") or ""), limit))
    elif record_type == "entity":
        rows = query_rows(
            store,
            base + " AND p.hash IN (SELECT paragraph_hash FROM paragraph_entities WHERE entity_hash = ?) LIMIT ?",
            (*tenant_params, str(row.get("hash") or ""), limit),
        )
    elif record_type == "relation":
        rows = query_rows(
            store,
            base + " AND p.hash IN (SELECT paragraph_hash FROM paragraph_relations WHERE relation_hash = ?) LIMIT ?",
            (*tenant_params, str(row.get("hash") or ""), limit),
        )
    elif record_type == "episode":
        rows = query_rows(
            store,
            base + " AND p.hash IN (SELECT paragraph_hash FROM episode_paragraphs WHERE episode_id = ?) LIMIT ?",
            (*tenant_params, str(row.get("episode_id") or ""), limit),
        )
    else:
        rows = []
    return [
        {
            "hash": str(item.get("hash") or ""),
            "content": str(item.get("content") or ""),
            "source": str(item.get("source") or ""),
            "created_at": format_timestamp(item.get("created_at")),
        }
        for item in rows
    ]


def correction_targets(value: Any) -> list[dict[str, str]]:
    """校验勾选的记忆目标；批量修正只支持段落与关系。"""
    if not isinstance(value, list) or not value:
        raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="请至少选择一条记忆")
    targets: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="所选记忆格式无效")
        kind = str(item.get("type") or "").strip().lower()
        token = str(item.get("id") or "").strip()
        if kind not in {"paragraph", "relation"} or not token:
            raise MemoryAdminError(kind=AdminFailure.INVALID_INPUT, detail="只能修正段落或关系")
        if {"type": kind, "id": token} not in targets:
            targets.append({"type": kind, "id": token})
    return targets

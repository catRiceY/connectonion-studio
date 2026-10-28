"""Small, inspectable Chinese technical glossary, not translation or an LLM.

The original query is always retained. English identifiers remain byte-for-byte
in the additional query. No source, collection, version or authority is inferred.
"""
from __future__ import annotations

import re

POLICY_VERSION = "studio-zh-technical-glossary-v1"
# Freeze these broad concepts before evaluating the evidence holdout. A match
# adds vocabulary only: it does not assert that a document answers the question.
GLOSSARY = (
    (("断线", "断连", "掉线", "断开连接", "重新连接", "重连"), "reconnect disconnected connection"),
    (("聊天记录", "对话历史", "历史消息"), "conversation history messages"),
    (("会话", "继续对话", "继续聊天"), "session conversation"),
    (("恢复", "还原"), "restore resume"),
    (("刷新", "重新加载"), "page refresh reload"),
    (("自动连接", "自动重连"), "automatic reconnect"),
    (("缓存", "本地存储"), "cache localStorage"),
    (("审批", "批准", "拒绝"), "approval approve reject"),
    (("权限", "授权", "越权"), "permission authorization owner"),
    (("结构化输出", "结构化响应"), "structured output Pydantic"),
    (("提示词", "系统提示"), "system prompt"),
    (("工具调用", "调用工具"), "tool call"),
    (("参数", "必填字段"), "parameters schema arguments"),
    (("超时",), "timeout"),
    (("重试",), "retry retries"),
    (("取消",), "cancel cancellation"),
    (("压缩", "摘要"), "compression summary"),
    (("上下文",), "context"),
    (("日志", "执行轨迹"), "logs trace"),
    (("持久化", "保存"), "persist save"),
    (("文件",), "file"),
    (("格式",), "format"),
    (("找不到", "不存在"), "missing not found"),
    (("版本",), "version"),
)


def query_plan(query: str, *, enabled: bool = True) -> dict:
    terms, matched = [], []
    if enabled and re.search(r"[\u3400-\u9fff]", query):
        for phrases, english in GLOSSARY:
            present = [phrase for phrase in phrases if phrase in query]
            if present:
                matched.extend(present)
                terms.extend(english.split())
    # URLs, code names, versions and error codes are preserved, never generated.
    identifiers = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.:/@+\-]*", query)
    additional = " ".join(dict.fromkeys(identifiers + terms)) if terms else None
    variants = [query] + ([additional] if additional and additional != query else [])
    return {"policy": POLICY_VERSION if enabled else "original-only",
            "variants": variants, "matched_terms": matched,
            "expanded": len(variants) > 1,
            "note": "Local technical vocabulary expansion, not general translation or evidence verification"}


def with_next_passages(ranked: list[dict], rows: list[dict], k: int) -> list[dict]:
    """Experimental bounded context, retaining distinct original citations.

    Rows must come from the SAME filtered database snapshot as ranked. Only
    the next ordinal of the same document revision can supplement an anchor.
    At most k neighbour candidates and k returned passages; no text stitching.
    """
    by_position = {(row["doc_id"], row["version"], row["content_hash"], row["ordinal"]): row for row in rows}
    result, seen = [], set()
    for anchor in ranked[:k]:
        following = by_position.get((anchor["doc_id"], anchor["version"], anchor["content_hash"], anchor["ordinal"] + 1))
        candidates = [{**anchor, "retrieval_role": "anchor"}]
        if following:
            candidates.append({**following, "score": None, "retrieval_role": "adjacent_context",
                               "anchor_chunk_id": anchor["chunk_id"]})
        for row in candidates:
            if row["chunk_id"] not in seen and len(result) < k:
                result.append(row)
                seen.add(row["chunk_id"])
    return result

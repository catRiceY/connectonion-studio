"""Bounded context assembled from one scoped retrieval snapshot.

Original top-k anchors are immutable. A small supplemental budget first admits
independent supporting candidates, then completes nearby paragraph/code context.
No document names, evaluation labels or expected answers enter this policy.
"""
from __future__ import annotations

from collections import Counter
import json
import re

from .knowledge import KnowledgeError, bm25, tokenize

POLICY_VERSION = "snapshot-context-v2"
_STOPWORDS = frozenset("a an and are as at be been but by can could did do does for from has have how i if in into is it its may me my of on or our should so than that the their them then there these they this those to use using was we were what when where which who why will with would you your".split())
_IDENTITY = ("chunk_id", "doc_id", "version", "content_hash", "collection", "source_uri", "ordinal", "content", "line_start", "line_end")


def validate_limits(context_limit, max_context_bytes, max_total_bytes):
    if (type(context_limit) is not int or not 0 <= context_limit <= 12
            or type(max_context_bytes) is not int or not 0 <= max_context_bytes <= 24000
            or type(max_total_bytes) is not int or not 1024 <= max_total_bytes <= 64000):
        raise KnowledgeError("Invalid evidence context budget")


def _serialized_size(rows):
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _revision(row):
    return tuple(row[key] for key in ("collection", "doc_id", "version", "content_hash", "source_uri"))


def _focused_rankings(variants, rows):
    """Auxiliary lexical retrieval suppresses corpus boilerplate, not IDs.

    Numeric/version-only tokens and generic function words can dominate broad
    technical queries. Filtering them affects supplemental candidates only;
    original anchors, query text, scope and source versions remain unchanged.
    """
    frequencies = Counter(term for row in rows for term in set(tokenize(row["content"])))
    result = []
    for variant in variants:
        terms = list(dict.fromkeys(term for term in tokenize(variant)
            if term not in _STOPWORDS and not term.isdecimal()
            and frequencies[term] <= max(3, len(rows) // 4)))
        if terms:
            result.append(bm25(" ".join(terms), rows, 12))
    return result


def _fence_boundaries(rows):
    """Track Markdown fence state from each full revision, never chunk parity."""
    documents = {}
    for row in rows:
        documents.setdefault(_revision(row), []).append(row)
    boundaries = {}
    for document in documents.values():
        expected, opened = 0, None
        for row in sorted(document, key=lambda item: item["ordinal"]):
            if row["ordinal"] != expected:
                # Incomplete snapshots cannot establish fence state safely.
                break
            for line in row["content"].splitlines():
                marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
                if marker is None:
                    continue
                fence, tail = marker.groups()
                if opened is None:
                    opened = (fence[0], len(fence))
                elif fence[0] == opened[0] and len(fence) >= opened[1] and not tail.strip():
                    opened = None
            boundaries[row["chunk_id"]] = opened is not None
            expected += 1
    return boundaries


def _boundary_priority(anchor, neighbour, fence_boundaries=None):
    """Prefer continuation over a new section; keep both source chunks intact."""
    following = neighbour["ordinal"] > anchor["ordinal"]
    left_row, right_row = (anchor, neighbour) if following else (neighbour, anchor)
    left, right = left_row["content"], right_row["content"]
    last = left.rstrip().splitlines()[-1] if left.strip() else ""
    first = right.lstrip().splitlines()[0] if right.strip() else ""
    # A trailing heading/list introducer or text before the next heading is
    # stronger context than a new section. Never infer an open code block from
    # a partial chunk's fence parity: it may start inside a block, making its
    # only fence a closing delimiter rather than an opening one.
    open_fence = (fence_boundaries or {}).get(left_row["chunk_id"], False)
    continues = open_fence or bool(re.match(r"^\s*#{1,6}\s", last)) or last.endswith(":")
    new_section = bool(re.match(r"^\s*#{1,6}\s", first))
    return (0 if continues else 2 if new_section else 1, 0 if following else 1)


def expand_context(anchors, rows, *, ranking, channels, variants, context_limit=8,
                   max_context_bytes=12000, max_total_bytes=32000):
    validate_limits(context_limit, max_context_bytes, max_total_bytes)
    by_id = {row["chunk_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise KnowledgeError("Ambiguous evidence snapshot")
    scopes = {row["collection"] for row in rows}
    if len(scopes) > 1:
        raise KnowledgeError("Evidence context crosses collection scope")
    for anchor in anchors:
        current = by_id.get(anchor["chunk_id"])
        if current is None or any(current.get(key) != anchor.get(key) for key in _IDENTITY):
            raise KnowledgeError("Evidence anchor does not match snapshot")
    if len({row["chunk_id"] for row in anchors}) != len(anchors):
        raise KnowledgeError("Duplicate evidence anchor")
    result = [{**row, "retrieval_role": "anchor"} for row in anchors]
    if _serialized_size(result) > max_total_bytes:
        raise KnowledgeError("Ranked evidence exceeds the input context budget")
    seen = {row["chunk_id"] for row in anchors}
    text_bytes = 0
    supplement_count = 0
    skipped_for_budget = 0

    def append(row, role, **provenance):
        nonlocal text_bytes, supplement_count, skipped_for_budget
        if row["chunk_id"] in seen or supplement_count >= context_limit:
            return False
        value = {**row, "score": None, "retrieval_role": role, **provenance}
        size = len(row["content"].encode("utf-8"))
        if text_bytes + size > max_context_bytes or _serialized_size(result + [value]) > max_total_bytes:
            skipped_for_budget += 1
            return False
        result.append(value)
        seen.add(row["chunk_id"])
        supplement_count += 1
        text_bytes += size
        return True

    # A separate supporting-candidate reservation prevents all additional slots
    # being spent on a single long document. Strong per-channel hits survive
    # multi-query fusion; focused lexical hits remove general boilerplate.
    supporting_limit = min(3, context_limit // 2)
    candidate_lists = _focused_rankings(variants, rows) + [channel[:2] for channel in channels] + [ranking]
    candidates = []
    candidate_seen = set(seen)
    for depth in range(12):
        for channel in candidate_lists:
            if depth >= len(channel):
                continue
            identifier = channel[depth][0]
            if identifier in by_id and identifier not in candidate_seen:
                candidates.append(by_id[identifier])
                candidate_seen.add(identifier)
    supporting = []
    # Prefer different supporting documents in this reservation, without ever
    # removing existing anchors. A later pass can reuse a document if necessary.
    supporting_docs = set()
    for diverse in (True, False):
        for row in candidates:
            if len(supporting) >= supporting_limit:
                break
            if diverse and _revision(row) in supporting_docs:
                continue
            if append(row, "supporting_anchor"):
                supporting.append(row)
                supporting_docs.add(_revision(row))

    positions = {(*_revision(row), row["ordinal"]): row for row in rows}
    fence_boundaries = _fence_boundaries(rows)
    queues = []
    for anchor in [*anchors, *supporting]:
        adjacent = [positions.get((*_revision(anchor), anchor["ordinal"] + offset)) for offset in (1, -1)]
        adjacent = [row for row in adjacent if row is not None]
        queues.append((anchor, sorted(adjacent, key=lambda row: _boundary_priority(anchor, row, fence_boundaries))))
    # Round-robin nearest neighbours gives every anchor a chance before a
    # document's second neighbour; no unbounded recursive expansion.
    for side in range(2):
        for anchor, neighbours in queues:
            if side < len(neighbours):
                append(neighbours[side], "adjacent_context", anchor_chunk_id=anchor["chunk_id"])
    return result, {"context_policy": POLICY_VERSION, "anchor_count": len(anchors),
        "context_count": supplement_count, "supporting_anchor_count": len(supporting),
        "context_limit": context_limit, "context_text_bytes": text_bytes,
        "max_context_bytes": max_context_bytes, "evidence_json_bytes": _serialized_size(result),
        "max_total_bytes": max_total_bytes, "context_budget_skips": skipped_for_budget,
        "context_note": "Supplemental evidence is not part of ranked Recall@k; citation identity retained"}

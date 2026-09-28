"""Byte-bounded complete-line windows with honest continuation metadata."""

import json


def line_window(text, offset, max_bytes, *, has_more=False):
    lines = text.splitlines(keepends=True)
    kept = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8"))
        if used + size > max_bytes:
            break
        kept.append(line)
        used += size
    omitted = [offset] if lines and not kept else []
    more = has_more or len(kept) < len(lines)
    return "".join(kept), {
        "offset": offset, "lines": len(kept),
        "has_more": more, "truncated": more,
        "next_offset": offset + len(kept) + len(omitted) if more else None,
        **({"omitted_lines": omitted, "omission_reason": "line exceeds output byte limit"} if omitted else {}),
    }


def bound_line_payload(payload, max_bytes):
    """Keep pagination valid when the serialized tool envelope also needs space."""
    if not isinstance(payload, dict):
        return payload
    for batch_key in ("patches", "evidence_slices"):
        if isinstance(payload.get(batch_key), list):
            result = dict(payload)
            items = result[batch_key] = [dict(item) for item in payload[batch_key]]
            while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > max_bytes:
                candidates = [item for item in items if isinstance(item.get("range"), dict)
                              and (item.get("content") or item.get("patch"))]
                if not candidates:
                    break
                largest = max(candidates, key=lambda item: len(json.dumps(item)))
                size = len(json.dumps(largest, ensure_ascii=False).encode("utf-8"))
                largest.update(bound_line_payload(largest, size - 1))
                if "truncated" in largest:
                    largest["truncated"] = True
            return result
    if not isinstance(payload.get("range"), dict):
        return payload
    key = "content" if "content" in payload else "patch"
    if not isinstance(payload.get(key), str):
        return payload
    result = dict(payload)
    original_range = payload["range"]
    size = lambda: len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    while size() > max_bytes and result[key]:
        lines = result[key].splitlines(keepends=True)
        result[key] = "".join(lines[:-1])
        count = len(lines) - 1
        result["range"] = dict(original_range, lines=count, truncated=True, has_more=True,
                               next_offset=original_range["offset"] + count)
        if "returned_lines" in original_range:
            result["range"]["returned_lines"] = count
        if not count:
            result["range"].update(omitted_lines=[original_range["offset"]],
                                   omission_reason="line exceeds serialized output byte limit",
                                   next_offset=original_range["offset"] + 1)
    return result

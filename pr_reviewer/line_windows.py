"""Byte-bounded complete-line windows with honest continuation metadata."""

import hashlib
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
    if isinstance(payload.get('selection'), dict) and isinstance(payload.get('content'), str):
        result = json.loads(json.dumps(payload))
        size = lambda: len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        navigation = result.get('navigation', [])
        while navigation and size() > max_bytes:
            navigation.pop()
        selection = result['selection']
        passages = selection.get('passages', [])
        while passages and size() > max_bytes:
            removed = passages.pop()
            end = passages[-1]['end_line'] if passages else 0
            result['content'] = '\n'.join(result['content'].splitlines()[:end])
            selection['excerpted'] = result['truncated'] = True
            digest = hashlib.sha256(result['content'].encode('utf-8')).hexdigest()
            if 'content_hash' in result:
                result['content_hash'] = digest
            if isinstance(result.get('provenance'), dict):
                result['provenance']['truncated'] = True
                if 'content_hash' in result['provenance']:
                    result['provenance']['content_hash'] = digest
            if 'matched_lines' in removed:
                selection['returned_matches'] = max(0, selection.get('returned_matches', 0) - removed['matched_lines'])
                selection['omitted_matches'] = selection.get('omitted_matches', 0) + removed['matched_lines']
            else:
                selection.pop('returned_matches', None)
                selection.pop('omitted_matches', None)
            notice = 'additional passages omitted by serialized response budget'
            if notice not in selection.setdefault('limitations', []):
                selection['limitations'].append(notice)
        if size() > max_bytes:
            return {'error': 'response budget too small for passage metadata'}
        return result
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

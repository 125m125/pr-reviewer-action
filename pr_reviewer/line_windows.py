"""Byte-bounded complete-line windows with honest continuation metadata."""

import hashlib
import json


def split_source_lines(text):
    """Split on LF only, matching the streamed patch reader and Git line numbers."""
    *terminated, tail = text.split("\n")
    return [line + "\n" for line in terminated] + ([tail] if tail else [])


def source_budget_result(payload, max_bytes, *, web=False, remote=False):
    """Report a budget failure only when existing source content was omitted entirely."""
    if payload.get('content', '').strip():
        return payload
    page = payload.get('range', {})
    selection = payload.get('selection', {})
    if not (page.get('omitted_lines') or selection.get('omitted_matches', 0) > 0):
        return payload
    result = dict(payload, error='No readable content fit within the source budget.',
                  error_code='source_budget_exceeded', source_budget_bytes=max_bytes)
    if page:
        result['range'] = dict(page)
        if page.get('total_lines') is not None and (page.get('next_offset') or 0) > page['total_lines']:
            result['range']['next_offset'] = None
    result['hint'] = (
        'You might want to use more focused search_terms or a smaller documentation page.' if web else
        'You might want to choose a smaller remote source or use repository search if available.' if remote else
        'If you need specific information, you might want to use git_grep to retrieve match-centered excerpts.'
    ) + ' Excerpts do not establish exhaustive claims about omitted content.'
    return result


def line_window(text, offset, max_bytes, *, has_more=False, input_truncated=False,
                source_line_count=None):
    lines = split_source_lines(text)
    kept = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8"))
        if used + size > max_bytes:
            break
        kept.append(line)
        used += size
    omitted = [offset] if lines and not kept else []
    clipped = len(kept) < len(lines)
    structure_changed = source_line_count is not None and source_line_count != len(lines)
    # Coordinate consumers using splitlines() cannot safely anchor these excerpts.
    coordinates_lost = structure_changed or len(text.splitlines()) != len(lines)
    more = has_more or clipped or input_truncated
    next_offset = offset + len(kept) + len(omitted) if more else None
    if more and structure_changed:
        next_offset = None if clipped else offset + source_line_count
    return "".join(kept), {
        "offset": offset, "lines": len(kept),
        "has_more": more, "truncated": clipped or input_truncated or coordinates_lost,
        "next_offset": next_offset,
        **({"coordinates_unavailable": True} if coordinates_lost else {}),
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
        lines = split_source_lines(result[key])
        result[key] = "".join(lines[:-1])
        count = len(lines) - 1
        coordinates_lost = original_range.get("coordinates_unavailable")
        result["range"] = dict(original_range, lines=count, truncated=True, has_more=True,
                               next_offset=None if coordinates_lost else original_range["offset"] + count)
        if "returned_lines" in original_range:
            result["range"]["returned_lines"] = count
        if not count and not coordinates_lost:
            result["range"].update(omitted_lines=[original_range["offset"]],
                                   omission_reason="line exceeds serialized output byte limit",
                                   next_offset=original_range["offset"] + 1)
    return result

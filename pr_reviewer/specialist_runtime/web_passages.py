"""Bounded source passages, not semantic summaries or a browser layout engine."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import json
import re
from typing import Callable

from .evidence import mask_secrets


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    kind: str = "paragraph"


@dataclass(frozen=True)
class _Section:
    start: int
    end: int
    level: int
    title: str
    parent: int | None


@dataclass(frozen=True)
class NormalizedDocument:
    text: str
    blocks: tuple[_Block, ...]
    sections: tuple[_Section, ...]
    anchors: dict[str, int]
    links: tuple[dict, ...]


class _DocumentParser(HTMLParser):
    """Keep source block boundaries; never interpret CSS or execute content."""

    def __init__(self, check_deadline):
        super().__init__(convert_charrefs=True)
        self.check = check_deadline
        self.parts = []
        self.records = []
        self.kind = "paragraph"
        self.ignored = []
        self.nav = 0
        self.pre = False
        self.heading = 0
        self.pending_anchors = []
        self.anchors = {}
        self.links = []
        self.link = None
        self.table_header = False

    def flush(self):
        text = "".join(self.parts)
        self.parts = []
        text = text.strip("\n") if self.pre else " ".join(text.split())
        if text:
            if self.heading:
                text = "#" * self.heading + " " + text
            for anchor in self.pending_anchors:
                self.anchors.setdefault(anchor, len(self.records))
            self.pending_anchors = []
            self.records.append((self.kind, text))

    def handle_starttag(self, tag, attrs):
        self.check()
        attrs = dict(attrs)
        if self.ignored:
            if tag in {"script", "style", "noscript", "template"}:
                self.ignored.append(tag)
            return
        if tag in {"script", "style", "noscript", "template"}:
            self.ignored.append(tag)
            return
        if tag == "nav":
            self.flush()
            self.nav += 1
        if tag == "a" and attrs.get("href"):
            self.link = {"url": attrs["href"], "label": "", "record": len(self.records)}
        if self.nav:
            return
        if tag in {"p", "div", "pre", "li", "tr", "blockquote", "main", "article"} or re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.kind = {"pre": "code", "li": "list", "tr": "table", "blockquote": "quote"}.get(tag, "paragraph")
        if tag == "pre":
            self.pre = True
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = int(tag[1])
            self.kind = "heading"
        if tag == "tr":
            self.table_header = False
        if tag in {"td", "th"}:
            if self.parts:
                self.parts.append(" | ")
            if tag == "th":
                self.table_header = True
                self.kind = "table-header"
        if tag == "br":
            self.parts.append("\n" if self.pre else " ")
        anchor = attrs.get("id") or (attrs.get("name") if tag == "a" else None)
        if anchor:
            self.pending_anchors.append(anchor)

    def handle_endtag(self, tag):
        self.check()
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == "a" and self.link is not None:
            self.links.append(self.link)
            self.link = None
        if tag == "nav":
            self.nav = max(0, self.nav - 1)
            return
        if self.nav:
            return
        if tag in {"p", "div", "pre", "li", "tr", "blockquote", "main", "article"} or re.fullmatch(r"h[1-6]", tag):
            self.flush()
        if tag == "pre":
            self.pre = False
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = 0
        if tag in {"p", "pre", "li", "tr", "blockquote"} or re.fullmatch(r"h[1-6]", tag):
            self.kind = "paragraph"

    def handle_data(self, data):
        self.check()
        if self.ignored:
            return
        if self.link is not None:
            self.link["label"] += data
        if not self.nav:
            self.parts.append(data)


def normalize_document(text: str, mime_type: str, *, check_deadline: Callable[[], None]) -> NormalizedDocument:
    """Line coordinates are assigned after normalization and secret masking."""
    check_deadline()
    anchors = {}
    links = []
    html = mime_type in {"text/html", "application/xhtml+xml"}
    if html:
        parser = _DocumentParser(check_deadline)
        for offset in range(0, len(text), 16384):
            check_deadline()
            parser.feed(text[offset:offset + 16384])
        parser.close()
        parser.flush()
        lines, blocks, starts = [], [], []
        for kind, content in parser.records:
            check_deadline()
            if lines:
                lines.append("")
            start = len(lines) + 1
            starts.append(start)
            lines.extend(mask_secrets(content).splitlines())
            blocks.append(_Block(start, len(lines), kind))
        anchors = {key: starts[index] for key, index in parser.anchors.items() if index < len(starts)}
        links = [{"url": item["url"], "label": mask_secrets(item["label"]),
                  "line": starts[item["record"]] if item["record"] < len(starts) else 1}
                 for item in parser.links]
    else:
        lines = mask_secrets(text).splitlines()
        blocks = []
        start, fence = None, None
        for index, line in enumerate(lines, 1):
            check_deadline()
            fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line) if mime_type != "text/plain" else None
            if fence:
                if re.fullmatch(r"\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                    blocks.append(_Block(start, index, "code"))
                    start, fence = None, None
                continue
            if fence_match:
                if start:
                    blocks.append(_Block(start, index - 1))
                start, fence = index, fence_match[1]
                continue
            heading = mime_type != "text/plain" and re.match(r"^\s{0,3}#{1,6}\s+", line)
            setext = mime_type != "text/plain" and index < len(lines) and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", lines[index])
            if heading or (setext and line.strip()):
                if start:
                    blocks.append(_Block(start, index - 1))
                blocks.append(_Block(index, index + int(bool(setext)), "heading"))
                start = None
            elif blocks and blocks[-1].end == index and blocks[-1].kind == "heading":
                continue
            elif not line.strip():
                if start:
                    blocks.append(_Block(start, index - 1))
                    start = None
            elif start is None:
                start = index
            for label, url in re.findall(r"\[([^\]]+)\]\(([^\s)]+)\)", line):
                links.append({"url": url, "label": label, "line": index})
        if start:
            blocks.append(_Block(start, len(lines), "code" if fence else "paragraph"))
    sections, stack = [], []
    for block in blocks:
        check_deadline()
        if block.kind != "heading":
            continue
        line = lines[block.start - 1]
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.*)", line)
        level = len(match[1]) if match else (1 if lines[block.end - 1].lstrip().startswith("=") else 2)
        title = match[2] if match else line
        while stack and sections[stack[-1]][2] >= level:
            sections[stack.pop()][1] = block.start - 1
        sections.append([block.start, len(lines), level, title, stack[-1] if stack else None])
        stack.append(len(sections) - 1)
    check_deadline()
    return NormalizedDocument("\n".join(lines), tuple(blocks), tuple(_Section(*s) for s in sections), anchors, tuple(links))


@dataclass(frozen=True)
class PassageSelection:
    content: str
    selection: dict
    navigation: tuple[dict, ...] = ()

    def as_dict(self):
        return {"content": self.content, "selection": self.selection, "navigation": list(self.navigation)}


def validate_search_terms(value):
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8
            or any(not isinstance(term, str) or not term.strip() or len(term) > 128 for term in value)):
        raise ValueError("search_terms requires 1-8 nonempty strings of at most 128 characters")
    return tuple(dict.fromkeys(term.strip() for term in value))


def _merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if start > end:
            continue
        if result and start <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def select_passages(document: NormalizedDocument, *, search_terms: tuple[str, ...] | None,
                    fragment: str | None, max_bytes: int,
                    check_deadline: Callable[[], None]) -> PassageSelection:
    """Expand actual hits, never using added context as proof of relevance."""
    terms = validate_search_terms(search_terms)
    lines = document.text.splitlines()
    lower, upper = 1, len(lines)
    limitations = []
    if fragment is not None:
        anchor = document.anchors.get(fragment)
        if anchor is None:
            upper = 0
            limitations.append("unknown or unsupported anchor")
        else:
            containing = [s for s in document.sections if s.start <= anchor <= s.end]
            lower, upper = (containing[-1].start, containing[-1].end) if containing else (anchor, len(lines))
    folded = tuple(term.casefold() for term in terms or ())
    hits, counts, sizes = [], [0], [0]
    for number, line in enumerate(lines, 1):
        check_deadline()
        matched = lower <= number <= upper and (not folded or any(t in line.casefold() for t in folded))
        counts.append(counts[-1] + int(matched))
        sizes.append(sizes[-1] + len(json.dumps(line, ensure_ascii=False).encode("utf-8")) + 2)
        if matched and len(hits) < 256:
            hits.append(number)
    if not counts[-1] and not limitations:
        limitations.append("no literal matches in downloaded text; not proof of semantic absence")
    if counts[-1] > 256:
        limitations.append("selection considered the first 256 matching lines")
    digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()

    def render(ranges, navigation=()):
        check_deadline()
        chunks, passages, retained = [], [], 0
        for start, end in _merged(ranges):
            if chunks:
                chunks.append("[... omitted source lines ...]")
            output_start = sum(chunk.count("\n") + 1 for chunk in chunks) + 1
            chunks.append("\n".join(lines[start - 1:end]))
            passages.append({"start_line": output_start, "end_line": output_start + end - start,
                             "source_start_line": start, "source_end_line": end})
            retained += counts[end] - counts[start - 1]
        excerpted = _merged(ranges) != ([(1, len(lines))] if lines else [])
        return PassageSelection("\n".join(chunks), {
            "document_hash": digest, "excerpted": excerpted,
            "matched_lines": counts[-1], "returned_matches": retained,
            "omitted_matches": counts[-1] - retained, "passages": passages,
            "limitations": list(limitations),
        }, tuple(navigation))

    selected = []

    def fits(ranges):
        merged = _merged(ranges)
        if sum(sizes[end] - sizes[start - 1] for start, end in merged) > max_bytes:
            return False
        return len(json.dumps(render(merged).as_dict(), ensure_ascii=False).encode("utf-8")) <= max_bytes

    if not fits([]):
        raise ValueError("response budget too small for passage metadata")
    for hit in hits:
        check_deadline()
        if fits(selected + [(hit, hit)]):
            selected.append((hit, hit))
    admitted = {h for h in hits if any(a <= h <= b for a, b in selected)}
    if len(admitted) < len(hits):
        limitations.append("some matching lines omitted because of the response budget")
        while selected and not fits(selected):
            selected.pop()
        admitted = {h for h in hits if any(a <= h <= b for a, b in selected)}

    def add(start, end):
        nonlocal selected
        start, end = max(lower, start), min(upper, end)
        if start <= end and fits(selected + [(start, end)]):
            selected = _merged(selected + [(start, end)])
            return True
        return False

    blocks = document.blocks
    owners = {}
    for hit in sorted(admitted):
        check_deadline()
        containing = [i for i, s in enumerate(document.sections) if s.start <= hit <= s.end]
        owners[hit] = containing[-1] if containing else None
        for index, block in enumerate(blocks):
            if block.start <= hit <= block.end:
                add(block.start, block.end)
                if block.kind == "table":
                    for previous in reversed(blocks[:index]):
                        if previous.kind == "table-header":
                            add(previous.start, previous.end)
                            break
                        if previous.kind != "table":
                            break
                break
    children = {i: [] for i in range(len(document.sections))}
    for i, section in enumerate(document.sections):
        if section.parent is not None:
            children[section.parent].append(i)
    represented = {}
    for i in reversed(range(len(document.sections))):
        check_deadline()
        represented[i] = all(represented[c] for c in children[i]) if children[i] else i in owners.values()
        if represented[i]:
            section = document.sections[i]
            add(section.start, section.end)
    # Nearby blocks may expand only in the owner's direct body, not unmatched children.
    for hit, owner in owners.items():
        section = document.sections[owner] if owner is not None else None
        body_start = section.start if section else lower
        body_end = min((document.sections[c].start - 1 for c in children.get(owner, ())), default=section.end if section else upper)
        for block in sorted((b for b in blocks if body_start <= b.start <= b.end <= body_end),
                            key=lambda b: min(abs(hit - b.start), abs(hit - b.end))):
            check_deadline()
            add(block.start, block.end)
    # A real heading and ancestor introduction remain source; no synthetic breadcrumb quote.
    ancestors = set()
    for owner in owners.values():
        while owner is not None:
            ancestors.add(owner)
            owner = document.sections[owner].parent
    for owner in sorted(ancestors, reverse=True):
        section = document.sections[owner]
        intro_end = min((document.sections[c].start - 1 for c in children[owner]), default=section.start)
        add(section.start, intro_end)
    navigation = []
    for link in document.links:
        check_deadline()
        if len(navigation) >= 8:
            break
        if any(a <= link["line"] <= b for a, b in selected) or any(t in link['label'].casefold() for t in folded):
            proposed = navigation + [link]
            if len(json.dumps(render(selected, proposed).as_dict(), ensure_ascii=False).encode("utf-8")) <= max_bytes:
                navigation = proposed
    return render(selected, navigation)

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

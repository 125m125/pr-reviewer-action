from pathlib import Path

import pytest

from pr_reviewer.specialist_runtime import web_passages


FIXTURES = Path(__file__).parent / 'fixtures' / 'web_passages'


def document(name):
    mime = 'text/markdown' if name.endswith('.md') else 'text/html'
    return web_passages.normalize_document(
        (FIXTURES / name).read_text(encoding='utf-8'), mime,
        check_deadline=lambda: None,
    )


def test_normalized_blocks_preserve_source_structure():
    doc = document('fixtures-like.html')
    assert '    value: 1' in doc.text
    assert 'invisible secret' not in doc.text
    assert doc.anchors['options'] > 0
    table = document('timeouts-like.html')
    assert 'Operation | Duration' in table.text
    assert 'Upload | Long' in table.text
    assert 'Warning: timeout' in table.text
    bash = document('bash-like.html')
    assert 'Navigation noise' not in bash.text
    assert any(link['url'] == 'invocation.html#invoke' for link in bash.links)
    nested = document('announcement-like.html')
    assert 'Nested qualification' in nested.text


def test_heading_hierarchy_skips_levels_without_parsing_code_headings():
    doc = document('permissions-like.md')
    assert [s.title for s in doc.sections] == [
        'Workflow', 'Authentication', 'Token permissions', 'Uploading',
        'Downloading', 'Other credentials', 'Rotating', 'Expiring',
    ]
    html = document('fixtures-like.html')
    assert html.sections[2].parent == 1


def test_malformed_html_keeps_text_and_checks_deadline():
    doc = web_passages.normalize_document('<h2>Title</h2><p>Unclosed text', 'text/html', check_deadline=lambda: None)
    assert 'Unclosed text' in doc.text
    def expired():
        raise TimeoutError('deadline')
    with pytest.raises(TimeoutError):
        web_passages.normalize_document('x' * 100_000, 'text/plain', check_deadline=expired)

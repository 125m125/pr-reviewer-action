from pathlib import Path
import json

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


def select(doc, terms, budget=4000, fragment=None):
    return web_passages.select_passages(doc, search_terms=terms, fragment=fragment,
        max_bytes=budget, check_deadline=lambda: None)


def test_both_matching_siblings_promote_parent_if_it_fits():
    result = select(document('permissions-like.md'), ('artifacts',))
    assert 'This introduction qualifies both operations.' in result.content
    assert 'Upload artifacts safely.' in result.content
    assert 'Download artifacts safely.' in result.content
    assert 'Rotation is unrelated.' not in result.content
    assert result.selection['returned_matches'] == 2


def test_partial_other_branch_prevents_grandparent_promotion():
    result = select(document('permissions-like.md'), ('artifacts', 'Rotation'))
    assert 'Rotation is unrelated.' in result.content
    assert 'Expiry is unrelated.' not in result.content


def test_small_document_still_excludes_unmatched_sibling():
    result = select(document('permissions-like.md'), ('Upload',), budget=50_000)
    assert 'Upload artifacts safely.' in result.content
    assert 'Download artifacts safely.' not in result.content
    assert result.selection['excerpted'] is True


def test_expansion_does_not_create_matches():
    doc = web_passages.normalize_document('# Root\n## A\nneedle\n## B\nother', 'text/markdown', check_deadline=lambda: None)
    result = select(doc, ('needle',))
    assert 'other' not in result.content
    assert result.selection['matched_lines'] == 1


def test_large_first_hit_does_not_starve_late_match():
    doc = web_passages.normalize_document('# Root\n## A\nneedle early\n' + 'filler\n' * 500 + '\n## B\nneedle late', 'text/markdown', check_deadline=lambda: None)
    result = select(doc, ('needle',), budget=1100)
    assert 'needle early' in result.content and 'needle late' in result.content
    assert len(json.dumps(result.as_dict(), ensure_ascii=False).encode('utf-8')) <= 1100


def test_table_header_and_matching_row_keep_separate_ranges():
    result = select(document('timeouts-like.html'), ('Upload',), budget=1000)
    assert 'Operation | Duration' in result.content
    assert 'Upload | Long' in result.content
    for passage in result.selection['passages']:
        actual = result.content.splitlines()[passage['start_line'] - 1:passage['end_line']]
        source = document('timeouts-like.html').text.splitlines()[passage['source_start_line'] - 1:passage['source_end_line']]
        assert actual == source


@pytest.mark.parametrize('terms,fragment,expected', [
    (('argument',), 'invoke', 'command argument'),
    (('Expansion',), 'invoke', ''),
    (None, 'missing', ''),
])
def test_anchor_selection_and_no_matches(terms, fragment, expected):
    result = select(document('bash-like.html'), terms, fragment=fragment)
    if expected:
        assert expected in result.content
    else:
        assert result.content == ''
        assert result.selection['limitations']


def test_oversized_line_and_hit_limit_are_explicit():
    doc = web_passages.normalize_document('needle' + 'é' * 5000, 'text/plain', check_deadline=lambda: None)
    result = select(doc, ('needle',), budget=600)
    assert not result.content
    assert result.selection['omitted_matches'] == 1
    assert result.selection['limitations']
    many = web_passages.normalize_document('\n\n'.join('needle' for _ in range(300)), 'text/plain', check_deadline=lambda: None)
    result = select(many, ('needle',), budget=1500)
    assert result.selection['matched_lines'] == 300
    assert result.selection['returned_matches'] <= 256
    assert result.selection['omitted_matches'] == 300 - result.selection['returned_matches']
    assert len(json.dumps(result.as_dict(), ensure_ascii=False).encode('utf-8')) <= 1500


def test_conversation_budget_keeps_selection_json_and_source_boundaries():
    from pr_reviewer.conversation import Conversation
    result = select(document('permissions-like.md'), ('artifacts',))
    state = Conversation(system='test')
    payload = result.as_dict()
    payload['navigation'] = [{'label': 'x' * 2000}]
    state.add_tool_result('c', payload, max_bytes=900)
    visible = json.loads(state.events[-1]['content'])
    assert len(state.events[-1]['content'].encode()) <= 900
    assert visible['selection']['excerpted']
    for passage in visible['selection']['passages']:
        assert passage['end_line'] <= len(visible['content'].splitlines())

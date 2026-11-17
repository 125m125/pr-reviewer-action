from pr_reviewer.tool_executors import read_file, read_remote_file, execute_tool_request
from pr_reviewer.line_windows import source_budget_result


def test_local_oversized_line_is_actionable_tool_error(tmp_path):
    (tmp_path / 'large.txt').write_text('x' * 20000)
    result = read_file('large.txt', str(tmp_path))
    assert result['error_code'] == 'source_budget_exceeded'
    assert result['range']['omitted_lines'] == [1]
    assert result['range']['next_offset'] is None
    assert 'git_grep' in result['hint']
    tool = execute_tool_request('read_file', {'path': 'large.txt'}, str(tmp_path), [], '', [], 12000, 15)
    assert tool['status'] == 'error'
    assert tool['result']['error_code'] == 'source_budget_exceeded'


def test_web_budget_failure_is_not_a_no_match_result():
    missing = {'content': '', 'selection': {'matched_lines': 0, 'omitted_matches': 0}, 'truncated': True}
    assert source_budget_result(missing, 12000, web=True) == missing
    omitted = {'content': '', 'selection': {'matched_lines': 3, 'omitted_matches': 3}, 'truncated': True}
    result = source_budget_result(omitted, 12000, web=True)
    assert result['error_code'] == 'source_budget_exceeded'
    assert 'git_grep' not in result['hint']


def test_empty_file_and_partial_page_are_successful(tmp_path):
    (tmp_path / 'empty.txt').write_text('')
    assert 'error' not in read_file('empty.txt', str(tmp_path))
    (tmp_path / 'partial.txt').write_text('first\n' + 'x' * 20000)
    result = read_file('partial.txt', str(tmp_path))
    assert result['content'] == 'first\n'
    assert 'error' not in result


def test_remote_oversized_line_preserves_identity_and_reports_failure(monkeypatch):
    monkeypatch.setattr('pr_reviewer.platform.gh_api', lambda *args: {
        'data': {'type': 'file', 'size': 20000}})
    monkeypatch.setattr('pr_reviewer.platform.gh_raw_file', lambda *args: {'content': b'x' * 20000})
    result = read_remote_file('other/project', 'large.txt', 'a' * 40,
                              {'other/project'}, 'current/project')
    assert result['error_code'] == 'source_budget_exceeded'
    assert result['repository'] == 'other/project'
    assert result['resolved_sha'] == 'a' * 40
    assert result['range']['next_offset'] is None
    assert 'git_grep' not in result['hint']

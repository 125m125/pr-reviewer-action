import subprocess

from pr_reviewer.tool_executors import execute_tool_request, git_grep


def test_grep_keeps_late_regex_matches_and_redacts_secrets(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    text = ('x' * 5000 + ' Criminal Minds ' + 'y' * 5000
            + ' INVINCIBLE password="my private password" ' + 'z' * 5000)
    (tmp_path / 'fixture.json').write_text(text, encoding='utf-8')
    subprocess.run(['git', 'add', 'fixture.json'], cwd=tmp_path, check=True)
    result = git_grep(r'Criminal Minds\|INVINCIBLE', str(tmp_path))
    output = '\n'.join(result['matches'])
    assert 'Criminal Minds' in output
    assert 'INVINCIBLE' in output
    assert 'fixture.json:1:' in output
    assert '[excerpt' in output
    assert 'my private password' not in output
    assert len(output.encode('utf-8')) < 5000


def test_grep_short_lines_unchanged(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'short.txt').write_text('first\nneedle here\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'short.txt'], cwd=tmp_path, check=True)
    assert git_grep('needle', str(tmp_path)) == {'matches': ['short.txt:2:needle here']}


def test_grep_unicode_excerpt_survives_executor_budget(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'unicode.txt').write_text('界' * 5000 + 'needle' + '界' * 5000, encoding='utf-8')
    subprocess.run(['git', 'add', 'unicode.txt'], cwd=tmp_path, check=True)
    result = execute_tool_request('git_grep', {'pattern': 'needle'}, str(tmp_path),
                                  [], '', [], 12000, 15)
    assert result['status'] == 'ok'
    output = '\n'.join(result['result']['matches'])
    assert 'needle' in output
    assert '\ufffd' not in output
    assert len(output.encode('utf-8')) <= 12000

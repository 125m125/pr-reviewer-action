from types import SimpleNamespace
from pr_reviewer.specialist_runtime.controller import _critic_anchor_excerpt


def test_critic_sees_late_anchor_and_surrounding_permissions():
    content = '@@ -1,0 +100,65 @@\n' + ''.join(f'+ filler {i} ' + 'x' * 40 + '\n' for i in range(58))
    content += '+ owner: root\n+ group: root\n+ mode: u=r,g=r,o=\n+ no_log: true\n'
    record = SimpleNamespace(content=content, tool='read_pr_diff', source_path='playbook.yml', source_range=None)
    text = _critic_anchor_excerpt(record, 'playbook.yml:160')
    assert text is not None
    assert 'mode: u=r,g=r,o=' in text
    assert 'owner: root' in text
    assert len(text.encode('utf-8')) <= 1200
    assert _critic_anchor_excerpt(record, 'other.yml:160') is None
    assert _critic_anchor_excerpt(record, 'playbook.yml:999') is None


def test_critic_numbered_diff_does_not_confuse_left_and_right():
    record = SimpleNamespace(content='@@ -159 +160 @@\n- [LEFT 159 | RIGHT -] old\n+ [LEFT - | RIGHT 160] new\n',
                             tool='read_pr_diff', source_path='a.py', source_range=None)
    assert _critic_anchor_excerpt(record, 'a.py:159') is None
    assert 'new' in _critic_anchor_excerpt(record, 'a.py:160')


def test_critic_file_window_respects_source_offset():
    record = SimpleNamespace(content='before\nmode: u=r,g=r,o=\nafter\n', tool='read_file',
                             source_path='a.py', source_range={'offset': 158})
    assert 'mode:' in _critic_anchor_excerpt(record, 'a.py:159')
    assert _critic_anchor_excerpt(record, 'a.py:2') is None

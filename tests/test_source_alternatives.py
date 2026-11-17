from pr_reviewer.specialist_runtime.policy import SourceRule
from pr_reviewer.specialist_runtime.web_evidence import SourcePolicy
from pr_reviewer.tool_executors import execute_tool_request
from pr_reviewer.specialist_runtime.adjudication import _source_note, _source_request
from pr_reviewer.specialist_runtime.web_evidence import SearchCandidate, source_access_request
from pr_reviewer.specialist_runtime.session import SpecialistSession


def test_alternatives_rank_exact_siblings_tld_and_similar_names():
    policy = SourcePolicy(tuple(SourceRule(host=h, path_prefixes=('/docs',),
                                         classification='official-docs') for h in (
        'unrelated.example', 'moviedb.org', 'themoviedb.com',
        'docs.themoviedb.org', 'developer.themoviedb.org')))
    hints = policy.authorized_alternatives('https://developer.themoviedb.org/private')
    assert [x['host'] for x in hints] == [
        'developer.themoviedb.org', 'docs.themoviedb.org', 'themoviedb.com']
    assert hints[0]['classification'] == 'official-docs'
    assert hints[0]['path_prefixes'] == ['/docs']
    assert hints[0]['content_verified'] is False
    assert not policy.classify('https://developer.themoviedb.org/private').approved
    assert policy.authorized_alternatives('https://totally-unrelated.invalid') == []


def test_alternatives_public_suffix_and_domain_name_similarity():
    policy = SourcePolicy((SourceRule(host='docs.other.co.uk'),
                           SourceRule(host='docs.themoviedb.co.uk'),
                           SourceRule(host='moviedb.org')))
    hints = policy.authorized_alternatives('https://developer.themoviedb.co.uk/docs')
    assert [x['host'] for x in hints] == ['docs.themoviedb.co.uk', 'moviedb.org']


def test_alternatives_do_not_reflect_unsafe_url_payloads():
    policy = SourcePolicy((SourceRule(host='docs.themoviedb.org'),))
    assert policy.authorized_alternatives('https://user:password@themoviedb.org/docs') == []


def test_public_suffix_wildcards_exceptions_and_private_domains():
    from pr_reviewer.specialist_runtime.source_alternatives import domain_parts

    assert domain_parts('docs.example.co.uk') == ('example', 'co.uk')
    assert domain_parts('www.city.kawasaki.jp') == ('city', 'kawasaki.jp')
    assert domain_parts('a.b.kawasaki.jp') == ('a', 'b.kawasaki.jp')
    assert domain_parts('docs.project.github.io') == ('project', 'github.io')


def test_denial_hints_round_trip_into_access_comment(tmp_path):
    policy = SourcePolicy((SourceRule(host='developer.themoviedb.org',
                                     path_prefixes=('/reference', '/docs'),
                                     classification='official-tmdb-documentation'),))
    url = 'https://developers.themoviedb.org/3/persons/get-person-combined-credits'
    result = execute_tool_request('web_fetch', {'url': url}, str(tmp_path),
                                  [], '', [], 12000, 15, source_policy=policy,
                                  search_url='https://search.example/search')
    assert result['status'] == 'rejected'
    payload = result['result']
    assert payload['authorized_alternatives'][0]['host'] == 'developer.themoviedb.org'
    assert 'might want to use web_search' in payload['hint']
    session = SpecialistSession.__new__(SpecialistSession)
    session.source_access_requests = ()
    session._record_source_access_requests('web_fetch', {'url': url}, result, ('O1',),
                                           model_purpose='Verify episode_count')
    assert len(session.source_access_requests) == 1
    assert list(session.source_access_requests[0].authorized_alternatives) == payload['authorized_alternatives']
    request = source_access_request(SearchCandidate(title=None, snippet=None, url=url),
                                    'O1', 'Verify episode_count',
                                    authorized_alternatives=payload['authorized_alternatives'])
    restored = _source_request(request.as_dict())
    assert restored == request
    note = _source_note(restored, obligations={'O1': object()})
    assert 'official-tmdb-documentation' in note.markdown
    assert '/reference' in note.markdown
    assert 'not verified replacements' in note.markdown
    assert url in note.markdown
    assert 'retrieval remains pending approval' in note.markdown


def test_no_search_hint_without_search_and_no_hints_for_security_denial(tmp_path):
    policy = SourcePolicy((SourceRule(host='docs.themoviedb.org'),))
    for url, alternatives in [('https://developer.themoviedb.org/docs', True),
                              ('https://user:secret@themoviedb.org/docs', False)]:
        result = execute_tool_request('web_fetch', {'url': url}, str(tmp_path),
                                      [], '', [], 12000, 15, source_policy=policy)
        assert result['status'] == 'rejected'
        assert bool(result['result'].get('authorized_alternatives')) == alternatives
        assert 'web_search' not in result['result'].get('hint', '')

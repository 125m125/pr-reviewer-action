"""Navigation hints only; never used to authorize a source."""

from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _suffix_rules():
    # Bundled official PSL snapshot; no network dependency during a review.
    return frozenset(
        line.encode('idna').decode('ascii').lower()
        for raw in Path(__file__).with_name('public_suffix_list.dat').read_text(encoding='utf-8').splitlines()
        if (line := raw.strip()) and not line.startswith('//')
    )


def domain_parts(host):
    labels = host.split('.')
    rules = _suffix_rules()
    suffix_size = 1
    for index in range(len(labels)):
        suffix = '.'.join(labels[index:])
        if '!' + suffix in rules:
            suffix_size = len(labels) - index - 1
            break
        if suffix in rules or (index + 1 < len(labels)
                               and '*.' + '.'.join(labels[index + 1:]) in rules):
            suffix_size = max(suffix_size, len(labels) - index)
    if len(labels) <= suffix_size:
        return '', host
    return labels[-suffix_size - 1], '.'.join(labels[-suffix_size:])


def rank_host(requested, candidate):
    if requested == candidate:
        return 0, 1.0, 'exact_hostname'
    name, suffix = domain_parts(requested)
    other, other_suffix = domain_parts(candidate)
    if not name or not other:
        return None
    if name == other:
        return (1 if suffix == other_suffix else 2), 1.0, (
            'same_registrable_domain' if suffix == other_suffix else 'same_domain_name')
    similarity = SequenceMatcher(None, name, other, autojunk=False).ratio()
    if min(len(name), len(other)) >= 5 and similarity >= 0.8:
        return 3, similarity, 'similar_domain_name'
    return None

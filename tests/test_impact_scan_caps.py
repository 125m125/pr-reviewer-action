from scripts.cap_impact_scan import cap_impact_lines


def test_huge_json_impact_match_is_deterministically_capped():
    huge = 'fixtures/tmdb.json:1:{"payload":"' + ("x" * 50000) + '"}\n'
    lines = [huge] + [f"fixtures/tmdb.json:{i}:small\n" for i in range(2, 20)]
    first, details = cap_impact_lines(
        lines, per_match_bytes=200, per_file_matches=3, total_bytes=700
    )
    second, details_again = cap_impact_lines(
        lines, per_match_bytes=200, per_file_matches=3, total_bytes=700
    )
    assert first == second
    assert details == details_again
    assert len(first.encode("utf-8")) <= 700
    assert "[match truncated]" in first
    assert "impact scan capped" in first
    assert details["truncated"] is True


def test_cap_accepts_streams_and_never_exceeds_tiny_total():
    output, details = cap_impact_lines(
        iter(["a.json:1:" + ("x" * 1000)]),
        per_match_bytes=100,
        per_file_matches=1,
        total_bytes=32,
    )
    assert len(output.encode("utf-8")) <= 32
    assert details["truncated"] is True

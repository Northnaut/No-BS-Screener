from app.parsers.reddit import _extract_rss_body_text


def test_simple_text_post_body():
    content = '<!-- SC_OFF --><div class="md"><p>Hello world</p></div><!-- SC_ON --><span><a href="#">[link]</a></span>'
    assert _extract_rss_body_text(content) == "Hello world"


def test_link_or_image_post_has_no_markers_returns_empty():
    # Link/image posts have no SC_OFF/SC_ON wrapping at all.
    content = '<a href="https://example.com/image.jpg">image.jpg</a>'
    assert _extract_rss_body_text(content) == ""


def test_empty_content_returns_empty():
    assert _extract_rss_body_text("") == ""


def test_none_content_returns_empty():
    assert _extract_rss_body_text(None) == ""


def test_deeply_nested_divs_are_not_truncated_early():
    # A naive `<div class="md">(.*?)</div>` regex would stop at the FIRST inner </div>,
    # truncating everything after the spoiler tag. Slicing on SC_OFF/SC_ON must capture it all.
    content = (
        '<!-- SC_OFF --><div class="md">'
        '<div class="spoiler"><div><table><tr><td>deep <div>nested</div> content</td></tr></table></div></div>'
        '<p>end of body</p>'
        '</div><!-- SC_ON --><span><a href="/message/compose">[link]</a></span>'
    )
    result = _extract_rss_body_text(content)
    assert "deep" in result
    assert "nested" in result
    assert "content" in result
    assert "end of body" in result


def test_nested_table_markup_fully_preserved_as_text():
    content = (
        '<!-- SC_OFF --><div class="md">'
        '<table><thead><tr><th>Col A</th><th>Col B</th></tr></thead>'
        '<tbody><tr><td>1</td><td>2</td></tr></tbody></table>'
        '</div><!-- SC_ON -->'
    )
    result = _extract_rss_body_text(content)
    for token in ("Col A", "Col B", "1", "2"):
        assert token in result


def test_html_entities_are_unescaped():
    # This is the value as feedparser hands it to us (already XML-decoded once), so entities
    # here are single-escaped HTML entities, e.g. "&#39;" not the double-escaped "&amp;#39;"
    # that appears in the raw XML source before feedparser's own XML parsing step.
    content = '<!-- SC_OFF --><div class="md"><p>It&#39;s &quot;great&quot; &amp; simple</p></div><!-- SC_ON -->'
    result = _extract_rss_body_text(content)
    assert result == 'It\'s "great" & simple'


def test_whitespace_is_collapsed_to_single_spaces():
    content = '<!-- SC_OFF --><div class="md">\n\n<p>Line one</p>\n\n\n<p>Line   two</p>\n</div><!-- SC_ON -->'
    result = _extract_rss_body_text(content)
    assert "  " not in result
    assert result == "Line one Line two"


def test_extremely_large_body_is_not_truncated():
    big_paragraph = "word " * 200_000  # ~1MB of raw text
    content = f'<!-- SC_OFF --><div class="md"><p>{big_paragraph}</p></div><!-- SC_ON -->'
    result = _extract_rss_body_text(content)
    # 200,000 repetitions of "word " -> 200,000 occurrences of the token "word" preserved.
    assert result.count("word") == 200_000
    assert result.startswith("word word word")


def test_on_marker_before_off_marker_returns_empty():
    # Pathological ordering: SC_ON appears before SC_OFF -> must not slice backwards or crash.
    content = '<!-- SC_ON --><div class="md">body</div><!-- SC_OFF -->'
    assert _extract_rss_body_text(content) == ""


def test_missing_sc_on_marker_returns_empty():
    content = '<!-- SC_OFF --><div class="md">body without a closing marker</div>'
    assert _extract_rss_body_text(content) == ""


def test_missing_sc_off_marker_returns_empty():
    content = '<div class="md">body without an opening marker</div><!-- SC_ON -->'
    assert _extract_rss_body_text(content) == ""


def test_body_without_md_div_wrapper_still_extracts_text():
    # Markers present but the inner content doesn't start with the expected md div (defensive
    # case - should still degrade gracefully rather than raise).
    content = '<!-- SC_OFF --><p>raw paragraph, no md wrapper</p><!-- SC_ON -->'
    result = _extract_rss_body_text(content)
    assert "raw paragraph, no md wrapper" in result

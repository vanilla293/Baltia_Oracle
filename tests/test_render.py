"""Рендер: markdown модели → HTML Telegram, нарезка длинных ответов, плоский текст."""
from __future__ import annotations

import pytest

from oracle.bot.render import (HR_LINE, escape, md_to_html, plain, split_message, well_formed)


# ── md_to_html: экранирование ────────────────────────────────────────────────
def test_escapes_html_specials():
    assert md_to_html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert md_to_html("<b>не тег</b>") == "&lt;b&gt;не тег&lt;/b&gt;"


def test_empty_and_plain_text():
    assert md_to_html("") == ""
    assert md_to_html(None) == ""  # type: ignore[arg-type]
    assert md_to_html("Просто текст.") == "Просто текст."


def test_placeholder_chars_in_input_do_not_leak():
    assert md_to_html("a\uE0000\uE001b") == "a0b"


# ── строчная разметка ────────────────────────────────────────────────────────
@pytest.mark.parametrize("src, want", [
    ("**жирный**", "<b>жирный</b>"),
    ("__жирный__", "<b>жирный</b>"),
    ("*курсив*", "<i>курсив</i>"),
    ("_курсив_", "<i>курсив</i>"),
    ("~~зачёркнуто~~", "<s>зачёркнуто</s>"),
    ("Это **важно**, понял?", "Это <b>важно</b>, понял?"),
    ("(*в скобках*)", "(<i>в скобках</i>)"),
    ("***жирный курсив***", "<b><i>жирный курсив</i></b>"),
    ("**жирный с _курсивом_ внутри**", "<b>жирный с <i>курсивом</i> внутри</b>"),
])
def test_inline_emphasis(src, want):
    assert md_to_html(src) == want


@pytest.mark.parametrize("src", [
    "some_var_name и other_var",
    "file_name.txt",
    "2 * 3 * 4 = 24",
    "5*3=15, а 2*4=8",
    "x_1 + y_2",
    "a**b**c",
    "**",
    "* *",
])
def test_no_emphasis_inside_words_or_math(src):
    out = md_to_html(src)
    assert "<i>" not in out and "<b>" not in out
    assert out == escape(src) or out.startswith("• ")


def test_snake_case_between_emphasis_is_kept():
    assert md_to_html("_курсив_ и my_var_name") == "<i>курсив</i> и my_var_name"


@pytest.mark.parametrize("src", ["**незакрытый", "*одна звезда", "_одно подчёркивание", "~~нет пары", "__ нет"])
def test_unbalanced_markers_stay_literal(src):
    assert md_to_html(src) == escape(src)


def test_crossing_markers_do_not_produce_bad_nesting():
    out = md_to_html("**a *b** c*")
    assert well_formed(out)
    assert out.count("<b>") == out.count("</b>")


def test_emphasis_does_not_span_lines():
    out = md_to_html("*начало\nконец*")
    assert "<i>" not in out


# ── код ──────────────────────────────────────────────────────────────────────
def test_inline_code_is_escaped_and_protected():
    assert md_to_html("вызови `a<b && **c**`") == "вызови <code>a&lt;b &amp;&amp; **c**</code>"


def test_inline_code_double_backticks():
    assert md_to_html("``код с ` внутри``") == "<code>код с ` внутри</code>"


def test_inline_code_inside_bold():
    assert md_to_html("**жирный `код`**") == "<b>жирный <code>код</code></b>"


def test_fenced_code_with_language():
    src = "Смотри:\n```python\nif a < b and c_d:\n    print(\"**x**\")\n```\nГотово."
    out = md_to_html(src)
    assert out == ('Смотри:\n<pre><code class="language-python">if a &lt; b and c_d:\n'
                   '    print("**x**")</code></pre>\nГотово.')
    assert well_formed(out)


def test_fenced_code_without_language_and_tilde_fence():
    assert md_to_html("```\nx = 1\n```") == "<pre>x = 1</pre>"
    assert md_to_html("~~~\n~~x~~\n~~~") == "<pre>~~x~~</pre>"


def test_unclosed_fence_runs_to_end():
    out = md_to_html("```sh\nrm -rf _tmp_\nls")
    assert out == '<pre><code class="language-sh">rm -rf _tmp_\nls</code></pre>'


def test_empty_fenced_block_is_dropped():
    assert md_to_html("до\n```\n```\nпосле") == "до\n\nпосле"


def test_triple_backticks_inline_is_code_not_fence():
    assert md_to_html("вот ```x``` так") == "вот <code>x</code> так"


def test_weird_language_is_ignored():
    out = md_to_html('```"><script>\nx\n```')
    assert well_formed(out)
    assert "script" not in out.split("\n")[0] or "&lt;script&gt;" in out


# ── ссылки и адреса ──────────────────────────────────────────────────────────
def test_markdown_link():
    assert md_to_html("[сайт](https://example.com)") == '<a href="https://example.com">сайт</a>'


def test_link_href_quotes_and_ampersands_are_escaped():
    out = md_to_html('[x](https://e.com/?a=1&b="2")')
    assert out == '<a href="https://e.com/?a=1&amp;b=&quot;2&quot;">x</a>'
    assert well_formed(out)


def test_link_text_gets_formatting_but_url_untouched():
    out = md_to_html("[**жирно**](https://e.com/a_b_c_d)")
    assert out == '<a href="https://e.com/a_b_c_d"><b>жирно</b></a>'


def test_tg_link_ok_but_javascript_is_not_a_link():
    assert md_to_html("[профиль](tg://user?id=1)") == '<a href="tg://user?id=1">профиль</a>'
    assert "<a" not in md_to_html("[bad](javascript:alert(1))")


def test_link_with_parentheses_in_url():
    out = md_to_html("[вики](https://ru.wikipedia.org/wiki/Python_(язык))")
    assert out == '<a href="https://ru.wikipedia.org/wiki/Python_(язык)">вики</a>'


def test_bare_urls_are_not_formatted():
    src = "см. https://example.com/_private_/some_path_x и *курсив*."
    assert md_to_html(src) == "см. https://example.com/_private_/some_path_x и <i>курсив</i>."


def test_bare_url_trailing_punctuation_stays_outside():
    assert md_to_html("Тут: https://e.com/a_b_.") == "Тут: https://e.com/a_b_."
    assert md_to_html("(см. https://e.com/x)") == "(см. https://e.com/x)"


def test_autolink_in_angle_brackets():
    assert md_to_html("<https://e.com/a_b_>") == "https://e.com/a_b_"


def test_mentions_are_protected():
    assert md_to_html("пиши @_some_user_ или @my_name") == "пиши @_some_user_ или @my_name"


# ── блоки ────────────────────────────────────────────────────────────────────
def test_headings_become_bold():
    assert md_to_html("# Заголовок") == "<b>Заголовок</b>"
    assert md_to_html("### План на **день** ###") == "<b>План на день</b>"
    assert md_to_html("# C#") == "<b>C#</b>"
    assert md_to_html("#хэштег") == "#хэштег"
    assert md_to_html("#12 · напоминание") == "#12 · напоминание"


def test_blockquotes_merge_consecutive_lines():
    out = md_to_html("> первая *строка*\n> вторая\nобычная\n> новая цитата")
    assert out == ("<blockquote>первая <i>строка</i>\nвторая</blockquote>\nобычная\n"
                   "<blockquote>новая цитата</blockquote>")


def test_nested_quote_markers_flattened():
    assert md_to_html(">> глубоко") == "<blockquote>глубоко</blockquote>"


def test_greater_than_not_at_line_start_is_text():
    assert md_to_html("x -> y > z") == "x -&gt; y &gt; z"


def test_bullets():
    src = "- раз\n* два\n+ три\n  - вложенный\n-5 градусов"
    assert md_to_html(src) == "• раз\n• два\n• три\n  • вложенный\n-5 градусов"


def test_bullet_with_formatting_and_italic_star_not_confused():
    assert md_to_html("* **важное** дело") == "• <b>важное</b> дело"


def test_horizontal_rule():
    assert md_to_html("---") == HR_LINE
    assert md_to_html("* * *") == HR_LINE


def test_numbered_list_untouched():
    assert md_to_html("1. раз\n2. два") == "1. раз\n2. два"


# ── корректность результата ──────────────────────────────────────────────────
@pytest.mark.parametrize("src", [
    "**a _b** c_", "*a **b* c**", "~~a *b~~ c*", "[**x](https://e.com) y**", "> **цитата\n**конец",
    "`a` **`b`** _`c`_", "# **_x_**", "- [a](https://a.b) **b** _c_ ~~d~~ `e`",
    "```\n<pre>\n```\n**<i>**", "&amp; &lt; <", "***a** b*",
])
def test_output_always_well_formed(src):
    assert well_formed(md_to_html(src))


def test_well_formed_rejects_bad_html():
    assert well_formed("<b>ok</b> <i>x</i>")
    assert not well_formed("<b>x</i>")
    assert not well_formed("<b>x")
    assert not well_formed("<script>x</script>")
    assert not well_formed("<b><i>x</b></i>")
    assert not well_formed("<code><b>x</b></code>")
    assert not well_formed("<a>x</a>")
    assert not well_formed('<a href="x" onclick="y">x</a>')
    assert not well_formed("<blockquote><blockquote>x</blockquote></blockquote>")
    assert not well_formed("1 < 2")
    assert well_formed('<pre><code class="language-py">x</code></pre>')


def test_fallback_to_escaped_text_when_tags_break(monkeypatch):
    from oracle.bot import render
    monkeypatch.setattr(render, "well_formed", lambda s: False)
    assert render.md_to_html("**x** <y>") == "**x** &lt;y&gt;"


# ── plain ────────────────────────────────────────────────────────────────────
def test_plain_strips_markers():
    src = "# Итог\n**жирно** и _курсив_, ~~нет~~, `код`\n[сайт](https://e.com/a_b)\n> цитата\n- пункт"
    assert plain(src) == "Итог\nжирно и курсив, нет, код\nсайт (https://e.com/a_b)\n│ цитата\n• пункт"


def test_plain_keeps_code_block_content_and_urls():
    assert plain("```py\nmy_var = 1\n```\nhttps://e.com/_x_") == "my_var = 1\nhttps://e.com/_x_"
    assert plain("") == ""


# ── split_message ────────────────────────────────────────────────────────────
def test_split_short_and_empty():
    assert split_message("привет") == ["привет"]
    assert split_message("") == []
    assert split_message("  \n\n ") == []
    assert split_message("\n\nтекст\n\n") == ["текст"]


def test_split_prefers_paragraphs():
    a, b, c = "А" * 300, "Б" * 300, "В" * 300
    parts = split_message(f"{a}\n\n{b}\n\n{c}", limit=650)
    assert parts == [f"{a}\n\n{b}", c]


def test_split_on_lines_when_no_blank_lines():
    lines = [f"строка {i:03d} " + "x" * 40 for i in range(50)]
    parts = split_message("\n".join(lines), limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == "\n".join(lines)
    assert all(not p.startswith("\n") and not p.endswith("\n") for p in parts)


def test_split_on_sentences_inside_long_paragraph():
    text = " ".join(f"Предложение номер {i} про всякое." for i in range(60))
    parts = split_message(text, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert all(p.endswith(".") for p in parts)
    assert " ".join(parts) == text


def test_split_long_single_word_hard_cut():
    text = "x" * 1234
    parts = split_message(text, limit=500)
    assert [len(p) for p in parts] == [500, 500, 234]
    assert "".join(parts) == text


def test_split_long_line_of_words_cuts_on_spaces():
    text = "слово " * 400
    parts = split_message(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert all(not p.startswith(" ") and "ово" in p for p in parts)


def _fences_balanced(chunk: str) -> bool:
    return sum(1 for line in chunk.split("\n") if line.strip().startswith("```")) % 2 == 0


def test_split_keeps_code_fences_balanced():
    code = "\n".join(f"value_{i} = compute({i})  # комментарий" for i in range(120))
    text = "Вот код:\n\n```python\n" + code + "\n```\n\nИ вывод после кода. " + "Ещё текст. " * 50
    parts = split_message(text, limit=700)
    assert len(parts) > 3
    for p in parts:
        assert len(p) <= 700
        assert _fences_balanced(p), p
        assert well_formed(md_to_html(p))
    # продолжение кода открывается тем же языком
    assert parts[1].startswith("```python\n")
    # весь код на месте
    joined = "\n".join(parts)
    for i in (0, 57, 119):
        assert f"value_{i} = compute({i})" in joined


def test_split_code_block_rendered_in_every_chunk():
    code = "\n".join(f"line_{i}" for i in range(200))
    parts = split_message("```\n" + code + "\n```", limit=300)
    for p in parts:
        html = md_to_html(p)
        assert html.startswith("<pre>") and html.endswith("</pre>")


def test_split_when_cut_falls_right_before_closing_fence():
    body = "a" * 280
    text = "```\n" + body + "\n```\n" + "хвост " * 100
    parts = split_message(text, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert all(_fences_balanced(p) for p in parts)
    assert not any(md_to_html(p) == "<pre></pre>" for p in parts)


def test_split_long_code_line_inside_fence():
    text = "```\n" + "z" * 1000 + "\n```"
    parts = split_message(text, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert all(_fences_balanced(p) for p in parts)
    assert "".join(p.replace("```\n", "").replace("\n```", "") for p in parts) == "z" * 1000


def test_split_tiny_limit_never_exceeds():
    parts = split_message("```\n" + "q" * 100 + "\n```", limit=10)
    assert all(len(p) <= 10 for p in parts)


def test_rendered_text_never_longer_than_source_for_markers():
    src = "---\n* * *\n# Заголовок\n- пункт\n**жирно**"
    visible = md_to_html(src)
    import re as _re
    assert len(_re.sub(r"<[^>]+>", "", visible)) <= len(src)


def test_utf16_len():
    from oracle.bot.render import utf16_len
    assert utf16_len("abc") == 3 and utf16_len("привет") == 6 and utf16_len("🙂") == 2

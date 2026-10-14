from pathlib import Path
from types import SimpleNamespace

import pytest

from core.file_parsers import process_md_file
from core.string_extraction import extract_all_unique_strings
from gui.main_window import ModTranslatorApp


def markdown_app(path=None):
    app = ModTranslatorApp.__new__(ModTranslatorApp)
    app.stop_requested = False
    app.get_translation = lambda text: "translated " + text
    app.scope_mod_lang_var = SimpleNamespace(get=lambda: True)
    app.scope_books_var = SimpleNamespace(get=lambda: True)
    app.scope_quests_var = SimpleNamespace(get=lambda: True)
    app.analyzed_jars = {}
    app.analyzed_book_texts = {}
    app.analyzed_class_texts = {}
    app.analyzed_loose = []
    app.analyzed_extra = [("md", str(path))] if path else []
    app.analyzed_zip_json = []
    app.safe_read_file = lambda name: Path(name).read_text(encoding="utf-8")
    app.log = lambda *_args: None
    return app


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_markdown_fenced_code_is_preserved_and_excluded_from_extraction(tmp_path, fence):
    content = f"Opening prose\n{fence}java\nString message = \"Do not translate code\";\n{fence}\nClosing prose\n"
    path = tmp_path / "guide.md"
    path.write_text(content, encoding="utf-8")
    app = markdown_app(path)

    assert extract_all_unique_strings(app) == {"Opening prose", "Closing prose"}
    assert process_md_file(app, content) == (
        f"translated Opening prose\n{fence}java\nString message = \"Do not translate code\";\n"
        f"{fence}\ntranslated Closing prose\n"
    )


def test_markdown_horizontal_rule_does_not_start_yaml_front_matter(tmp_path):
    content = "Opening prose\n---\nClosing prose\n"
    path = tmp_path / "guide.md"
    path.write_text(content, encoding="utf-8")
    app = markdown_app(path)

    assert extract_all_unique_strings(app) == {"Opening prose", "Closing prose"}
    assert process_md_file(app, content) == "translated Opening prose\n---\ntranslated Closing prose\n"


def test_markdown_stop_preserves_unprocessed_tail():
    app = markdown_app()

    def translate_and_stop(text):
        app.stop_requested = True
        return "translated " + text

    app.get_translation = translate_and_stop
    assert process_md_file(app, "Opening prose\nClosing prose\nFinal prose\n") == (
        "translated Opening prose\nClosing prose\nFinal prose\n"
    )


@pytest.mark.parametrize("title", ['"Guide title"', "'Guide title'", "Guide title"])
def test_markdown_yaml_title_translation_is_a_valid_scalar(title):
    app = markdown_app()
    app.get_translation = lambda _text: 'A "quoted": author\'s title'
    content = f"---\ntitle: {title}\nlayout: guide\n---\n"

    assert process_md_file(app, content) == (
        '---\ntitle: "A \\"quoted\\": author\'s title"\nlayout: guide\n---\n'
    )


def test_markdown_short_fence_inside_long_code_block_is_not_a_closing_fence():
    app = markdown_app()
    content = "````\n```\nKeep this code\n````\nVisible prose\n"

    assert process_md_file(app, content) == (
        "````\n```\nKeep this code\n````\ntranslated Visible prose\n"
    )


def test_markdown_front_matter_scope_and_crlf_are_preserved(tmp_path):
    content = "---\r\ntitle: \"Guide title\"\r\nlayout: technical\r\n...\r\nBody prose\r\n"
    path = tmp_path / "guide.md"
    path.write_bytes(content.encode("utf-8"))
    app = markdown_app(path)

    assert extract_all_unique_strings(app) == {"Guide title", "Body prose"}
    assert process_md_file(app, content) == (
        '---\r\ntitle: "translated Guide title"\r\nlayout: technical\r\n...\r\ntranslated Body prose\r\n'
    )


@pytest.mark.parametrize("title", [
    "'Guide''s title'",
    r'"Guide \"quoted\" title"',
    r'"Guide\nsecond line"',
    "Guide title",
])
def test_markdown_yaml_title_cache_miss_preserves_original_line(title):
    app = markdown_app()
    del app.get_translation
    app.translation_cache = {}
    app.global_memory_var = SimpleNamespace(get=lambda: False)
    content = f"---\r\ntitle: {title}\r\n---\r\n"

    assert process_md_file(app, content) == content


@pytest.mark.parametrize("title,decoded", [
    ("'Guide''s title'", "Guide's title"),
    (r'"Guide \"quoted\" title"', 'Guide "quoted" title'),
    (r'"Guide\nsecond line"', "Guide\nsecond line"),
])
def test_markdown_escaped_yaml_title_uses_decoded_text_for_extraction_and_translation(
        tmp_path, title, decoded):
    content = f"---\ntitle: {title}\n---\n"
    path = tmp_path / "guide.md"
    path.write_text(content, encoding="utf-8")
    app = markdown_app(path)
    app.get_translation = lambda text: 'Translated "title"' if text == decoded else text

    assert extract_all_unique_strings(app) == {decoded}
    assert process_md_file(app, content) == '---\ntitle: "Translated \\"title\\""\n---\n'


@pytest.mark.parametrize("title", [
    r'"Guide\x20title"',
    "'Guide's title'",
    "!custom Guide title",
    "Guide title # metadata comment",
])
def test_markdown_unsupported_yaml_title_syntax_is_preserved(tmp_path, title):
    content = f"---\ntitle: {title}\n---\n"
    path = tmp_path / "guide.md"
    path.write_text(content, encoding="utf-8")
    app = markdown_app(path)

    assert extract_all_unique_strings(app) == set()
    assert process_md_file(app, content) == content

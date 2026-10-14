from pathlib import Path
from types import SimpleNamespace

from core.file_parsers import process_text_file
from core.string_extraction import extract_all_unique_strings
from gui.main_window import ModTranslatorApp


def snbt_app(path=None, strict=False):
    app = ModTranslatorApp.__new__(ModTranslatorApp)
    app.stop_requested = False
    app.strict_whitelist_var = SimpleNamespace(get=lambda: strict)
    app.scope_quests_var = SimpleNamespace(get=lambda: True)
    app.get_translation = lambda text: "translated " + text
    app.log = lambda *_args: None
    app.analyzed_jars = {}
    app.analyzed_book_texts = {}
    app.analyzed_class_texts = {}
    app.analyzed_loose = []
    app.analyzed_extra = [("snbt", str(path))] if path else []
    app.analyzed_zip_json = []
    app.safe_read_file = lambda name: Path(name).read_text(encoding="utf-8")
    return app


def test_snbt_multiline_technical_scalar_preserved(tmp_path):
    content = '{\n type:\n "item"\n title: "Collect items"\n}'
    path = tmp_path / "chapter.snbt"
    path.write_text(content, encoding="utf-8")
    app = snbt_app(path)

    assert extract_all_unique_strings(app) == {"Collect items"}
    assert process_text_file(app, content) == '{\n type:\n "item"\n title: "translated Collect items"\n}'


def test_snbt_technical_list_values_and_nested_nbt_are_preserved(tmp_path):
    content = '{\n command: [\n "say Hello player"\n ]\n nbt: {Name: "Technical label"}\n description: [\n "Read this quest"\n ]\n}'
    path = tmp_path / "chapter.snbt"
    path.write_text(content, encoding="utf-8")
    app = snbt_app(path, strict=True)

    assert extract_all_unique_strings(app) == {"Read this quest"}
    assert process_text_file(app, content) == '{\n command: [\n "say Hello player"\n ]\n nbt: {Name: "Technical label"}\n description: [\n "translated Read this quest"\n ]\n}'


def test_snbt_quoted_keys_and_comments_are_not_translation_input(tmp_path):
    content = '{"description": "Read this quest"\n // "Do not translate comment"\n}'
    path = tmp_path / "chapter.snbt"
    path.write_text(content, encoding="utf-8")
    app = snbt_app(path)

    assert extract_all_unique_strings(app) == {"Read this quest"}
    assert process_text_file(app, content) == '{"description": "translated Read this quest"\n // "Do not translate comment"\n}'


def test_snbt_inline_nested_fields_preserve_technical_values_and_escaped_text():
    app = snbt_app(strict=True)
    app.get_translation = lambda _text: 'A "quoted" quest\\path'
    content = '{tasks: [{type: "item", item: "minecraft:stone", title: "Quest title"}], description: [["Quest details"]]}'

    assert process_text_file(app, content) == (
        '{tasks: [{type: "item", item: "minecraft:stone", title: "A \\"quoted\\" quest\\\\path"}], '
        'description: [["A \\"quoted\\" quest\\\\path"]]}'
    )


def test_snbt_double_quotes_inside_single_quoted_technical_value_are_preserved():
    app = snbt_app()
    content = '{command: \'say "Unchanged command"\', title: "Quest title"}'

    assert process_text_file(app, content) == (
        '{command: \'say "Unchanged command"\', title: "translated Quest title"}'
    )

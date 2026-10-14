import argparse
import contextlib
import os
import sys
import tkinter as tk

from MinecraftTranslatorGUI import ModTranslatorApp


def _print_safe(message, stream=None):
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, errors="replace").decode(
        encoding, errors="replace")
    print(safe, file=stream, flush=True)


class CliModTranslatorApp(ModTranslatorApp):
    def log(self, message):
        _print_safe(message)

    def _ask_proceed_from_thread(self, title, msg):
        self.log(f"[CLI] {title}: {msg}")
        return True

    def update_progress(self, current, total, text_mode=False):
        if total <= 0:
            return
        step = max(1, total // 20)
        if current == total or current % step == 0:
            unit = "strings" if text_mode else "files"
            _print_safe(f"[progress] {current}/{total} {unit}")

    def _set_summary_card(self, *args, **kwargs):
        return None

    def _refresh_api_summary(self, *args, **kwargs):
        return None

    def _refresh_output_summary(self, *args, **kwargs):
        return None

    def _schedule_save(self, *args):
        return None

    def save_config(self):
        return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Minecraft modpack translator CLI (en_us/zh_cn -> zh_tw).")
    parser.add_argument("--modpack", required=True, help="Minecraft instance root folder")
    parser.add_argument("--output-dir", default=None, help="Output folder")
    parser.add_argument("--name", default="Auto_Translated_Mods_zh_tw.zip")
    parser.add_argument("--output-mode", choices=("resource_pack", "hybrid", "jar_patch"),
                        default="jar_patch")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not translate")
    parser.add_argument("--skip-mods", action="store_true", help="Skip JAR/mod language outputs")
    parser.add_argument("--skip-quests", action="store_true", help="Skip quest/config outputs")
    parser.add_argument("--max-steps", type=int, default=-1, help="Limit analyzed file targets")
    parser.add_argument("--retry", type=int, default=None, help="Validation retry count")
    parser.add_argument(
        "--server-mode", action="store_true",
        help="Explicitly allow dedicated-server JAR output (requires server.properties)")
    parser.add_argument("--engine", choices=(
        "market_ai", "non_ai_chain", "google", "deepl", "azure",
        "claude", "openai", "local"))
    parser.add_argument("--provider", help="Market AI provider label, e.g. DeepSeek, Kimi / Moonshot")
    parser.add_argument("--model", help="Model id")
    parser.add_argument("--base-url", help="Market AI base URL or local engine endpoint")
    parser.add_argument("--api-key", help="Provider API key")
    return parser.parse_args()


def clear_previous_analysis(app):
    app.analyzed_jars.clear()
    app.analyzed_book_texts.clear()
    app.analyzed_book_text_repairs.clear()
    app.analyzed_static_assets.clear()
    app.analyzed_loose.clear()
    app.analyzed_loose_base.clear()
    app.analyzed_extra.clear()
    app.analyzed_jars_zh_base.clear()
    app._reset_progress_counters()


def apply_filters(app, args):
    if args.skip_mods:
        app.analyzed_jars.clear()
        app.analyzed_loose = [
            p for p in app.analyzed_loose
            if app._is_quest_path(p) or app._is_book_path(p)
        ]
    if args.skip_quests:
        app.analyzed_extra = [
            item for item in app.analyzed_extra
            if not app._is_quest_path(item[1])
        ]
        app.analyzed_loose = [
            p for p in app.analyzed_loose
            if not app._is_quest_path(p)
        ]

    if args.max_steps is not None and args.max_steps > 0:
        remaining = args.max_steps
        new_jars = {}
        for jar_path, files in app.analyzed_jars.items():
            if remaining <= 0:
                break
            selected = {}
            for path_in_jar, data in files.items():
                if remaining <= 0:
                    break
                selected[path_in_jar] = data
                remaining -= 1
            if selected:
                new_jars[jar_path] = selected
        app.analyzed_jars = new_jars
        if remaining > 0:
            app.analyzed_loose = app.analyzed_loose[:remaining]
            remaining -= len(app.analyzed_loose)
        else:
            app.analyzed_loose = []
        if remaining > 0:
            app.analyzed_extra = app.analyzed_extra[:remaining]
            remaining -= len(app.analyzed_extra)
        else:
            app.analyzed_extra = []

        def limited_nested_targets(targets):
            nonlocal remaining
            selected_targets = {}
            if not isinstance(targets, dict):
                return selected_targets
            for archive_path, files in targets.items():
                selected_files = {}
                for internal_path, value in files.items():
                    if remaining <= 0:
                        break
                    selected_files[internal_path] = value
                    remaining -= 1
                if selected_files:
                    selected_targets[archive_path] = selected_files
            return selected_targets

        # Every file that extraction/packaging can consume shares the same
        # limit, including manuals and optional class tooltips inside archives.
        for name in ("analyzed_book_texts", "analyzed_book_text_repairs",
                     "analyzed_class_texts", "analyzed_static_assets"):
            if hasattr(app, name):
                setattr(app, name, limited_nested_targets(getattr(app, name)))
        if hasattr(app, "analyzed_zip_json"):
            app.analyzed_zip_json = app.analyzed_zip_json[:remaining]


def main():
    args = parse_args()
    modpack = os.path.abspath(args.modpack)
    if not os.path.isdir(modpack):
        _print_safe(f"Modpack folder not found: {modpack}", sys.stderr)
        return 2

    output_dir = os.path.abspath(args.output_dir or os.getcwd())
    if not os.path.isdir(output_dir):
        _print_safe(f"Output folder not found: {output_dir}", sys.stderr)
        return 2
    output_name = ModTranslatorApp._safe_zip_filename(
        args.name, "Auto_Translated_Mods_zh_tw")

    root = None
    app = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = CliModTranslatorApp(root)
        app._server_mode_requested = bool(getattr(args, "server_mode", False))
        app.mod_dir_var.set(modpack)
        app.rp_dir_var.set(output_dir)
        app.rp_name_var.set(output_name)
        app.output_mode_var.set(args.output_mode)
        if args.retry is not None:
            app.retry_count_var.set(max(0, min(10, args.retry)))
        if args.engine:
            app.engine_var.set(args.engine)
        if args.provider:
            app.ai_provider_var.set(args.provider)
            app._on_ai_provider_change()
        selected_engine = args.engine or app.engine_var.get()
        if args.model:
            model_field = {
                "openai": "openai_model_var", "claude": "claude_model_var",
            }.get(selected_engine, "ai_model_var")
            getattr(app, model_field).set(args.model)
        if args.base_url:
            endpoint_field = "local_url_var" if selected_engine == "local" else "ai_base_url_var"
            getattr(app, endpoint_field).set(args.base_url)
        if args.api_key:
            key_field = {
                "google": "api_key_var", "deepl": "deepl_key_var",
                "azure": "azure_key_var", "claude": "claude_key_var",
                "openai": "openai_key_var",
            }.get(selected_engine, "ai_api_key_var")
            getattr(app, key_field).set(args.api_key)

        clear_previous_analysis(app)
        app.analyzed_mc_dir = modpack
        app._analyze_task(modpack)
        root.update()
        if getattr(app, "_last_analysis_succeeded", True) is False:
            _print_safe("Scan failed or was cancelled; no translation executed.", sys.stderr)
            return 1
        apply_filters(app, args)

        unique_strings = app.extract_all_unique_strings()
        # Translation reuses the scan snapshot; replace it after CLI filters so
        # excluded files cannot still trigger translation requests.
        app._analysis_unique_strings = set(unique_strings)
        _print_safe("")
        _print_safe("Scan summary")
        _print_safe(f"  JARs: {len(app.analyzed_jars)}")
        _print_safe(f"  loose lang files: {len(app.analyzed_loose)}")
        _print_safe(f"  extra files: {len(app.analyzed_extra)}")
        _print_safe(f"  unique strings: {len(unique_strings)}")
        for sample in sorted(unique_strings, key=lambda s: (-len(s), s))[:5]:
            _print_safe(f"  sample: {sample[:120].replace(chr(10), ' ')}")

        if args.dry_run:
            _print_safe("[dry-run] No translation executed.")
            return 0

        pack_format = app.pack_format_var.get()
        app._translate_task(output_dir, output_name, pack_format, args.output_mode)
        root.update()
        if getattr(app, "_last_translation_succeeded", True) is False:
            _print_safe("Translation failed or was cancelled; see the log above.", sys.stderr)
            return 1
        return 0
    except Exception as error:
        # Provider exceptions may contain credentials or query parameters.
        _print_safe(f"CLI failed ({type(error).__name__}).", sys.stderr)
        return 1
    finally:
        cache = getattr(app, "translation_cache", None)
        close = getattr(cache, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        if root is not None:
            with contextlib.suppress(Exception):
                root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())

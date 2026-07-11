import argparse
import os
import sys
import tkinter as tk

from MinecraftTranslatorGUI import ModTranslatorApp


class CliModTranslatorApp(ModTranslatorApp):
    def log(self, message):
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = str(message).encode(encoding, errors="replace").decode(
            encoding, errors="replace")
        print(safe, flush=True)

    def _ask_proceed_from_thread(self, title, msg):
        print(f"[CLI] {title}: {msg}", flush=True)
        return True

    def update_progress(self, current, total, text_mode=False):
        if total <= 0:
            return
        step = max(1, total // 20)
        if current == total or current % step == 0:
            unit = "strings" if text_mode else "files"
            print(f"[progress] {current}/{total} {unit}", flush=True)

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
                        default="hybrid")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not translate")
    parser.add_argument("--skip-mods", action="store_true", help="Skip JAR/mod language outputs")
    parser.add_argument("--skip-quests", action="store_true", help="Skip quest/config outputs")
    parser.add_argument("--max-steps", type=int, default=-1, help="Limit analyzed file targets")
    parser.add_argument("--retry", type=int, default=None, help="Validation retry count")
    parser.add_argument("--engine", choices=(
        "market_ai", "non_ai_chain", "google", "deepl", "azure",
        "claude", "openai", "local"))
    parser.add_argument("--provider", help="Market AI provider label, e.g. DeepSeek, Kimi / Moonshot")
    parser.add_argument("--model", help="Model id")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
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
        else:
            app.analyzed_extra = []


def main():
    args = parse_args()
    modpack = os.path.abspath(args.modpack)
    if not os.path.isdir(modpack):
        print(f"Modpack folder not found: {modpack}", file=sys.stderr)
        return 2

    output_dir = os.path.abspath(args.output_dir or os.getcwd())
    if not os.path.isdir(output_dir):
        print(f"Output folder not found: {output_dir}", file=sys.stderr)
        return 2
    output_name = ModTranslatorApp._safe_zip_filename(
        args.name, "Auto_Translated_Mods_zh_tw")

    root = tk.Tk()
    root.withdraw()
    app = CliModTranslatorApp(root)
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
    if args.model:
        app.ai_model_var.set(args.model)
    if args.base_url:
        app.ai_base_url_var.set(args.base_url)
    if args.api_key:
        app.ai_api_key_var.set(args.api_key)

    clear_previous_analysis(app)
    app.analyzed_mc_dir = modpack
    app._analyze_task(modpack)
    root.update()
    apply_filters(app, args)

    unique_strings = app.extract_all_unique_strings()
    print("")
    print("Scan summary")
    print(f"  JARs: {len(app.analyzed_jars)}")
    print(f"  loose lang files: {len(app.analyzed_loose)}")
    print(f"  extra files: {len(app.analyzed_extra)}")
    print(f"  unique strings: {len(unique_strings)}")
    for sample in sorted(unique_strings, key=lambda s: (-len(s), s))[:5]:
        text = f"  sample: {sample[:120].replace(chr(10), ' ')}"
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))

    if args.dry_run:
        print("[dry-run] No translation executed.")
        root.destroy()
        return 0

    pack_format = app.pack_format_var.get()
    app._translate_task(output_dir, output_name, pack_format, args.output_mode)
    root.update()
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

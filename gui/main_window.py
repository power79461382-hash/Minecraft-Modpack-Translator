import os
import sys
import json
import re
import base64
import zipfile
import hashlib
import pickle
import time
import threading
import webbrowser
import requests
import shutil
from collections import Counter, deque
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from tkinter import ttk

from translator_providers import build_provider_registry, normalize_base_url
from core.json_loader import load_json_lenient
from core.analysis_scan import (
    run_analyze_task,
    run_analyze_task_impl,
    scan_single_jar as core_scan_single_jar,
)
from core.batch_translation import batch_translate_missing as core_batch_translate_missing
from core.string_extraction import extract_all_unique_strings as core_extract_all_unique_strings
from core.verification import (
    verify_translations as core_verify_translations,
    write_failed_items_report as core_write_failed_items_report,
)
from core.coverage_check import run_coverage_task, run_coverage_task_server
from core.file_parsers import (
    process_advancement_json as core_process_advancement_json,
    process_apoth_names_cfg as core_process_apoth_names_cfg,
    process_md_file as core_process_md_file,
    process_text_file as core_process_text_file,
    snbt_escape as core_snbt_escape,
    snbt_key_for_value as core_snbt_key_for_value,
    snbt_skip_by_key as core_snbt_skip_by_key,
    snbt_structure_signature as core_snbt_structure_signature,
)
from core.class_patcher import (
    has_cjk_text as core_has_cjk_text,
    decode_mutf8 as core_decode_mutf8,
    class_utf8_entries as core_class_utf8_entries,
    is_hardcoded_lore_string as core_is_hardcoded_lore_string,
    mutf8_encode as core_mutf8_encode,
    patch_class_hardcoded_strings as core_patch_class_hardcoded_strings,
)
from core.format_mask import (
    mask_format as core_mask_format,
    unmask_format as core_unmask_format,
    fix_placeholders as core_fix_placeholders,
    repair_patchouli_macros as core_repair_patchouli_macros,
)
from core.config_store import (
    obfuscate as core_obfuscate,
    deobfuscate as core_deobfuscate,
)
from core.json_utils import clean_json_text as core_clean_json_text
from core.translation_flow import (
    POST_TRANSLATION_RESCUE_LIMIT,
    run_translate_task,
)
from core.jar_patcher import (
    build_class_inject_for_jar as core_build_class_inject_for_jar,
    generate_class_patch_jars as core_generate_class_patch_jars,
    generate_jar_patches as core_generate_jar_patches,
    has_openloader_resources,
    jar_launch_risk_reasons,
    jar_rewrite_is_high_risk,
    mixin_targets_client_renderer,
    rebuild_jar_with_inject,
    write_openloader_resource_overlay as core_write_openloader_resource_overlay,
)
from translation_cache import (
    cache_snapshot,
    load_translation_cache,
    review_and_fix_cache,
    save_translation_cache,
)
from translation_packager import (
    drop_untranslated_lang_entries,
    load_lang_content,
    merge_structured_json_with_existing_zh,
    sanitize_text,
    sanitize_value,
)


def _bounded_post_translation_engine_items(app, items):
    """Return deterministic rescue work within the shared engine-call budget."""
    unique_items = sorted(set(items))
    budget = max(0, min(
        POST_TRANSLATION_RESCUE_LIMIT,
        int(getattr(
            app, '_post_translation_rescue_budget',
            POST_TRANSLATION_RESCUE_LIMIT)),
    ))
    selected = unique_items[:budget]
    omitted = len(unique_items) - len(selected)
    if hasattr(app, '_post_translation_rescue_budget'):
        app._post_translation_rescue_budget = budget - len(selected)
        app._post_translation_rescue_budget_managed = True
    if omitted:
        app.log(
            f"ℹ️  補翻引擎工作上限 {POST_TRANSLATION_RESCUE_LIMIT} 筆；"
            f"另 {omitted} 筆保留原文，避免完成後長時間卡住。")
    return selected

try:
    from opencc import OpenCC
    _OPENCC_TW = OpenCC("s2twp")
except Exception:
    _OPENCC_TW = None


class ModTranslatorApp:
    # ── 類別層級：預先編譯所有 should_translate 用到的正則，避免每次呼叫重新編譯 ──
    _RE_NUMBER     = re.compile(r'^[-+]?\d*\.?\d+[a-zA-Z]*$')
    # 資源 ID 限小寫（MC 規範強制小寫；IGNORECASE 會把 "Mode:Auto" 這類顯示文字誤殺）
    _RE_NAMESPACE  = re.compile(r'^[a-z0-9_\.]+:[a-z0-9_/\.\-]+$')
    # 混合大小寫的 lang key 參照（如 advancement.enigmaticlegacy:unholyGrailWorthy.desc）：
    # 「同時含冒號與點號」且無空白才視為技術參照
    _RE_LANG_KEY_REF = re.compile(
        r'^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_\-]+)*:[A-Za-z0-9_/\-]*\.[A-Za-z0-9_/\.\-]+$')
    _RE_FILEPATH   = re.compile(r'^[\w\-/]+\.\w+$')
    _RE_DOTPATH    = re.compile(r'^[a-z0-9_]+(\.[a-z0-9_]+)+$', re.IGNORECASE)
    _STRUCTURAL_REF_EXTS = {
        'nbt', 'schem', 'schematic', 'mcstructure',
        'png', 'jpg', 'jpeg', 'webp', 'gif', 'ogg', 'wav',
        'json', 'mcmeta', 'toml', 'cfg', 'properties', 'yaml', 'yml',
        'class', 'jar', 'zip',
    }
    _HIGH_RISK_STRUCTURAL_EXTS = {'nbt', 'schem', 'schematic', 'mcstructure'}
    # 三段以上 snake_case（has_xxx_yyy_slab 之類的技術 key）
    _RE_SNAKE_KEY  = re.compile(r'^[a-z0-9]+(?:_[a-z0-9]+){2,}$')
    # CJK 字元（判斷字串是否已是中文）
    _RE_CJK_CHAR   = re.compile(r'[一-鿿㐀-䶿]')
    # 4 字母以上的英文單詞（判斷中文字串裡是否殘留待翻英文）
    _RE_EN_WORD    = re.compile(r'[A-Za-z]{4,}')
    # 鍵盤快捷鍵（CTRL + ALT + C、Shift + %s、[ F4 ] 等）：永遠不用翻
    _RE_KEY_CHORD  = re.compile(
        r'^\[?\s*(?:ctrl|alt|shift|cmd|win|tab)'
        r'(?:\s*\+\s*(?:ctrl|alt|shift|cmd|win|tab|f?\d{1,2}|%s|[a-z]))*\s*\+?\s*\]?$'
        r'|^\[\s*f\d{1,2}\s*\]$',
        re.IGNORECASE)
    # 兩字母以上的英文連字（text_clean 必須含此才值得送翻）
    _RE_ALPHA_RUN2 = re.compile(r'[A-Za-z]{2,}')
    _RE_BRACED_LANG_KEY = re.compile(
        r'^\{[^{}\s]+\}(?:(?:\\n|\\\\n)\{[^{}\s]+\})*$',
        re.IGNORECASE)
    _RE_PATCHOULI_CONTROL_TOKEN = re.compile(
        r'^#[A-Za-z_][A-Za-z0-9_]*#?$')
    _RE_COLOR      = re.compile(r'^#?[0-9a-fA-F]{6,8}$')
    _RE_ALLCAPS    = re.compile(r'^[A-Z0-9_]+$')
    _RE_NONWORD    = re.compile(r'^[\W_0-9]+$')
    _RE_HASALPHA   = re.compile(r'[a-zA-Z\u0400-\u04FF\u00C0-\u017F\u4e00-\u9fa5]')
    _RE_QUOTED_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
    # %單字% 自訂佔位符優先於 printf 樣式，避免 %DAMAGE_REDUCTION% 被當成 %D。
    # 字元限 ASCII（不可用 \w，CJK 也是 \w 會把 %s中文%s 誤併）
    _RE_PLACEHOLDER = re.compile(r'%[A-Za-z_][A-Za-z0-9_]{1,40}%|%[0-9\.\$]*[a-zA-Z]')
    _RE_FORMAT = re.compile(
        r'(§[0-9a-fk-orlmnx]'
        r'|”[0-9a-fk-orlmnx]'
        r'|&[0-9a-fk-orlmnx]'
        r'|!\[[^\]]*\]\([^)]+\)'
        r'|\[[^\]]+\]\([^)]+\)'
        r'|\([A-Za-z0-9_./\-]+\.md(?:#[A-Za-z0-9_\-]+)?\)'
        # 模組自訂的 %單字% 佔位符（%COST%、%DAMAGE_REDUCTION%、%PLAYER% 等）
        # 必須整段當成一個 token——且要排在下面 printf 規則「之前」，
        # 否則 %DAMAGE_REDUCTION% 會被 printf 的 %[字母] 切成 %D，殘留 AMAGE_REDUCTION%。
        # 字元限 ASCII（不可用 \w，否則 CJK 也算 word，會把 %s中文%s 併成一個假 token）
        r'|%[A-Za-z_][A-Za-z0-9_]{1,40}%'
        r'|%(?:\d+\$)?[-+]?[\d.]*[a-zA-Z]'
        # Minecraft 斜線指令（/sethome、/home、/spawn、/rtp…）：指令名一律保留原文。
        # 只在「指令邊界」才匹配：斜線前是色碼（&e/sethome、§a/home、”a/）或非英數邊界
        # （行首、空白、括號）。如此可保護指令，又不會把 and/or、config/file、1/2
        # 這類詞中斜線的第二段誤當指令（避免誤遮既有正確譯文）
        r'|(?:(?<=[&§”][0-9a-fk-orx])|(?<![A-Za-z0-9/]))/[a-z][a-z0-9_]{1,29}'
        r'|\$\([^)\r\n]{0,240}\)'   # Patchouli 巨集，含空巨集 $()（重置格式，常見）——
        r'|\\[ntr\\]'               # 允許較長連結巨集；空 $() 漏掉會被翻成全形 $（）導致整頁失效
        # FTB Quests 內嵌標記：{image:atm:textures/...png width:100 align:center}、
        # {@pagebreak}、{item:...} 等——整段含命名空間/路徑/.png 檔名/關鍵字，
        # 一個字都不能翻，否則 FTBQ 找不到圖片→全部破圖（實測 ATM10 638 個被翻壞）
        r'|\{(?:image|item|@)[^{}]*\}'
        r'|\{[a-zA-Z_]\w{0,30}\}'
        # FTB Quests / KubeJS 動態文字效果標籤（MiniMessage 類語法）：
        # <grad from=#... hue=true>、<pend ...>、<glitch ...>、<neon ...> 等
        # 內部屬性、true/false 與標籤名都屬於語法，不能送進翻譯引擎。
        r'|</?(?:grad|gradient|rainb|rainbow|wave|shake|pend|glitch|neon|pulse|spin|scale|transition|color|colour|font|lang|keybind|newline|br|bold|italic|underlined?|strikethrough|obfuscated|reset)(?:\s+[^<>]{0,180})?>'
        r'|<[a-zA-Z_][a-zA-Z0-9_ ]{0,25}>'  # 角括號樣板變數 <class>、<from slot> 等
        # MC 能量/通量單位（EU、RF、FE、CF，含 /t 變體）：是代號不是英文字，
        # 不可翻——否則 EU→歐盟、FE→鐵 之類。限「全大寫獨立 token」(?-i:) 關掉
        # 外層 IGNORECASE，避免誤抓 euro、feud 等小寫；前後須非英數，避免切到單字
        r'|(?-i:(?<![A-Za-z0-9])(?:EU|RF|FE|CF)(?:/t)?(?![A-Za-z0-9]))'
        # 羅馬數字：限大寫、至少 2 字元、且必須是「合法」羅馬序列——
        # [IVXLC]{2,7} 會把 XXL/CC/ILL 等普通大寫詞當格式碼，
        # 導致這類字串被遮罩破壞或譯文被驗證誤殺
        r'|(?-i:(?<![A-Za-z])(?=[IVXLC]{2})'
        r'(?:XC|XL|L?X{1,3}(?:IX|IV|V?I{0,3})?|II|III|IV|VI|VII|VIII|IX)'
        r'(?![A-Za-z]))'
        r')',
        re.IGNORECASE
    )
    _RE_ROMAN_TOKEN = re.compile(
        r'^(?:C|XC|XL|L?X{1,3}(?:IX|IV|V?I{0,3})?|II|III|IV|V|VI|VII|VIII|IX)$',
        re.IGNORECASE,
    )
    # JAR 簽名檔（修改簽名 JAR 內容時必須移除，否則 JVM 驗簽拋 SecurityException）
    _RE_JAR_SIG = re.compile(r'^META-INF/[^/]+\.(SF|RSA|DSA|EC)$', re.IGNORECASE)
    SYNTHETIC_LANG_ZH_TW = {
        "attribute.name.generic.additionalentityattributes.critical_bonus_damage": "暴擊額外傷害",
        "attribute.name.generic.additionalentityattributes.water_speed": "水中速度",
        "attribute.name.generic.additionalentityattributes.lava_speed": "熔岩速度",
        "attribute.name.generic.additionalentityattributes.monster_explosion_damage_bonus": "怪物爆炸傷害加成",
        "attribute.name.generic.additionalentityattributes.monster_projectile_damage_bonus": "怪物投射物傷害加成",
        "attribute.name.generic.additional_attributes.critical_bonus_damage": "暴擊額外傷害",
        "attribute.name.generic.additional_attributes.water_speed": "水中速度",
        "attribute.name.generic.additional_attributes.lava_speed": "熔岩速度",
        "attribute.name.generic.additional_attributes.monster_explosion_damage_bonus": "怪物爆炸傷害加成",
        "attribute.name.generic.additional_attributes.monster_projectile_damage_bonus": "怪物投射物傷害加成",
        "attribute.name.generic.monster_explosion_damage_bonus": "怪物爆炸傷害加成",
        "attribute.name.generic.monster_projectile_damage_bonus": "怪物投射物傷害加成",
        "attribute.name.generic.monster.explosion_damage_bonus": "怪物爆炸傷害加成",
        "attribute.name.generic.monster.projectile_damage_bonus": "怪物投射物傷害加成",
        "monster.explosion_damage_bonus": "怪物爆炸傷害加成",
        "monster.projectile_damage_bonus": "怪物投射物傷害加成",
        "monster-explosion_damage_bonus": "怪物爆炸傷害加成",
        "monster-projectile_damage_bonus": "怪物投射物傷害加成",
        "attribute.name.generic.protect_fury": "保護狂怒",
        "attribute.name.generic.protect_magic": "保護魔法",
        "attribute.name.generic.protect_projectile": "保護投射物",
        "attribute.name.generic.protect_explosion": "保護爆炸",
        "attribute.name.generic.ars_nouveau.perk.toughness": "魔法韌性",
    }
    ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = SYNTHETIC_LANG_ZH_TW

    MC_PACK_FORMATS = {
        "1.12.2": {"rp": 3, "dp": 1},
        "1.16.5": {"rp": 6, "dp": 6},
        "1.18.2": {"rp": 8, "dp": 9},
        "1.19.2": {"rp": 9, "dp": 10},
        "1.19.4": {"rp": 13, "dp": 12},
        "1.20.1": {"rp": 15, "dp": 15},
        "1.20.4": {"rp": 22, "dp": 26},
        "1.20.6": {"rp": 32, "dp": 41},
        "1.21.1+": {"rp": 34, "dp": 48},
    }

    # 模組條件運算式：or(mod(...))、not(item(...))、and(tag(...)) 等 FTB Quests 語法
    _RE_MOD_CONDITION = re.compile(
        r'\((?:mod|item|tag|fluid|block|entity|biome|dimension)\s*\(',
        re.IGNORECASE
    )

    # ── SNBT 技術欄位黑名單：這些 key 的 value 不應翻譯 ──
    _SNBT_NO_TRANS_KEYS = frozenset({
        'id', 'group', 'filename', 'type', 'shape', 'icon', 'item', 'tag',
        'dimension', 'quest', 'command', 'team_rank', 'trigger_type',
        'default_quest_shape', 'default_chapter_image', 'color', 'image',
        'loot_table', 'nbt', 'uid', 'uuid', 'key', 'function',
        'reward_team_uuid', 'unlock_type', 'logic_type', 'visibility',
        'quest_shape', 'chapter', 'linked_chapter',
        'default_reward_team', 'default_consume_items',
        'default_autoclaim_rewards', 'default_quest_disable_jei',
        # 實體、結構、進度等技術引用
        'entity', 'structure', 'advancement', 'recipe', 'biome',
        'block', 'fluid', 'enchantment', 'effect', 'attribute',
        'advancement_icon', 'progress_text', 'team', 'player',
    })

    # 注意：'category' 是 Patchouli 的 registry 參照、'translate' 的值是 lang key，
    # 都不是可翻譯文字，翻了會讓條目消失——不可加入此白名單
    _STRICT_TEXT_KEYS = frozenset({
        'name', 'title', 'subtitle', 'description', 'desc', 'text', 'message',
        'tooltip', 'tooltips', 'summary', 'label', 'labels', 'body', 'content',
        'details', 'note', 'notes', 'hint', 'hints', 'lore', 'dialogue',
        'dialog', 'chapter', 'quest_title', 'quest_description', 'task',
        'reward', 'rewards', 'page', 'pages', 'paragraph',
        'header', 'footer', 'question', 'answer', 'placeholder',
    })
    _STRICT_TEXT_SUFFIXES = (
        '_name', '.name', 'name',
        '_title', '.title', 'title',
        '_description', '.description', 'description',
        '_desc', '.desc', 'desc',
        '_text', '.text', 'text',
        '_tooltip', '.tooltip', 'tooltip',
        '_message', '.message', 'message',
        '_summary', '.summary', 'summary',
        '_label', '.label', 'label',
        '_lore', '.lore', 'lore',
    )

    # 匹配 SNBT 引號值前方最後一個 key。
    # FTB Quests 常把物件寫成單行：{id: "...", type: "item", ...}
    # 因此不能只看「行開頭」，必須抓 match 前最後一個 key。
    _RE_SNBT_KEY_CTX = re.compile(r'(?:"([^"]+)"|([A-Za-z_][\w\-]*))\s*:\s*$')

    # 舊版輸出若曾把 FTB Quests type ID 翻成中文，載入或重跑時強制還原。
    _FTBQ_TYPE_REPAIR = {
        '進度': 'advancement',
        '进度': 'advancement',
        '項目': 'item',
        '项目': 'item',
        '物品': 'item',
        '經驗值': 'xp',
        '经验值': 'xp',
        '經驗': 'xp',
        '经验': 'xp',
        '殺': 'kill',
        '杀': 'kill',
        '擊殺': 'kill',
        '击杀': 'kill',
        '維度': 'dimension',
        '维度': 'dimension',
    }

    # 12 位以上純 hex 字串 = FTB Quests 任務 ID，不應翻譯
    _RE_HEX_ID = re.compile(r'^[0-9a-fA-F]{12,}$')

    # ── 深色主題色盤 ──
    C_BG        = "#1e1e2e"
    C_SURFACE   = "#2a2a3e"
    C_BORDER    = "#3a3a5c"
    C_ACCENT    = "#7c6af7"
    C_ACCENT2   = "#5bc0eb"
    C_SUCCESS   = "#4caf7d"
    C_WARN      = "#f0a500"
    C_DANGER    = "#e05c5c"
    C_TEXT      = "#e0e0f0"
    C_MUTED     = "#888aaa"
    C_ENTRY_BG  = "#14142a"
    C_ENTRY_FG  = "#d0d0f0"
    C_LOG_BG    = "#0d0d1a"
    C_LOG_FG    = "#a8ffbd"

    # ── 引擎定義：(value, 顯示文字, 顏色) ──
    ENGINES = [
        ("google",  "🚀  Google API / GTX",          "#7c6af7"),
        ("deepl",   "⚡  DeepL API  (推薦·快·準)",    "#06b6d4"),
        ("azure",   "🌐  Microsoft Azure Translator", "#0ea5e9"),
        ("claude",  "🤖  Claude API  (AI·最高品質)",  "#a855f7"),
        ("openai",  "💬  OpenAI API  (GPT·AI 翻譯)",  "#10b981"),
        ("market_ai", "🌍  市面 AI 模型  (API / 登入)", "#f59e0b"),
        ("local",   "🖥️  本地端 AI  (LM Studio…)",    "#5bc0eb"),
    ]

    AI_PROVIDER_PRESETS = {
        "openai": {
            "label": "OpenAI",
            "api_type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "models": (
                "gpt-5.4-mini",
                "gpt-5.4-nano",
                "gpt-5.4",
                "gpt-5.5",
                "gpt-5.2",
                "gpt-4.1",
            ),
            "login_url": "https://platform.openai.com/api-keys",
            "hint": "OpenAI 官方 API（2026-06 查證）；大量批次翻譯建議 gpt-5.4-mini / gpt-5.4-nano，品質優先用 gpt-5.5。"
                    "gpt-3.5/gpt-4/gpt-4o 系列 2026-10-23 全面退役，已移除。",
            "requires_key": True,
            "token_param": "max_completion_tokens",
        },
        "anthropic": {
            "label": "Anthropic Claude",
            "api_type": "anthropic",
            "base_url": "https://api.anthropic.com/v1/messages",
            "models": (
                "claude-haiku-4-5",
                "claude-sonnet-4-6",
                "claude-opus-4-8",
                "claude-opus-4-7",
                "claude-haiku-4-5-20251001",
            ),
            "login_url": "https://console.anthropic.com/settings/keys",
            "hint": "Claude 主線（2026-06 查證）：Opus 4.8 / Sonnet 4.6 / Haiku 4.5；大量批次建議 Haiku 4.5，品質優先用 Sonnet/Opus。"
                    "Claude 3.x 全系列與 claude-*-4-20250514 已退役，已移除。",
            "requires_key": True,
        },
        "gemini": {
            "label": "Google Gemini",
            "api_type": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "models": (
                "gemini-3.1-flash-lite",
                "gemini-3.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-3.1-pro-preview",
            ),
            "login_url": "https://aistudio.google.com/app/apikey",
            "hint": "Gemini 官方 API（2026-06 查證）；大量翻譯建議 gemini-3.1-flash-lite，品質優先用 gemini-3.5-flash。"
                    "免費額度只涵蓋 Flash 系列（約 10 RPM/1500 次日）。2.0 系列已退役，已移除。",
            "requires_key": True,
        },
        "deepseek": {
            "label": "DeepSeek",
            "api_type": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "models": (
                "deepseek-v4-flash",
                "deepseek-v4-pro",
            ),
            "login_url": "https://platform.deepseek.com/api_keys",
            "hint": "DeepSeek 官方 V4（2026-06 查證）：批次翻譯用 deepseek-v4-flash，品質用 deepseek-v4-pro。"
                    "deepseek-chat / deepseek-reasoner 2026-07-24 完全停用，已移除。",
            "requires_key": True,
        },
        "kimi": {
            "label": "Kimi / Moonshot",
            "api_type": "openai_compatible",
            "base_url": "https://api.moonshot.cn/v1",
            "models": (
                "kimi-k2.6",
                "kimi-k2.5",
                "moonshot-v1-8k",
                "moonshot-v1-32k",
                "moonshot-v1-128k",
            ),
            "login_url": "https://platform.kimi.ai/",
            "hint": "Kimi（2026-06 查證）：主力 kimi-k2.6（需 temperature=1）。"
                    "kimi-k2-*-preview / k2-thinking 系列已於 2026-05-25 停用，已移除。"
                    "國際站帳號請把 Base URL 改成 https://api.moonshot.ai/v1。",
            "requires_key": True,
            "extra_body": {"temperature": 1},
            "request_timeout": (10, 180),
        },
        "xiaomi_mimo": {
            "label": "Xiaomi MiMo",
            "api_type": "openai_compatible",
            "base_url": "https://api.xiaomimimo.com/v1",
            "models": (
                "mimo-v2.5-pro",
                "mimo-v2.5",
            ),
            "login_url": "https://platform.xiaomimimo.com/",
            "hint": "Xiaomi MiMo（2026-06 查證）：文字旗艦 mimo-v2.5-pro。"
                    "V2 系列（v2-pro/v2-omni/v2-flash）2026-06-30 完全停用，已移除。需 MiMo API Key。",
            "requires_key": True,
            "token_param": "max_completion_tokens",
            "extra_body": {
                "temperature": 0.1,
                "top_p": 0.95,
                "thinking": {"type": "disabled"},
            },
        },
        "qwen": {
            "label": "Qwen / Alibaba Cloud",
            "api_type": "openai_compatible",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "models": (
                "qwen-mt-turbo",
                "qwen-mt-plus",
                "qwen-mt-flash",
                "qwen3.5-flash",
                "qwen3.5-plus",
                "qwen-flash",
                "qwen-plus",
                "qwen-max",
                "qwen-turbo",
            ),
            "login_url": "https://modelstudio.console.alibabacloud.com/",
            "hint": "Qwen / DashScope（2026-06 查證）：qwen-mt-* 是翻譯專用模型（92 語言、支援術語表、最便宜 $0.5/M），"
                    "批次翻譯首推 qwen-mt-turbo。通用模型用 qwen3.5-flash/plus。"
                    "預設國際 endpoint，中國大陸請改 https://dashscope.aliyuncs.com/compatible-mode/v1。",
            "requires_key": True,
        },
        "deepseek_v4_flash_free": {
            "label": "DeepSeek V4 Flash Free (OpenRouter)",
            "api_type": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "models": ("deepseek/deepseek-v4-flash:free",),
            "login_url": "https://openrouter.ai/settings/keys",
            "hint": "OpenRouter 免費變體；價格標示 $0 input / $0 output，但仍需 OpenRouter API Key，且可能有流量/可用性限制。",
            "requires_key": True,
        },
        "openrouter_free_router": {
            "label": "OpenRouter Free Router",
            "api_type": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "models": ("openrouter/free",),
            "login_url": "https://openrouter.ai/settings/keys",
            "hint": "OpenRouter 免費模型路由；模型填 openrouter/free，會自動選可用免費模型。仍需 OpenRouter API Key。",
            "requires_key": True,
        },
        "openrouter_free_models": {
            "label": "OpenRouter 免費模型",
            "api_type": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "models": (
                "qwen/qwen3-next-80b-a3b-instruct:free",
                "openrouter/free",
                "moonshotai/kimi-k2.6:free",
                "qwen/qwen3-coder:free",
                "openai/gpt-oss-120b:free",
                "openai/gpt-oss-20b:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "google/gemma-4-31b-it:free",
                "google/gemma-4-26b-a4b-it:free",
            ),
            "login_url": "https://openrouter.ai/settings/keys",
            "hint": "OpenRouter 免費模型（2026-06 查證；中文翻譯首推 qwen3-next-80b）。"
                    "價格 $0 仍需 OpenRouter API Key；限流約 20 次/分、每日 50~200 次。清單變動快，以 openrouter.ai/models 為準。",
            "requires_key": True,
        },
        "mistral": {
            "label": "Mistral AI",
            "api_type": "openai_compatible",
            "base_url": "https://api.mistral.ai/v1",
            "models": (
                "mistral-small-latest",
                "mistral-small-4-0-26-03",
                "mistral-medium-3-5-26-04",
                "mistral-medium-latest",
                "mistral-large-3-25-12",
                "mistral-large-latest",
                "ministral-3-14b-25-12",
                "ministral-3-8b-25-12",
            ),
            "login_url": "https://console.mistral.ai/api-keys/",
            "hint": "Mistral（2026-06 查證）：批次翻譯用 mistral-small-4-0（$0.15/M 輸入），品質用 mistral-medium-3-5 / large-3。"
                    "Small 3.x / Large 2.1 / Magistral 舊款已退役，已移除。",
            "requires_key": True,
        },
        "xai": {
            "label": "xAI Grok",
            "api_type": "openai_compatible",
            "base_url": "https://api.x.ai/v1",
            "models": (
                "grok-4.3",
                "grok-4.20-0309-non-reasoning",
                "grok-4.20-0309-reasoning",
                "grok-4.20-multi-agent-0309",
                "grok-build-0.1",
            ),
            "login_url": "https://console.x.ai/",
            "hint": "xAI Grok（2026-06 查證）：旗艦 grok-4.3（$1.25/$2.50，1M context）。"
                    "grok-3 / grok-4 / grok-4.1-fast 舊款已下架（請求會自動轉導到 4.3 計費），已移除。",
            "requires_key": True,
        },
        "perplexity": {
            "label": "Perplexity Sonar",
            "api_type": "openai_compatible",
            "base_url": "https://api.perplexity.ai",
            "models": (
                "sonar",
                "sonar-pro",
                "sonar-reasoning",
                "sonar-reasoning-pro",
                "sonar-deep-research",
            ),
            "login_url": "https://www.perplexity.ai/settings/api",
            "hint": "Perplexity Sonar 支援 OpenAI Chat Completions；適合需要網路查證的翻譯/說明，一般大量翻譯建議先用 sonar。",
            "requires_key": True,
        },
        "groq": {
            "label": "Groq",
            "api_type": "openai_compatible",
            "base_url": "https://api.groq.com/openai/v1",
            "models": (
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3-32b",
                "llama-3.1-8b-instant",
                "llama-3.3-70b-versatile",
                "meta-llama/llama-4-maverick-17b-128e-instruct",
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "moonshotai/kimi-k2-instruct-0905",
            ),
            "login_url": "https://console.groq.com/keys",
            "hint": "Groq 低延遲模型平台；實際可用模型取決於帳號權限。",
            "requires_key": True,
        },
        "together": {
            "label": "Together AI",
            "api_type": "openai_compatible",
            "base_url": "https://api.together.ai/v1",
            "models": (
                "Qwen/Qwen3-235B-A22B-Instruct-2507",
                "Qwen/Qwen3-32B",
                "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
                "moonshotai/Kimi-K2-Instruct-0905",
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
            ),
            "login_url": "https://api.together.ai/settings/api-keys",
            "hint": "Together AI 是 OpenAI-compatible 開源模型平台；模型 ID 遵循 provider/model_name，可自行貼上模型庫中的 ID。",
            "requires_key": True,
        },
        "fireworks": {
            "label": "Fireworks AI",
            "api_type": "openai_compatible",
            "base_url": "https://api.fireworks.ai/inference/v1",
            "models": (
                "accounts/fireworks/models/kimi-k2-instruct-0905",
                "accounts/fireworks/models/qwen3-235b-a22b-instruct-2507",
                "accounts/fireworks/models/deepseek-v3p1",
                "accounts/fireworks/models/llama-v4-maverick-instruct-basic",
                "accounts/fireworks/models/llama-v3p1-8b-instruct",
                "accounts/fireworks/models/llama-v3p1-70b-instruct",
            ),
            "login_url": "https://app.fireworks.ai/settings/users/api-keys",
            "hint": "Fireworks AI 提供 OpenAI-compatible serverless 模型；實際可用模型請以 Fireworks model library 為準。",
            "requires_key": True,
        },
        "openrouter": {
            "label": "OpenRouter",
            "api_type": "openai_compatible",
            "base_url": "https://openrouter.ai/api/v1",
            "models": (
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
                "openai/gpt-5.2",
                "openai/gpt-5.2-pro",
                "openai/gpt-5.5",
                "openai/gpt-5.4-mini",
                "anthropic/claude-opus-4.7",
                "anthropic/claude-opus-4.8",
                "anthropic/claude-sonnet-4.6",
                "anthropic/claude-haiku-4.5",
                "google/gemini-3.5-flash",
                "google/gemini-3.1-flash-lite",
                "google/gemini-3.1-pro-preview",
                "qwen/qwen3.7-max",
                "qwen/qwen3.7-plus",
                "qwen/qwen3.5-flash",
                "x-ai/grok-4.3",
                "x-ai/grok-4.20",
                "x-ai/grok-4-fast",
                "moonshotai/kimi-k2.6",
                "mistralai/mistral-medium-3-5",
                "mistralai/mistral-large-3",
                "perplexity/sonar",
                "openrouter/auto",
            ),
            "login_url": "https://openrouter.ai/settings/keys",
            "hint": "OpenRouter 可用同一組 API Key 路由多家模型；也可手動輸入任意模型 id。",
            "requires_key": True,
        },


        "cohere": {
            "label": "Cohere",
            "api_type": "openai_compatible",
            "base_url": "https://api.cohere.ai/compatibility/v1",
            "models": (
                "command-a-translate-08-2025",
                "command-a-03-2025",
            ),
            "login_url": "https://dashboard.cohere.com/api-keys",
            "hint": "Cohere Command A Translate 是翻譯專用模型（23 語言）；Trial Key 免費可試用。"
                    "注意：官方語言清單未明列繁中，建議先小批測試品質。",
            "requires_key": True,
        },
        "sambanova": {
            "label": "SambaNova",
            "api_type": "openai_compatible",
            "base_url": "https://api.sambanova.ai/v1",
            "models": (
                "DeepSeek-V3.2",
                "DeepSeek-V3.1",
                "gpt-oss-120b",
                "Meta-Llama-3.3-70B-Instruct",
                "Llama-4-Maverick-17B-128E-Instruct",
                "MiniMax-M2.7",
                "gemma-4-31B-it",
                "gemma-3-12b-it",
            ),
            "login_url": "https://cloud.sambanova.ai/",
            "hint": "SambaNova Cloud，OpenAI-compatible；公開模型 API 目前列出 DeepSeek-V3.2、GPT-OSS、Llama、Gemma、MiniMax 等，適合付費高速翻譯。",
            "requires_key": True,
        },
        "cerebras": {
            "label": "Cerebras",
            "api_type": "openai_compatible",
            "base_url": "https://api.cerebras.ai/v1",
            "models": (
                "gpt-oss-120b",
                "llama-3.3-70b",
                "qwen-3-235b-a22b-instruct-2507",
                "qwen-3-32b",
                "llama3.1-8b",
            ),
            "login_url": "https://cloud.cerebras.ai/",
            "hint": "Cerebras Inference，高速 OpenAI-compatible API；部分舊模型如 llama3.1-8b、qwen-3-235b-a22b-instruct-2507 已有淘汰日期，建議優先用 GPT-OSS / Llama 3.3 / Qwen 新項目。",
            "requires_key": True,
        },
        "custom": {
            "label": "本地/自訂 OpenAI-compatible",
            "api_type": "openai_compatible",
            "base_url": "http://localhost:1234/v1",
            "models": (
                "local-model",
                "qwen2.5-72b-instruct",
                "llama-3.3-70b-instruct",
            ),
            "login_url": "",
            "hint": "用於 LM Studio、Ollama、vLLM、LiteLLM 或公司內部 OpenAI-compatible 服務。",
            "requires_key": False,
        },
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Minecraft 模組翻譯器")
        self.root.configure(bg=self.C_BG)
        self.root.resizable(True, True)

        self.analyzed_jars    = {}
        self.analyzed_book_texts = {}  # {jar_path: {book_txt_path_in_jar: raw_text}}
        self.analyzed_book_text_repairs = {}  # {jar_path: {zh_tw_book_txt_path_in_jar: raw_text}}
        self.analyzed_static_assets = {}  # {jar_path: {zh_tw_path_in_jar: parsed_json/raw_text}}：可安全直接搬到資源包的既有翻譯
        self.analyzed_class_texts = {}  # {jar_path: {class_path_in_jar: [hardcoded tooltip strings]}}
        self.analyzed_loose   = []   # lang/en_us.json 路徑清單
        self.analyzed_loose_base = {}  # {en_us_path: zh_tw_dict}：散落檔既有翻譯基底
        self.analyzed_extra   = []   # (type, path)：snbt / md / patchouli JSON / quest JSON
        self.analyzed_zip_json = []  # (zip_path, internal_json_path)：Paxi/datapack/resourcepack ZIP 內 JSON
        self.analyzed_mc_dir  = ""   # 分析時使用的資料夾（rel_path 基準，與翻譯時保持一致）
        self.analyzed_jars_zh_base = {}  # {jar_path: {en_us_path_in_jar: zh_tw_dict}} 既有 zh_tw 基底
        self.is_processing   = False
        self.stop_requested  = False
        self.pause_requested = False
        self._jar_lock       = threading.Lock()
        self._cache_lock     = threading.Lock()   # 快取讀寫鎖
        self.last_save_time  = 0.0
        self._save_timer     = None   # 防抖動自動儲存計時器
        self._progress_started_at = time.time()
        self._progress_error_count = 0
        self._progress_translated_count = 0
        self._progress_skipped_count = 0
        self._progress_explicit_counts = False
        self.translation_cache = {}
        self._analysis_total_strings = 0
        self._analysis_cache_hits = 0
        self._analysis_memory_hits = 0
        self._analysis_missing_strings = 0
        self._analysis_changed_files = 0
        self._analysis_unchanged_files = 0
        self._last_output_path = ""

        # exe（PyInstaller frozen）時用 sys.executable 所在目錄；
        # 直接跑 .py 時用 __file__ 所在目錄，確保快取/設定檔位置固定
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_file_ai = os.path.join(self.base_dir, 'translation_cache.json')
        self.cache_file_std = os.path.join(self.base_dir, 'translation_cache_gtx.json')
        self.cache_file = self.cache_file_ai
        # 舊版曾使用 dictionary_zh_tw.json 做固定詞典命中；這會讓跨模組翻譯
        # 變成硬編碼清單。保留路徑只為相容舊設定/檔案，不再自動載入或套用。
        self.dictionary_file = os.path.join(self.base_dir, 'dictionary_zh_tw.json')
        self.config_file = os.path.join(self.base_dir, 'app_config.json')
        self.global_memory_file = os.path.join(self.base_dir, 'translation_memory_pool.json')
        self.update_index_file = os.path.join(self.base_dir, 'translation_update_index.json')
        self.failed_items_dir = os.path.join(self.base_dir, 'Failed Items')
        self.translation_dictionary = {}
        self.translation_memory = {}
        self._window_icon_image = None
        self._window_icon_handles = []
        self._log_queue = deque()
        self._log_lock = threading.Lock()
        self._log_polling = False
        # Tracks last provider preset Base URL so user-edited URLs are kept.
        self._last_ai_default_url = None

        self._configure_window_icon()
        self.setup_ui_imagegen()
        self.root.after(250, self._configure_window_icon)
        self._force_retranslate_this_start = (
            os.environ.get("MCT_FORCE_RETRANSLATE", "").strip() == "1")
        self._auto_translate_after_analysis = (
            os.environ.get("MCT_AUTO_TRANSLATE", "").strip() == "1")
        if self._force_retranslate_this_start:
            self.process_mode_var.set("force")
            self.log("INFO  本次啟動已啟用「強制重翻」（只影響目前這次啟動）。")
        if os.environ.get("MCT_AUTO_ANALYZE", "").strip() == "1":
            self.root.after(800, self._auto_start_analysis)

        # 綁定視窗關閉事件，確保正確清理資源（特別是 shelve 快取）
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ═══════════════════════════════════════════════
    #  UI 建立
    # ═══════════════════════════════════════════════
    def _resource_path(self, filename):
        bundle_dir = getattr(sys, '_MEIPASS', None)
        if bundle_dir:
            bundled = os.path.join(bundle_dir, filename)
            if os.path.exists(bundled):
                return bundled
        return os.path.join(self.base_dir, filename)

    def _configure_window_icon(self):
        ico_path = self._resource_path('app_icon.ico')
        png_path = self._resource_path('app_icon.png')

        try:
            if os.path.exists(ico_path):
                self.root.iconbitmap(default=ico_path)
                self.root.iconbitmap(ico_path)
        except tk.TclError:
            pass

        try:
            if os.path.exists(png_path):
                self._window_icon_image = tk.PhotoImage(file=png_path)
                self.root.iconphoto(True, self._window_icon_image)
        except tk.TclError:
            pass

        self._apply_windows_window_icon(ico_path)

    def _apply_windows_window_icon(self, ico_path):
        if os.name != 'nt' or not os.path.exists(ico_path):
            return
        try:
            import ctypes

            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            user32 = ctypes.windll.user32
            image_icon = 1
            lr_loadfromfile = 0x0010
            wm_seticon = 0x0080
            icon_small = 0
            icon_big = 1

            loaded = []
            for size, icon_type in ((16, icon_small), (32, icon_big)):
                hicon = user32.LoadImageW(
                    None, ico_path, image_icon, size, size, lr_loadfromfile)
                if hicon:
                    user32.SendMessageW(hwnd, wm_seticon, icon_type, hicon)
                    loaded.append((hicon, icon_type))

            # Keep icon handles alive for the lifetime of the Tk window.
            self._window_icon_handles.extend(hicon for hicon, _ in loaded)

            if not loaded:
                return

            set_class_long_ptr = getattr(user32, 'SetClassLongPtrW', None)
            if set_class_long_ptr is None:
                set_class_long_ptr = user32.SetClassLongW
            set_class_long_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            set_class_long_ptr.restype = ctypes.c_void_p

            gclp_hicon = -14
            gclp_hiconsm = -34
            big_icon = next((hicon for hicon, typ in loaded if typ == icon_big), loaded[-1][0])
            small_icon = next((hicon for hicon, typ in loaded if typ == icon_small), loaded[0][0])
            set_class_long_ptr(hwnd, gclp_hicon, big_icon)
            set_class_long_ptr(hwnd, gclp_hiconsm, small_icon)
        except Exception:
            pass

    def _make_card(self, parent, title, icon=""):
        outer = tk.Frame(parent, bg=self.C_BORDER, bd=0)
        inner = tk.Frame(outer, bg=self.C_SURFACE, bd=0)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        if title:
            hdr = tk.Frame(inner, bg=self.C_ACCENT, height=2)
            hdr.pack(fill=tk.X, side=tk.TOP)
            title_lbl = tk.Label(
                inner, text=f"  {icon}  {title}" if icon else f"  {title}",
                bg=self.C_SURFACE, fg=self.C_ACCENT,
                font=("微軟正黑體", 10, "bold"), anchor="w")
            title_lbl.pack(fill=tk.X, padx=8, pady=(6, 2))
            sep = tk.Frame(inner, bg=self.C_BORDER, height=1)
            sep.pack(fill=tk.X, padx=8, pady=(0, 6))
        body = tk.Frame(inner, bg=self.C_SURFACE)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(2, 10))
        return outer, body

    def _make_collapsible_card(self, parent, title, icon="", default_open=True):
        """可折疊卡片：點擊標題列切換展開／收起，▼ 展開、▶ 收起。"""
        outer = tk.Frame(parent, bg=self.C_BORDER, bd=0)
        inner = tk.Frame(outer, bg=self.C_SURFACE, bd=0)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # ── 頂部彩色細條 ──
        hdr_bar = tk.Frame(inner, bg=self.C_ACCENT, height=2)
        hdr_bar.pack(fill=tk.X, side=tk.TOP)
        hdr_bar.pack_propagate(False)

        # ── 可點擊標題列 ──
        is_open = [default_open]
        arrow   = "  ▼" if default_open else "  ▶"
        prefix  = f"  {icon}  {title}" if icon else f"  {title}"

        title_btn = tk.Button(
            inner,
            text=prefix + arrow,
            bg=self.C_SURFACE, fg=self.C_ACCENT,
            activebackground=self.C_BORDER, activeforeground=self.C_ACCENT,
            font=("微軟正黑體", 10, "bold"),
            anchor="w", relief="flat", bd=0, cursor="hand2",
            padx=8, pady=5)
        title_btn.pack(fill=tk.X, side=tk.TOP)
        title_btn.bind("<Enter>", lambda e: title_btn.config(bg=self.C_BORDER))
        title_btn.bind("<Leave>", lambda e: title_btn.config(bg=self.C_SURFACE))

        sep = tk.Frame(inner, bg=self.C_BORDER, height=1)
        sep.pack(fill=tk.X, padx=8, pady=(0, 4), side=tk.TOP)

        # ── 內容區 ──
        body = tk.Frame(inner, bg=self.C_SURFACE)

        def _toggle():
            if is_open[0]:
                body.pack_forget()
                title_btn.config(text=prefix + "  ▶")
                is_open[0] = False
            else:
                body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(2, 10))
                title_btn.config(text=prefix + "  ▼")
                is_open[0] = True

        title_btn.config(command=_toggle)

        if default_open:
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(2, 10))

        return outer, body

    def _make_label(self, parent, text, muted=False):
        fg = self.C_MUTED if muted else self.C_TEXT
        return tk.Label(parent, text=text, bg=self.C_SURFACE, fg=fg,
                        font=("微軟正黑體", 9))

    def _make_entry(self, parent, textvariable, width=55, show=""):
        kwargs = dict(
            textvariable=textvariable, width=width,
            bg=self.C_ENTRY_BG, fg=self.C_ENTRY_FG,
            insertbackground=self.C_ACCENT,
            relief="flat", bd=0,
            font=("Consolas", 9),
            highlightthickness=1,
            highlightcolor=self.C_ACCENT,
            highlightbackground=self.C_BORDER,
        )
        if show:
            kwargs["show"] = show
        return tk.Entry(parent, **kwargs)

    def _make_browse_btn(self, parent, command):
        btn = tk.Button(
            parent, text="📂  瀏覽", command=command,
            bg=self.C_BORDER, fg=self.C_TEXT,
            activebackground=self.C_ACCENT, activeforeground="#ffffff",
            font=("微軟正黑體", 9), relief="flat", bd=0,
            cursor="hand2", padx=10, pady=4)
        btn.bind("<Enter>", lambda e: btn.config(bg=self.C_ACCENT, fg="#ffffff"))
        btn.bind("<Leave>", lambda e: btn.config(bg=self.C_BORDER, fg=self.C_TEXT))
        return btn

    def _make_action_btn(self, parent, text, command, color, state=tk.NORMAL):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=color, fg="#ffffff",
            activebackground=color, activeforeground="#ffffff",
            font=("微軟正黑體", 10, "bold"), relief="flat", bd=0,
            cursor="hand2", padx=16, pady=8,
            state=state,
            disabledforeground="#666688")
        def on_enter(e):
            if btn["state"] != tk.DISABLED:
                btn.config(bg=self._lighten(color))
        def on_leave(e):
            if btn["state"] != tk.DISABLED:
                btn.config(bg=color)
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn._base_color = color
        return btn

    @staticmethod
    def _lighten(hex_color, factor=0.25):
        hex_color = hex_color.lstrip('#')
        r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        r = min(255, int(r + (255 - r) * factor))
        g = min(255, int(g + (255 - g) * factor))
        b = min(255, int(b + (255 - b) * factor))
        return f"#{r:02x}{g:02x}{b:02x}"

    def setup_ui_imagegen(self):
        """Imagegen mockup-inspired dashboard UI."""
        bg = "#071016"
        panel = "#111c24"
        panel2 = "#0d171f"
        border = "#263640"
        entry_bg = "#0a1219"
        text = "#d8e2e7"
        muted = "#8fa1aa"
        cyan = "#27d4d1"
        green = "#70d66b"
        amber = "#ffd15f"
        red = "#ff5a52"

        self.root.configure(bg=bg)
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.columnconfigure(2, weight=0)
        self.root.rowconfigure(0, weight=3)
        # 日誌列也給 weight：視窗放大時多出的空間分給日誌區，而不是留一塊空白；
        # minsize 保證小視窗時執行記錄不會被擠到只剩標題
        self.root.rowconfigure(1, weight=1, minsize=150)
        self.root.rowconfigure(2, weight=0)

        # 統一 Combobox 下拉清單配色（預設白底黑字在暗色主題上非常突兀）
        self.root.option_add("*TCombobox*Listbox.background", entry_bg)
        self.root.option_add("*TCombobox*Listbox.foreground", text)
        self.root.option_add("*TCombobox*Listbox.selectBackground", cyan)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#000000")

        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor="#1c2a32",
                        background=cyan,
                        bordercolor=border,
                        lightcolor=cyan,
                        darkcolor=cyan)
        style.configure("TSpinbox",
                        fieldbackground=entry_bg,
                        foreground=text,
                        background=border,
                        arrowcolor=text)
        style.configure("Dark.TCombobox",
                        fieldbackground=entry_bg,
                        foreground=text,
                        background=border,
                        arrowcolor=text)
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", entry_bg), ("!disabled", entry_bg)],
                  foreground=[("readonly", text), ("!disabled", text)],
                  background=[("readonly", border), ("!disabled", border)])

        def make_panel(parent, title):
            outer = tk.Frame(parent, bg=border, bd=0)
            inner = tk.Frame(outer, bg=panel, bd=0)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
            if title:
                tk.Label(inner, text=title, bg=panel, fg=text,
                         font=("微軟正黑體", 10, "bold"), anchor="w").pack(
                             fill=tk.X, padx=12, pady=(8, 3))
            body = tk.Frame(inner, bg=panel)
            body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(2, 8))
            return outer, body

        def label(parent, row, text_value):
            lbl = tk.Label(parent, text=text_value, bg=panel, fg=text,
                           font=("微軟正黑體", 9), width=12, anchor="w")
            lbl.grid(row=row, column=0, sticky="w", pady=3)
            return lbl

        def entry(parent, row, var, show="", width=36):
            ent = tk.Entry(parent, textvariable=var, width=width, show=show,
                           bg=entry_bg, fg=text, insertbackground=cyan,
                           relief="flat", bd=0, font=("Consolas", 10),
                           highlightthickness=1, highlightbackground=border,
                           highlightcolor=cyan)
            ent.grid(row=row, column=1, sticky="ew", pady=5, padx=(8, 8), ipady=5)
            ent.grid_configure(pady=3, ipady=4)
            return ent

        def small_button(parent, text_value, command, row, col):
            btn = tk.Button(parent, text=text_value, command=command,
                            bg="#17262f", fg=text,
                            activebackground="#1f3942", activeforeground="#ffffff",
                            font=("微軟正黑體", 9), relief="flat", bd=0,
                            cursor="hand2", padx=10, pady=5)
            btn.grid(row=row, column=col, sticky="ew", pady=5, padx=(0, 6))
            btn.grid_configure(pady=3)
            return btn

        def pill(parent, text_value, command, color, state=tk.NORMAL):
            btn = tk.Button(parent, text=text_value, command=command,
                            bg=color, fg="#ffffff",
                            activebackground=self._lighten(color, 0.16),
                            activeforeground="#ffffff",
                            disabledforeground="#6a7780",
                            font=("微軟正黑體", 10, "bold"),
                            relief="flat", bd=0, cursor="hand2",
                            padx=16, pady=6, state=state)
            btn._base_color = color
            return btn

        # Sidebar
        sidebar = tk.Frame(self.root, bg=panel2, width=190)
        sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text="▣  Minecraft 模組翻譯器",
                 bg=panel2, fg=text, font=("微軟正黑體", 11, "bold"),
                 anchor="w").pack(fill=tk.X, padx=14, pady=(14, 12))

        self._sidebar_nav = {}

        def add_nav(title, key, command):
            row = tk.Frame(sidebar, bg=panel2)
            row.pack(fill=tk.X, padx=10, pady=4)
            bar = tk.Frame(row, bg=panel2, width=3)
            bar.pack(side=tk.LEFT, fill=tk.Y)
            btn = tk.Button(row, text="   " + title, command=command,
                            bg=panel2, fg=text,
                            activebackground="#0c5b61", activeforeground="#ffffff",
                            font=("微軟正黑體", 11),
                            anchor="w", relief="flat", bd=0,
                            cursor="hand2", padx=0, pady=10)
            btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
            row.bind("<Button-1>", lambda _e: command())
            self._sidebar_nav[key] = (row, bar, btn)

        add_nav("專案", "project", lambda: self._focus_ui_section("project"))
        add_nav("翻譯引擎", "engine", lambda: self._focus_ui_section("engine"))
        add_nav("輸出", "output", lambda: self._focus_ui_section("output"))
        add_nav("快取", "cache", self.show_cache_summary)
        add_nav("記錄", "log", lambda: self._focus_ui_section("log"))
        add_nav("📦 安裝說明", "guide", self.show_install_guide)
        add_nav("🔍 覆蓋檢查", "coverage", self.run_coverage_check)

        # 依目前輸出模式顯示的迷你安裝提示（詳細說明點上面的「安裝說明」）
        self.sidebar_guide_label = tk.Label(
            sidebar, text="", bg=panel2, fg=muted,
            font=("微軟正黑體", 8), anchor="nw", justify=tk.LEFT, wraplength=160)
        self.sidebar_guide_label.pack(fill=tk.X, padx=16, pady=(10, 0))

        bottom_tools = tk.Frame(sidebar, bg=panel2)
        bottom_tools.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=(0, 18))
        for caption, command in [
            ("⚙", self.show_settings_summary),
            ("?", self.show_help),
            ("i", self.show_about),
        ]:
            tk.Button(bottom_tools, text=caption, command=command,
                      bg=panel2, fg=text,
                      activebackground="#17262f", activeforeground="#ffffff",
                      font=("微軟正黑體", 15),
                      relief="flat", bd=0, cursor="hand2",
                      width=3).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(sidebar, text="v1.2.9", bg=panel2, fg=muted,
                 font=("Consolas", 9), anchor="w").pack(
                     side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 10))

        # Main center —— 可捲動：視窗縮小時內容不會被截斷或互相遮擋
        center_holder = tk.Frame(self.root, bg=bg)
        center_holder.grid(row=0, column=1, sticky="nsew", padx=(14, 4), pady=(14, 8))
        center_holder.columnconfigure(0, weight=1)
        center_holder.rowconfigure(0, weight=1)
        center_canvas = tk.Canvas(center_holder, bg=bg, highlightthickness=0, bd=0)
        center_canvas.grid(row=0, column=0, sticky="nsew")
        center_vbar = tk.Scrollbar(center_holder, orient="vertical",
                                   command=center_canvas.yview,
                                   bg=border, troughcolor=entry_bg,
                                   activebackground=cyan, relief="flat", width=10)
        center_vbar.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        center_canvas.configure(yscrollcommand=center_vbar.set)
        center = tk.Frame(center_canvas, bg=bg)
        center_window = center_canvas.create_window((0, 0), window=center, anchor="nw")
        center.columnconfigure(0, weight=1)
        center.bind("<Configure>", lambda e: center_canvas.configure(
            scrollregion=center_canvas.bbox("all")))
        center_canvas.bind("<Configure>", lambda e: center_canvas.itemconfig(
            center_window, width=e.width))

        def _center_mousewheel(event):
            # 只有滑鼠落在中欄範圍內才捲動中欄；日誌區等維持原生捲動。
            # 順帶防止滾輪誤觸 Combobox 改值。
            try:
                px, py = self.root.winfo_pointerxy()
                target = self.root.winfo_containing(px, py)
            except (tk.TclError, KeyError):
                return None
            while target is not None:
                if target is center_canvas:
                    center_canvas.yview_scroll(int(-event.delta / 120), "units")
                    return "break"
                target = getattr(target, "master", None)
            return None
        self.root.bind_all("<MouseWheel>", _center_mousewheel, add="+")
        self._center_canvas = center_canvas

        self.mod_dir_var = tk.StringVar()
        self.rp_dir_var = tk.StringVar()
        self.rp_name_var = tk.StringVar(value="Auto_Translated_Mods_zh_tw")
        self.datapack_name_var = tk.StringVar(value="")
        self.output_mode_var = tk.StringVar(value="jar_patch")
        self.mc_version_var = tk.StringVar(value="等待自動判定")
        self.datapack_format_var = tk.IntVar(value=15)
        self.process_mode_var = tk.StringVar(value="append")
        self.retry_count_var = tk.IntVar(value=3)
        self.scope_mod_lang_var = tk.BooleanVar(value=True)
        self.scope_books_var = tk.BooleanVar(value=True)
        self.scope_quests_var = tk.BooleanVar(value=True)
        self.datapack_output_var = tk.BooleanVar(value=True)
        self.global_memory_var = tk.BooleanVar(value=True)
        self.strict_whitelist_var = tk.BooleanVar(value=True)
        self.update_detect_var = tk.BooleanVar(value=True)
        self.include_large_backups_var = tk.BooleanVar(value=False)
        self.class_tooltip_patch_var = tk.BooleanVar(value=True)

        project_outer, project = make_panel(center, "專案設定")
        self.project_outer = project_outer
        project_outer.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        project.columnconfigure(1, weight=1)
        label(project, 0, "來源資料夾")
        entry(project, 0, self.mod_dir_var)
        small_button(project, "瀏覽...", self.browse_mod_dir, 0, 2)
        small_button(project, "開啟", lambda: self._open_folder_var(self.mod_dir_var), 0, 3)
        label(project, 1, "輸出資料夾")
        entry(project, 1, self.rp_dir_var)
        small_button(project, "瀏覽...", self.browse_rp_dir, 1, 2)
        small_button(project, "開啟", lambda: self._open_folder_var(self.rp_dir_var), 1, 3)

        label(project, 2, "輸出檔名")
        name_row = tk.Frame(project, bg=panel)
        name_row.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        name_row.columnconfigure(1, weight=1)
        name_row.columnconfigure(3, weight=1)
        tk.Label(name_row, text="資源包", bg=panel, fg=muted,
                 font=("微軟正黑體", 8)).grid(row=0, column=0, sticky="w", padx=(0, 6))
        tk.Entry(name_row, textvariable=self.rp_name_var, width=24,
                 bg=entry_bg, fg=text, insertbackground=cyan,
                 relief="flat", bd=0, font=("Consolas", 9),
                 highlightthickness=1, highlightbackground=border,
                 highlightcolor=cyan).grid(row=0, column=1, sticky="ew", padx=(0, 10), ipady=4)
        tk.Label(name_row, text="DataPack", bg=panel, fg=muted,
                 font=("微軟正黑體", 8)).grid(row=0, column=2, sticky="w", padx=(0, 6))
        tk.Entry(name_row, textvariable=self.datapack_name_var, width=24,
                 bg=entry_bg, fg=text, insertbackground=cyan,
                 relief="flat", bd=0, font=("Consolas", 9),
                 highlightthickness=1, highlightbackground=border,
                 highlightcolor=cyan).grid(row=0, column=3, sticky="ew", ipady=4)
        tk.Label(name_row, text="免加 .zip；DataPack 留空會自動使用「資源包名稱_Datapack」",
                 bg=panel, fg=muted, font=("微軟正黑體", 8),
                 anchor="w").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        label(project, 3, "輸出模式")
        self.output_mode_var.set("jar_patch")
        mode_badge = tk.Label(
            project, text="JAR 直接翻譯（免資源包）", bg="#0b6c70", fg=text,
            font=("微軟正黑體", 9, "bold"), padx=18, pady=6,
            anchor="center")
        mode_badge.grid(row=3, column=1, columnspan=3, sticky="ew", pady=6, padx=(8, 6))
        self.output_mode_hint = tk.Label(project, text="", bg=panel, fg=muted,
                                         font=("微軟正黑體", 8), anchor="w")
        self.output_mode_hint.grid(row=4, column=1, columnspan=3, sticky="w", padx=(8, 0))
        label(project, 5, "Minecraft 版本")
        version_row = tk.Frame(project, bg=panel)
        version_row.grid(row=5, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        version_row.columnconfigure(0, weight=1)
        tk.Label(
            version_row, textvariable=self.mc_version_var,
            bg=entry_bg, fg=cyan, font=("Consolas", 9, "bold"),
            padx=12, pady=5, anchor="w").grid(row=0, column=0, sticky="w")
        self.pack_format_hint = tk.Label(
            version_row, text="選擇來源後按分析，由程式自動判定", bg=panel, fg=muted,
            font=("微軟正黑體", 8), anchor="w")
        self.pack_format_hint.grid(row=0, column=1, sticky="w", padx=(12, 0))

        label(project, 6, "翻譯範圍")
        scope_row = tk.Frame(project, bg=panel)
        scope_row.grid(row=6, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        for caption, var in [
            ("模組介面", self.scope_mod_lang_var),
            ("手冊/指南", self.scope_books_var),
            ("FTB/KubeJS 任務", self.scope_quests_var),
            ("Data Pack/OpenLoader", self.datapack_output_var),
        ]:
            tk.Checkbutton(scope_row, text=caption, variable=var,
                           bg=panel, fg=text, activebackground=panel,
                           activeforeground=text, selectcolor=entry_bg,
                           font=("微軟正黑體", 8),
                           command=self._schedule_save).pack(side=tk.LEFT, padx=(0, 12))

        label(project, 7, "處理模式")
        process_row = tk.Frame(project, bg=panel)
        process_row.grid(row=7, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        process_row.columnconfigure(0, weight=1)
        process_row.columnconfigure(1, weight=1)
        process_row.columnconfigure(2, weight=1)
        for col, (caption, value) in enumerate([
            ("補缺", "append"),
            ("跳過 90%", "skip90"),
            ("強制重翻", "force"),
        ]):
            tk.Radiobutton(process_row, text=caption, variable=self.process_mode_var, value=value,
                           indicatoron=False, selectcolor="#0b6c70",
                           bg=entry_bg, fg=text,
                           activebackground="#123842", activeforeground=text,
                           font=("微軟正黑體", 8, "bold"),
                           relief="flat", bd=1, padx=10, pady=5).grid(
                               row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0))

        label(project, 8, "驗證重試")
        retry_row = tk.Frame(project, bg=panel)
        retry_row.grid(row=8, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        retry_spin = ttk.Spinbox(
            retry_row, from_=0, to=10, textvariable=self.retry_count_var,
            width=5, font=("Consolas", 9), style="TSpinbox")
        retry_spin.pack(side=tk.LEFT)
        tk.Label(retry_row, text="次；格式碼/佔位符驗證失敗時用小批次重試",
                 bg=panel, fg=muted, font=("微軟正黑體", 8)).pack(
                     side=tk.LEFT, padx=(8, 0))

        label(project, 9, "安全增量")
        safety_row = tk.Frame(project, bg=panel)
        safety_row.grid(row=9, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        for caption, var in [
            ("全域翻譯記憶池", self.global_memory_var),
            ("嚴格白名單防爆", self.strict_whitelist_var),
            ("只補缺漏 + 更新偵測", self.update_detect_var),
            ("伺服器大型 JAR 備份", self.include_large_backups_var),
        ]:
            tk.Checkbutton(safety_row, text=caption, variable=var,
                           bg=panel, fg=text, activebackground=panel,
                           activeforeground=text, selectcolor=entry_bg,
                           font=("微軟正黑體", 8),
                           command=self._schedule_save).pack(side=tk.LEFT, padx=(0, 14))

        # Engine settings
        engine_outer, engine_body = make_panel(center, "翻譯引擎設定")
        self.engine_outer = engine_outer
        engine_outer.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        engine_body.columnconfigure(1, weight=1)
        self.engine_var = tk.StringVar(value="market_ai")
        self.pack_format_var = tk.IntVar(value=15)
        self.workers_var = tk.IntVar(value=8)
        self.api_key_var = tk.StringVar()
        self.deepl_key_var = tk.StringVar()
        self.azure_key_var = tk.StringVar()
        self.azure_region_var = tk.StringVar(value="eastasia")
        self.azure_endpoint_var = tk.StringVar()
        self.claude_key_var = tk.StringVar()
        self.claude_model_var = tk.StringVar(value="claude-haiku-4-5-20251001")
        self.openai_key_var = tk.StringVar()
        self.openai_model_var = tk.StringVar(value="gpt-5.4-mini")
        self.ai_provider_menu_var = tk.StringVar(value="free")
        self.ai_provider_var = tk.StringVar(value="DeepSeek V4 Flash Free (OpenRouter)")
        self.ai_auth_mode_var = tk.StringVar(value="api")
        self.ai_api_key_var = tk.StringVar()
        self.ai_api_keys_var = tk.StringVar()
        self.ai_model_var = tk.StringVar(value="deepseek/deepseek-v4-flash:free")
        self.ai_base_url_var = tk.StringVar(value="https://openrouter.ai/api/v1")
        self.ai_login_url_var = tk.StringVar(value="https://openrouter.ai/settings/keys")
        self.auto_normalize_endpoint_var = tk.BooleanVar(value=True)
        self.local_url_var = tk.StringVar(value="http://localhost:1234/v1/chat/completions")

        label(engine_body, 0, "模型提供者")
        provider_row = tk.Frame(engine_body, bg=panel)
        provider_row.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3, padx=(8, 8))
        provider_row.columnconfigure(0, weight=1)
        route_row = tk.Frame(provider_row, bg=panel)
        route_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        route_row.columnconfigure(0, weight=1)
        route_row.columnconfigure(1, weight=1)
        for col, (caption, value) in enumerate([
            ("市面 AI 模型", "market_ai"),
            ("非 AI 翻譯鏈", "non_ai_chain"),
        ]):
            tk.Radiobutton(route_row, text=caption, variable=self.engine_var, value=value,
                           command=self._on_engine_route_change,
                           indicatoron=False, selectcolor="#0b6c70",
                           bg=entry_bg, fg=text,
                           activebackground="#123842", activeforeground=text,
                           font=("微軟正黑體", 8, "bold"),
                           relief="flat", bd=1, padx=10, pady=4).grid(
                               row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0))
        self.ai_provider_menu_row = tk.Frame(provider_row, bg=panel)
        menu_row = self.ai_provider_menu_row
        menu_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        menu_row.columnconfigure(0, weight=1)
        menu_row.columnconfigure(1, weight=1)
        for col, (caption, value) in enumerate([
            ("免費 / 免費額度", "free"),
            ("付費 / 商用", "paid"),
        ]):
            tk.Radiobutton(menu_row, text=caption, variable=self.ai_provider_menu_var, value=value,
                           command=self._on_ai_provider_menu_change,
                           indicatoron=False, selectcolor="#0b6c70",
                           bg=entry_bg, fg=text,
                           activebackground="#123842", activeforeground=text,
                           font=("微軟正黑體", 8, "bold"),
                           relief="flat", bd=1, padx=10, pady=4).grid(
                               row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0))
        self.ai_provider_cb = ttk.Combobox(provider_row, textvariable=self.ai_provider_var,
                                           values=self._ai_provider_labels(),
                                           state="readonly", font=("Consolas", 10),
                                           width=30, style="Dark.TCombobox")
        self.ai_provider_cb.grid(row=2, column=0, sticky="ew", ipady=3)
        self.ai_provider_cb.bind("<<ComboboxSelected>>", self._on_ai_provider_change)
        auth_row = tk.Frame(provider_row, bg=panel)
        self.ai_auth_row = auth_row
        auth_row.grid(row=2, column=1, sticky="e", padx=(8, 0))
        for col, (caption, value) in enumerate([("API Key", "api"), ("登入取得", "login")]):
            tk.Radiobutton(auth_row, text=caption, variable=self.ai_auth_mode_var, value=value,
                           indicatoron=False, selectcolor="#0b6c70",
                           bg=entry_bg, fg=text,
                           activebackground="#123842", activeforeground=text,
                           font=("微軟正黑體", 8, "bold"),
                           relief="flat", bd=1, padx=10, pady=4,
                           command=self._on_ai_auth_mode_change).grid(
                               row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0))

        self.ai_model_label = label(engine_body, 1, "模型")
        model_row = tk.Frame(engine_body, bg=panel)
        self.ai_model_row = model_row
        model_row.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3, padx=(8, 8))
        model_row.columnconfigure(0, weight=1)
        self.ai_model_cb = ttk.Combobox(model_row, textvariable=self.ai_model_var,
                                        values=list(self.AI_PROVIDER_PRESETS["deepseek_v4_flash_free"]["models"]),
                                        font=("Consolas", 10), width=38,
                                        style="Dark.TCombobox")
        self.ai_model_cb.grid(row=0, column=0, sticky="ew", ipady=4)
        self.ai_model_badge = tk.Label(model_row, text="待設定", bg=self.C_BORDER, fg="#ffffff",
                                       font=("微軟正黑體", 8, "bold"), padx=8, pady=3)
        self.ai_model_badge.grid(row=0, column=1, padx=(8, 0))

        self.ai_key_label = label(engine_body, 2, "API Key")
        self.ai_api_key_entry = entry(engine_body, 2, self.ai_api_key_var, show="●")
        self._ai_api_key_visible = False
        self.btn_show_ai_key = small_button(engine_body, "顯示", self.toggle_ai_api_key_visibility, 2, 2)
        self.btn_open_ai_login = small_button(engine_body, "開啟登入頁", self.open_ai_login_page, 2, 3)
        self.ai_base_label = label(engine_body, 3, "Base URL")
        self.ai_base_entry = entry(engine_body, 3, self.ai_base_url_var)
        self.ai_base_help = tk.Label(engine_body, text="?", bg=panel, fg=muted,
                                     font=("微軟正黑體", 11, "bold"))
        self.ai_base_help.grid(row=3, column=3, sticky="w")

        label(engine_body, 4, "連線狀態")
        status_line = tk.Frame(engine_body, bg=panel)
        status_line.grid(row=4, column=1, columnspan=3, sticky="ew", pady=4, padx=(8, 8))
        self.ai_status_labels = {}
        for key, caption, color_value in [
            ("connection", "● 未測試", muted),
            ("latency", "延遲：--", muted),
            ("model", "模型狀態：未測試", muted),
            ("plan", "方案：--", muted),
        ]:
            lbl = tk.Label(status_line, text=caption, bg=panel, fg=color_value,
                           font=("微軟正黑體", 9))
            lbl.pack(side=tk.LEFT, padx=(0, 18))
            self.ai_status_labels[key] = lbl
        self.ai_provider_hint = tk.Label(engine_body, text="",
                                         bg=panel, fg=muted,
                                         font=("微軟正黑體", 8),
                                         anchor="w", justify=tk.LEFT)
        self.ai_provider_hint.grid(row=5, column=1, columnspan=3,
                                   sticky="w", padx=(8, 8), pady=(0, 4))

        button_row = tk.Frame(engine_body, bg=panel)
        button_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(5, 0))
        # 用 grid 分「左工具鈕｜彈性間隔｜右動作鈕」三區：間隔欄吸收多餘寬度，
        # 視窗變窄時間隔收為 0、按鈕各自留在自己的子框內，永遠不會互相重疊
        button_row.columnconfigure(0, weight=0)
        button_row.columnconfigure(1, weight=1)   # 彈性間隔
        button_row.columnconfigure(2, weight=0)
        left_btns = tk.Frame(button_row, bg=panel)
        left_btns.grid(row=0, column=0, sticky="w")
        right_btns = tk.Frame(button_row, bg=panel)
        right_btns.grid(row=0, column=2, sticky="e")

        pill(left_btns, "Key 池", self.edit_ai_key_pool,
             "#17262f").pack(side=tk.LEFT, padx=(0, 8))
        pill(left_btns, "整理端點", self.normalize_ai_endpoint,
             "#17262f").pack(side=tk.LEFT, padx=(0, 8))
        pill(left_btns, "測試連線", self.test_ai_connection,
             "#17262f").pack(side=tk.LEFT)

        self.btn_stop = pill(right_btns, "停止", self.stop_process, "#9b3636", state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_pause = pill(right_btns, "暫停", self.pause_process, "#9a7b2f", state=tk.DISABLED)
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_analyze = pill(right_btns, "分析檔案", self.start_analysis, "#17262f")
        self.btn_analyze.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_translate = pill(right_btns, "▶  開始翻譯", self.start_translation,
                                  "#13a8a6", state=tk.DISABLED)
        self.btn_translate.pack(side=tk.LEFT)

        # 引擎鏈／執行緒建議文字移到自己的一列，並開啟自動換行，
        # 不再和按鈕搶同一列寬度（這正是先前「暫停」被擠到重疊的主因）
        self.workers_hint = tk.Label(engine_body, text="", bg=panel, fg=muted,
                                     font=("微軟正黑體", 8), anchor="w",
                                     justify=tk.LEFT, wraplength=560)
        self.workers_hint.grid(row=7, column=0, columnspan=4,
                               sticky="ew", padx=(4, 8), pady=(6, 2))

        # Hidden legacy provider frames, kept for existing engine switching/config behavior.
        self.key_frame = tk.Frame(engine_body, bg=panel)
        self.google_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.deepl_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.azure_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.claude_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.openai_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.market_ai_key_frame = tk.Frame(self.key_frame, bg=panel)
        self.local_key_frame = tk.Frame(self.key_frame, bg=panel)

        # Progress summary
        progress_outer, progress_body = make_panel(center, "進度摘要")
        self.progress_outer = progress_outer
        progress_outer.grid(row=2, column=0, sticky="ew")
        for col in range(6):
            progress_body.columnconfigure(col, weight=1, minsize=92)
        progress_body.columnconfigure(6, weight=0, minsize=150)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_body, variable=self.progress_var,
                                            maximum=100, style="Dark.Horizontal.TProgressbar")
        self.progress_bar.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 10))
        self.progress_label = tk.Label(progress_body, text="就緒", bg=panel, fg=muted,
                                       font=("微軟正黑體", 9), anchor="e")
        self.progress_label.grid(row=0, column=6, sticky="e", padx=(10, 0))
        self.progress_stat_labels = {}
        for col, (key, title, value, color_value) in enumerate([
            ("percent", "總進度", "0.0%", cyan),
            ("processed", "已處理", "0", text),
            ("translated", "已翻譯", "0", green),
            ("skipped", "已跳過", "0", "#5ca8ff"),
            ("errors", "錯誤", "0", red),
            ("eta", "預估剩餘時間", "--:--", text),
        ]):
            box = tk.Frame(progress_body, bg=panel)
            box.grid(row=1, column=col, sticky="ew", padx=(0, 14))
            box.columnconfigure(1, weight=1)
            tk.Label(box, text=title, bg=panel, fg=muted,
                     font=("微軟正黑體", 8)).grid(
                         row=0, column=0, sticky="w", padx=(0, 6))
            value_label = tk.Label(box, text=value, bg=panel, fg=color_value,
                                   font=("Consolas", 12, "bold"),
                                   anchor="w")
            value_label.grid(row=0, column=1, sticky="w")
            self.progress_stat_labels[key] = value_label

        # 目前處理項目：長任務時讓使用者知道現在卡在哪個檔案/階段
        self.current_item_label = tk.Label(progress_body, text="", bg=panel, fg=muted,
                                           font=("微軟正黑體", 8), anchor="w")
        self.current_item_label.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(6, 0))

        # Right summary cards
        right = tk.Frame(self.root, bg=bg, width=280)
        right.grid(row=0, column=2, sticky="nsew", padx=(0, 14), pady=(14, 8))
        right.grid_propagate(False)
        right.columnconfigure(0, weight=1)

        self.summary_card_labels = {}

        def stat_card(key, row, title, value, sub, value_color):
            outer, body = make_panel(right, title)
            outer.grid(row=row, column=0, sticky="ew", pady=(0, 12))
            value_label = tk.Label(body, text=value, bg=panel, fg=value_color,
                                   font=("Consolas", 24, "bold"), anchor="w")
            value_label.pack(anchor="w")
            sub_label = tk.Label(body, text=sub, bg=panel, fg=muted,
                                 font=("微軟正黑體", 9), anchor="w", justify=tk.LEFT,
                                 wraplength=230)
            sub_label.pack(anchor="w", pady=(4, 0))
            self.summary_card_labels[key] = {
                "value": value_label,
                "sub": sub_label,
            }

        stat_card("cache", 0, "快取命中", "待分析", "總計：--", green)
        stat_card("pending", 1, "待翻譯", "待分析", "總計：--", amber)
        stat_card("output", 2, "輸出模式", "依設定", "語言：zh_tw", text)
        stat_card("api", 3, "API 狀態", "待測試", "提供者：--\n模型：--", amber)
        right.lift()

        # Log area
        log_outer, log_body = make_panel(self.root, "執行記錄")
        self.log_outer = log_outer
        log_outer.grid(row=1, column=1, columnspan=2, sticky="nsew", padx=14, pady=(0, 8))
        log_body.columnconfigure(0, weight=1)
        log_body.rowconfigure(0, weight=1)   # 視窗放大時日誌區跟著長高
        self.log_area = scrolledtext.ScrolledText(
            log_body, state='disabled',
            bg="#05090d", fg="#d7e2e8",
            insertbackground=cyan,
            font=("Consolas", 9),
            relief="flat", bd=0, wrap=tk.WORD, height=6)
        self.log_area.grid(row=0, column=0, sticky="nsew")
        self.log_area.vbar.config(bg=border, troughcolor=entry_bg,
                                  activebackground=cyan, relief="flat", width=10)
        self._start_log_polling()

        footer = tk.Frame(self.root, bg="#060b10", height=28)
        footer.grid(row=2, column=1, columnspan=2, sticky="ew")
        footer.grid_propagate(False)
        self.status_label = tk.Label(footer, text="就緒", bg="#060b10", fg=muted,
                                     font=("微軟正黑體", 8), anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=10)
        self.footer_info_label = tk.Label(footer, text="", bg="#060b10", fg=muted,
                                          font=("微軟正黑體", 8), anchor="e")
        self.footer_info_label.pack(side=tk.RIGHT, padx=10)

        def _refresh_footer_info(*_):
            def apply():
                try:
                    self.footer_info_label.config(
                        text=f"執行緒：{self.workers_var.get()}    快取：{len(self.translation_cache):,} 筆")
                except Exception:
                    pass
            try:
                if self.root.winfo_exists():
                    self.root.after(0, apply)
            except Exception:
                pass
        self._refresh_footer_info = _refresh_footer_info
        self.workers_var.trace_add('write', _refresh_footer_info)
        _refresh_footer_info()
        right.lift()

        self._on_output_mode_change()
        self._on_mc_version_change()
        self._on_ai_provider_change()
        self._on_engine_change()
        self._set_sidebar_active("project")
        self.log("INFO  介面已載入：DeepSeek V4 Flash Free (OpenRouter) 已設為預設市面 AI 模型")
        self.load_config()
        self._on_engine_change()
        self._refresh_right_summary()

        for _sv in [self.mod_dir_var, self.rp_dir_var, self.rp_name_var,
                    self.datapack_name_var,
                    self.api_key_var, self.deepl_key_var,
                    self.azure_key_var, self.azure_region_var, self.azure_endpoint_var,
                    self.claude_key_var, self.claude_model_var,
                    self.openai_key_var, self.openai_model_var,
                    self.ai_provider_menu_var, self.ai_provider_var, self.ai_auth_mode_var,
                    self.ai_api_key_var, self.ai_api_keys_var, self.ai_model_var,
                    self.ai_base_url_var, self.ai_login_url_var,
                    self.engine_var, self.local_url_var,
                    self.pack_format_var, self.workers_var,
                    self.output_mode_var, self.mc_version_var,
                    self.datapack_format_var, self.process_mode_var,
                    self.retry_count_var,
                    self.scope_mod_lang_var, self.scope_books_var,
                    self.scope_quests_var, self.datapack_output_var,
                    self.global_memory_var, self.strict_whitelist_var,
                    self.update_detect_var, self.class_tooltip_patch_var,
                    self.auto_normalize_endpoint_var]:
            _sv.trace_add('write', self._schedule_save)
        self.mc_version_var.trace_add('write', self._on_mc_version_change)
        for _sv in [self.ai_provider_menu_var, self.ai_provider_var, self.ai_model_var, self.ai_base_url_var,
                    self.ai_api_key_var, self.ai_api_keys_var, self.engine_var]:
            _sv.trace_add('write', self._refresh_api_summary)
        self.output_mode_var.trace_add('write', self._refresh_output_summary)
        self.class_tooltip_patch_var.trace_add(
            'write', self._refresh_output_summary)
        self.datapack_output_var.trace_add('write', self._refresh_output_summary)

    def _set_summary_card(self, key, value=None, sub=None, color=None):
        def apply():
            cards = getattr(self, "summary_card_labels", {})
            card = cards.get(key)
            if not card:
                return
            if value is not None:
                card["value"].config(text=value)
            if sub is not None:
                card["sub"].config(text=sub)
            if color is not None:
                card["value"].config(fg=color)

        try:
            if self.root.winfo_exists():
                self.root.after(0, apply)
        except Exception:
            pass

    def _output_mode_summary(self):
        return "JAR 直接翻譯", "語言：zh_tw\n輸出：重建 mods/JAR + 低風險 class + 設定"

    def _refresh_output_summary(self, *args):
        value, sub = self._output_mode_summary()
        self._set_summary_card("output", value, sub, self.C_TEXT)

    def _on_mc_version_change(self, *args):
        version = self.mc_version_var.get().strip() if hasattr(self, "mc_version_var") else ""
        formats = self.MC_PACK_FORMATS.get(version)
        if formats is None:
            return
        if hasattr(self, "pack_format_var"):
            self.pack_format_var.set(formats["rp"])
        if hasattr(self, "datapack_format_var"):
            self.datapack_format_var.set(formats["dp"])
        hint = getattr(self, "pack_format_hint", None)
        if hint:
            hint.config(text=f"Resource Pack：{formats['rp']} / Data Pack：{formats['dp']}")

    def _is_book_path(self, path):
        p = (path or "").replace("\\", "/").lower()
        return any(m in p for m in (
            "/patchouli_books/", "/ae2guide/", "/guide/", "/guides/",
            "/guidebook/", "/guidebooks/", "/manual/", "/manuals/",
            "/lexicon/", "/research/", "/researches/", "/journal/",
            "/book/"))

    def _is_structured_book_json_path(self, path):
        p = (path or "").replace("\\", "/").lower()
        return p.endswith(".json") and (
            "/book/" in p
            or "/patchouli_books/" in p
            or "/ae2guide/" in p
            or "/guide/" in p
            or "/guides/" in p
            or "/guidebook/" in p
            or "/guidebooks/" in p
            or "/manual/" in p
            or "/manuals/" in p
            or "/journal/" in p
            or "/advancements/" in p
        )

    def _is_quest_path(self, path):
        p = (path or "").replace("\\", "/").lower()
        return any(m in p for m in (
            "ftbquests", "ftb_quests", "ftb-quests", "/quests/",
            "/advancements/", "puffish_skills",
            "heracles", "odyssey", "betterquesting", "customnpcs", "questbook"))

    @staticmethod
    def _is_openloader_resources_zip_path(path):
        p = (path or "").replace("\\", "/").lower()
        return "/config/openloader/resources/" in "/" + p and p.endswith(".zip")

    @staticmethod
    def _is_mmorpg_data_json_path(path):
        p = (path or "").replace("\\", "/").lower()
        return (p.endswith(".json")
                and "/config/openloader/data/" in "/" + p
                and "/data/mmorpg/" in p)

    def _scope_allows_analyzed_path(self, kind, path):
        if kind == "book_txt":
            return self.scope_books_var.get()
        if kind == "jar":
            if self._is_advancement_json_path(path):
                # 與收集端一致：advancement 歸「任務書」範圍管
                return self.scope_quests_var.get()
            if self._is_book_path(path):
                return self.scope_books_var.get()
            return self.scope_mod_lang_var.get()
        if kind == "loose":
            if self._is_quest_path(path):
                return self.scope_quests_var.get()
            if self._is_book_path(path):
                return self.scope_books_var.get()
            return self.scope_mod_lang_var.get()
        return True

    def _scope_allows_extra(self, file_type, path):
        if file_type == "snbt":
            return self.scope_quests_var.get()
        if file_type == "apoth_names":
            return (self.scope_mod_lang_var.get()
                    or getattr(self, "_server_mode", False))
        if file_type == "md":
            return self.scope_books_var.get() or self.scope_quests_var.get()
        if file_type == "mns_json":
            return self.scope_mod_lang_var.get()
        if file_type in ("json", "lang"):
            if self._is_quest_path(path):
                return self.scope_quests_var.get()
            if self._is_book_path(path):
                return self.scope_books_var.get()
            return self.scope_mod_lang_var.get()
        return True

    def _ai_plan_info(self):
        provider_key = self._ai_provider_key()
        cfg = self._ai_provider_config()
        model = self.ai_model_var.get().strip().lower()
        if (provider_key in ("deepseek_v4_flash_free", "openrouter_free_router", "openrouter_free_models")
                or model == "openrouter/free"
                or model.endswith(":free")):
            return "免費模型", "#4f8b38", "方案：免費模型"
        if provider_key in ("bing_free", "libretranslate", "custom") or not cfg.get("requires_key", True):
            return "本機/自架", self.C_ACCENT2, "方案：本機/自架"
        return "API 計費", self.C_WARN, "方案：依 API 帳號計費"

    def _refresh_ai_status_widgets(self, connection=None, latency=None, model_state=None):
        badge_text, badge_color, plan_text = self._ai_plan_info()
        if hasattr(self, "ai_model_badge"):
            self.ai_model_badge.config(text=badge_text, bg=badge_color)

        labels = getattr(self, "ai_status_labels", {})
        cfg = self._ai_provider_config()
        needs_key = cfg.get("requires_key", True)
        has_key = bool(self.ai_api_key_var.get().strip())
        if connection is None:
            if needs_key and not has_key:
                connection = "● 需 Key"
                conn_color = self.C_WARN
                model_state = model_state or "模型狀態：等待 API Key"
            else:
                connection = "● 未測試"
                conn_color = self.C_MUTED
                model_state = model_state or "模型狀態：未測試"
        else:
            conn_color = self.C_SUCCESS if "已連線" in connection else (
                self.C_DANGER if "失敗" in connection else self.C_WARN)

        updates = {
            "connection": (connection, conn_color),
            "latency": (latency or "延遲：--", self.C_MUTED),
            "model": (model_state or "模型狀態：未測試", self.C_MUTED),
            "plan": (plan_text, self.C_MUTED),
        }
        for key, (text, color) in updates.items():
            label = labels.get(key)
            if label:
                label.config(text=text, fg=color)

    def _engine_summary(self):
        engine = self._normalize_engine_route_value(self.engine_var.get())
        if engine == "market_ai":
            cfg = self._ai_provider_config()
            label = cfg.get("label", "AI")
            model = self.ai_model_var.get().strip() or "--"
            requires_key = cfg.get("requires_key", True)
            api_key = self.ai_api_key_var.get().strip()
            if requires_key and not api_key:
                state, color = "需 Key", self.C_WARN
            else:
                state, color = "已設定", self.C_SUCCESS
            return state, f"提供者：{label}\n模型：{model}", color
        engine_names = {
            "google": "Google API / GTX",
            "deepl": "DeepL",
            "azure": "Microsoft Azure",
            "claude": "Claude",
            "openai": "OpenAI",
            "local": "本地 AI",
            "non_ai_chain": "非 AI 翻譯鏈：僅用 GTX 高速大批次（遇 429 自動退避）",
        }
        label = engine_names.get(engine, engine or "--")
        return "已設定", f"引擎：{label}", self.C_SUCCESS

    def _refresh_api_summary(self, *args, state=None, color=None, sub=None):
        self._refresh_ai_status_widgets()
        if state is None or sub is None:
            current_state, current_sub, current_color = self._engine_summary()
            if state is None:
                state = current_state
            if sub is None:
                sub = current_sub
            if color is None:
                color = current_color
        self._set_summary_card("api", state, sub, color or self.C_TEXT)

    def _refresh_right_summary(self):
        self._refresh_output_summary()
        self._refresh_api_summary()
        total = getattr(self, "_analysis_total_strings", 0)
        if total > 0:
            hits = getattr(self, "_analysis_cache_hits", 0)
            mem_hits = getattr(self, "_analysis_memory_hits", 0)
            missing = getattr(self, "_analysis_missing_strings", 0)
            rate = hits / total * 100
            update_text = ""
            if self.update_detect_var.get():
                update_text = (f"\n更新：{getattr(self, '_analysis_changed_files', 0):,}/"
                               f"{getattr(self, '_analysis_unchanged_files', 0):,}")
            self._set_summary_card(
                "cache", f"{hits:,}", f"總計：{total:,}\n命中率：{rate:.1f}%\n記憶池：{mem_hits:,}{update_text}", self.C_SUCCESS)
            self._set_summary_card(
                "pending", f"{missing:,}", f"總計：{total:,}\n狀態：已分析", self.C_WARN if missing else self.C_SUCCESS)
        else:
            self._set_summary_card("cache", "待分析", "總計：--", self.C_MUTED)
            self._set_summary_card("pending", "待分析", "總計：--", self.C_MUTED)

    def _count_analysis_strings(self):
        self.set_current_item("統計待翻譯詞彙...", force=True)
        unique_strings = self.extract_all_unique_strings()
        self._analysis_unique_strings = unique_strings
        cache, messages = load_translation_cache(
            self._current_cache_file(), self._RE_FORMAT, self._is_valid_trad_translation)
        for message in messages:
            self.log(message)
        memory = self._load_translation_memory() if self.global_memory_var.get() else {}
        total = len(unique_strings)
        memory_hits = unique_strings & set(memory.keys())
        hits = len((unique_strings & set(cache.keys())) | memory_hits)
        missing = max(0, total - hits)
        self._analysis_total_strings = total
        self._analysis_cache_hits = hits
        self._analysis_memory_hits = len(memory_hits)
        self._analysis_missing_strings = missing
        return total, hits, missing

    def _load_translation_memory(self):
        if getattr(self, "translation_memory", None):
            return self.translation_memory
        pkl_file = self.global_memory_file + '.pkl'
        json_candidates = (self.global_memory_file, self.global_memory_file + '.tmp')
        try:
            if os.path.exists(pkl_file):
                with open(pkl_file, 'rb') as f:
                    data = pickle.load(f)
            else:
                data = {}
                for json_file in json_candidates:
                    if not os.path.exists(json_file):
                        continue
                    try:
                        with open(json_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        break
                    except (OSError, json.JSONDecodeError):
                        data = {}
            if isinstance(data, dict):
                self.translation_memory = {
                    sanitize_text(str(k)): sanitize_text(str(v)) for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
        except (OSError, json.JSONDecodeError, TypeError, pickle.PickleError, EOFError):
            self.translation_memory = {}
        return self.translation_memory

    def _save_translation_memory(self):
        snapshot = sanitize_value(dict(getattr(self, "translation_memory", {}) or {}))
        tmp_pkl = self.global_memory_file + '.pkl.tmp'
        tmp_json = self.global_memory_file + '.tmp'
        try:
            with open(tmp_pkl, 'wb') as f:
                pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_pkl, self.global_memory_file + '.pkl')

            with open(tmp_json, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp_json, self.global_memory_file)
        except Exception as e:
            for tmp in (tmp_pkl, tmp_json):
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            self.log(f"⚠️ 全域翻譯記憶池儲存失敗：{e}")

    def _add_memory_pair(self, source, target):
        if not self.global_memory_var.get():
            return False
        if not isinstance(source, str) or not isinstance(target, str):
            return False
        source = source.strip()
        source = sanitize_text(source)
        target = sanitize_text(self._to_traditional(target.strip()))
        if not source or not target or source == target:
            return False
        if not self.should_translate(source):
            return False
        if not self._is_valid_trad_translation(source, target):
            return False
        if (Counter(self._critical_format_tokens(source))
                != Counter(self._critical_format_tokens(target))):
            return False
        self.translation_memory[source] = target
        return True

    def _add_memory_from_lang_pair(self, source_data, target_data):
        added = 0
        if not isinstance(source_data, dict) or not isinstance(target_data, dict):
            return added
        for key, source in source_data.items():
            target = target_data.get(key)
            if self._add_memory_pair(source, target):
                added += 1
        return added

    def _build_global_memory_pool(self, mod_dir):
        if not self.global_memory_var.get():
            self.log("ℹ️ 全域翻譯記憶池已停用，跳過建立")
            return
        self.log("🧠 開始建立全域翻譯記憶池...")
        self.set_current_item("整理中：建立全域翻譯記憶池", force=True)
        try:
            self.translation_memory = self._load_translation_memory().copy()
            before = len(self.translation_memory)
            self.log(f"ℹ️ 載入既有記憶池：{before:,} 筆")
        except Exception as e:
            self.log(f"⚠️ 載入記憶池失敗：{e}，從空白開始")
            self.translation_memory = {}
            before = 0
        added = 0

        jar_bases = dict(getattr(self, "analyzed_jars_zh_base", {}) or {})
        self.log(f"ℹ️ 從已分析結果提取 JAR 記憶對：{len(jar_bases)} 個來源")
        for idx, (jar_path, zh_base_map) in enumerate(jar_bases.items(), 1):
            if self.stop_requested:
                break
            if idx == 1 or idx % 100 == 0:
                self.set_current_item(
                    f"整理記憶池：JAR {idx}/{len(jar_bases)} - {os.path.basename(jar_path)}")
            try:
                source_map = self.analyzed_jars.get(jar_path, {})
                for src_fn, target_data in zh_base_map.items():
                    src_data = source_map.get(src_fn)
                    added += self._add_memory_from_lang_pair(src_data, target_data)
            except Exception as e:
                self.log(f"⚠️ JAR 記憶提取失敗 {os.path.basename(jar_path)}: {e}")
                continue

        loose_paths = list(self.analyzed_loose)
        self.log(f"ℹ️ 從 {len(loose_paths)} 個散落語言檔提取記憶對...")
        for idx, path in enumerate(loose_paths, 1):
            if self.stop_requested:
                break
            if idx == 1 or idx % 100 == 0:
                self.set_current_item(
                    f"整理記憶池：語言檔 {idx}/{len(loose_paths)}")
            try:
                lang_dir = os.path.dirname(path)
                suffix = '.lang' if path.lower().endswith('.lang') else '.json'
                target_path = None
                for lang in ('zh_tw', 'zh_hk', 'zh_sg', 'zh_cn'):
                    cand = os.path.join(lang_dir, lang + suffix)
                    if os.path.exists(cand):
                        target_path = cand
                        break
                if not target_path:
                    continue
                src_data = load_lang_content(
                    self.safe_read_file(path), path, self._clean_json_text)
                target_data = load_lang_content(
                    self.safe_read_file(target_path), target_path, self._clean_json_text)
                added += self._add_memory_from_lang_pair(src_data, target_data)
            except Exception:
                continue

        # 從快取併入記憶池：跳過此步驟，避免大量 shelve 讀取卡死
        # （快取內容已在上面的 JAR/語言檔掃描中自動建立記憶對）
        self.log("ℹ️ 跳過快取併入記憶池（避免大量讀取卡死）")

        if self.stop_requested:
            self.log("⏸️ 全域翻譯記憶池整理已中止。")
            return
        self._save_translation_memory()
        final_count = len(self.translation_memory)
        self.log(f"🧠 全域翻譯記憶池：{final_count:,} 筆（本次新增/更新 {final_count - before:,} 筆）")

    def _seed_cache_from_memory(self, unique_strings):
        if not self.global_memory_var.get():
            return 0
        memory = self._load_translation_memory()
        added = 0
        for text in unique_strings:
            if text in self.translation_cache:
                continue
            translated = memory.get(text)
            if (translated
                    and self._is_valid_trad_translation(text, translated)
                    and Counter(self._critical_format_tokens(text))
                        == Counter(self._critical_format_tokens(translated))):
                self.translation_cache[text] = translated
                added += 1
        if added:
            self.log(f"🧠 全域翻譯記憶池命中 {added:,} 筆，已併入本次快取。")
        return added

    def _file_signature(self, path, old_sig=None):
        try:
            st = os.stat(path)
            mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
            # size+mtime 都沒變就沿用舊雜湊，免去整檔重讀（大型整合包可省數 GB IO）
            if (isinstance(old_sig, dict)
                    and old_sig.get("size") == st.st_size
                    and old_sig.get("mtime_ns") == mtime_ns
                    and old_sig.get("sha1")):
                return dict(old_sig)
            digest = hashlib.sha1()
            with open(path, 'rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    digest.update(block)
            return {
                "size": st.st_size,
                "mtime_ns": mtime_ns,
                "sha1": digest.hexdigest(),
            }
        except OSError:
            return None

    def _load_update_index(self):
        try:
            with open(self.update_index_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_update_index(self, index):
        try:
            with open(self.update_index_file, 'w', encoding='utf-8') as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f"⚠️ 更新偵測索引儲存失敗：{e}")

    def _record_update_detection(self, mod_dir):
        if not self.update_detect_var.get():
            self._analysis_changed_files = 0
            self._analysis_unchanged_files = 0
            return
        self.set_current_item("整理中：模組更新偵測", force=True)
        old_index = self._load_update_index()
        new_index = {}
        changed = 0
        unchanged = 0
        source_paths = set(getattr(self, "_last_scan_jar_paths", []))
        source_paths.update(self.analyzed_book_texts.keys())
        source_paths.update(self.analyzed_loose)
        source_paths.update(path for _typ, path in self.analyzed_extra)
        source_paths.update(path for path, _internal in self.analyzed_zip_json)
        sorted_paths = sorted(source_paths)
        total_paths = len(sorted_paths)
        for idx, path in enumerate(sorted_paths, 1):
            if self.stop_requested:
                break
            if idx == 1 or idx % 100 == 0:
                self.set_current_item(f"更新偵測：{idx}/{total_paths} 個來源檔")
            rel_key = os.path.relpath(path, mod_dir).replace('\\', '/')
            sig = self._file_signature(path, old_index.get(rel_key))
            if not sig:
                continue
            rel = rel_key
            new_index[rel] = sig
            if old_index.get(rel) == sig:
                unchanged += 1
            else:
                changed += 1
        self._analysis_changed_files = changed
        self._analysis_unchanged_files = unchanged
        if self.stop_requested:
            self.log("⏸️ 模組更新偵測已中止。")
            return
        self._save_update_index(new_index)
        self.log(f"🔎 模組更新偵測：新增/變更 {changed:,} 個來源檔，未變更 {unchanged:,} 個。")

    # ═══════════════════════════════════════════════
    #  翻譯覆蓋檢查：直接掃描 mods 成品，回報哪裡還是英文（免開遊戲）
    # ═══════════════════════════════════════════════
    def run_coverage_check(self):
        mod_dir = self.mod_dir_var.get().strip() if hasattr(self, "mod_dir_var") else ""
        if not os.path.isdir(mod_dir):
            messagebox.showwarning("覆蓋檢查", "請先在「專案」選擇有效的來源資料夾", parent=self.root)
            return
        threading.Thread(target=self._coverage_task, args=(mod_dir,), daemon=True).start()

    def _coverage_task(self, mod_dir):
        return run_coverage_task(self, mod_dir)

    def _coverage_task_server(self, mod_dir):
        return run_coverage_task_server(self, mod_dir)

    def _update_analysis_summary(self):
        try:
            total, hits, missing = self._count_analysis_strings()
        except Exception as e:
            self.log(f"⚠️ 右側統計更新失敗：{e}")
            self._set_summary_card("cache", "分析完成", "總計：--", self.C_WARN)
            self._set_summary_card("pending", "待確認", "總計：--", self.C_WARN)
            return
        if total > 0:
            rate = hits / total * 100
            mem_hits = getattr(self, "_analysis_memory_hits", 0)
            update_text = ""
            if self.update_detect_var.get():
                update_text = (f"\n更新：{getattr(self, '_analysis_changed_files', 0):,}/"
                               f"{getattr(self, '_analysis_unchanged_files', 0):,}")
            self._set_summary_card(
                "cache", f"{hits:,}", f"總計：{total:,}\n命中率：{rate:.1f}%\n記憶池：{mem_hits:,}{update_text}", self.C_SUCCESS)
            self._set_summary_card(
                "pending", f"{missing:,}", f"總計：{total:,}\n狀態：可開始翻譯",
                self.C_WARN if missing else self.C_SUCCESS)
        else:
            self._set_summary_card("cache", "0", "總計：0\n未找到可翻譯詞彙", self.C_WARN)
            self._set_summary_card("pending", "0", "總計：0\n狀態：無需翻譯", self.C_SUCCESS)

    def _open_folder_var(self, var):
        path = var.get().strip()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showerror("錯誤", "找不到資料夾！")

    def _set_sidebar_active(self, key):
        nav = getattr(self, "_sidebar_nav", {})
        active_bg = "#0c5b61"
        inactive_bg = "#0d171f"
        accent = "#27d4d1"
        text = "#d8e2e7"
        for nav_key, (row, bar, btn) in nav.items():
            active = (nav_key == key)
            bg = active_bg if active else inactive_bg
            row.config(bg=bg)
            bar.config(bg=accent if active else bg)
            btn.config(bg=bg, fg=text,
                       font=("微軟正黑體", 11, "bold" if active else "normal"))

    def _flash_section_border(self, widget):
        if not widget:
            return
        try:
            original = widget.cget("bg")
            widget.config(bg="#27d4d1")
            self.root.after(450, lambda: widget.winfo_exists() and widget.config(bg=original))
        except Exception:
            pass

    def _focus_ui_section(self, key):
        self._set_sidebar_active(key)
        targets = {
            "project": getattr(self, "project_outer", None),
            "engine": getattr(self, "engine_outer", None),
            "output": getattr(self, "project_outer", None),
            "log": getattr(self, "log_outer", None),
        }
        self._flash_section_border(targets.get(key))
        if key == "engine" and hasattr(self, "ai_provider_cb"):
            self.ai_provider_cb.focus_set()
            self.log("INFO  已切換到翻譯引擎設定。")
        elif key == "output":
            self.log("INFO  已切換到輸出設定；固定使用 JAR 直接翻譯，不需資源包。")
        elif key == "log" and hasattr(self, "log_area"):
            self.log_area.focus_set()
            self.log("INFO  已切換到執行記錄。")
        elif key == "project":
            self.log("INFO  已切換到專案設定。")

    def show_cache_summary(self):
        self._set_sidebar_active("cache")
        self._flash_section_border(getattr(self, "log_outer", None))
        stats = []
        try:
            for label, path in [("AI 快取", self.cache_file_ai), ("機翻快取", self.cache_file_std)]:
                count = 0
                bak_count = 0
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        count = len(data)
                bak_file = path + ".bak"
                if os.path.exists(bak_file):
                    with open(bak_file, "r", encoding="utf-8") as f:
                        bak_data = json.load(f)
                    if isinstance(bak_data, dict):
                        bak_count = len(bak_data)
                stats.append((label, path, count, bak_count, size))
        except (OSError, json.JSONDecodeError) as exc:
            self.log(f"⚠️ 快取狀態讀取失敗：{exc}")
            messagebox.showwarning("快取", f"快取讀取失敗：\n{exc}")
            return

        lines = [f"目前使用：{self._current_cache_file()}", ""]
        for label, path, count, bak_count, size in stats:
            lines.append(f"{label}：{path}")
            lines.append(f"  主快取：{count:,} 筆 / 備份：{bak_count:,} 筆 / {size / 1024:.1f} KB")
        msg = "\n".join(lines)
        self.log("INFO  快取狀態："
                 + "；".join(f"{label} {count:,} 筆" for label, _, count, _, _ in stats))
        messagebox.showinfo("快取狀態", msg)

    def show_settings_summary(self):
        self._set_sidebar_active("project")
        msg = (
            f"設定檔：{self.config_file}\n"
            f"AI 快取：{self.cache_file_ai}\n"
            f"機翻快取：{self.cache_file_std}\n\n"
            "設定會自動儲存；API Key 僅以本機設定檔保存。"
        )
        self.log("INFO  已開啟設定資訊。")
        messagebox.showinfo("設定", msg)

    def show_install_guide(self):
        """輸出檔案安裝說明：依目前輸出模式說明每個產物該放哪、要不要解壓。"""
        rp_name = (self.rp_name_var.get().strip() or "翻譯包") if hasattr(self, "rp_name_var") else "翻譯包"
        mode = (self.output_mode_var.get()
                if hasattr(self, "output_mode_var") else "jar_patch")
        server_note = (
            "伺服器（服務端）翻譯\n"
            "────────────────────────────\n"
            "• 把「來源資料夾」指到伺服器目錄（含 server.properties 那層）再分析，\n"
            "  並在安全確認視窗明確啟用伺服器模式：只翻任務書、advancement 顯示文字、\n"
            "  Apotheosis 命名表（這些由伺服器同步，玩家不用裝補丁就看得到中文）。\n"
            "• 伺服器模式同樣輸出 JAR 套用包（不產生客戶端資源包）：\n"
            "  停服 → 整包解壓覆蓋到伺服器根目錄（mods/ + config/）→ 重啟伺服器。\n"
            "• 小型設定/任務檔會附 _backups/；大型 JAR 備份可在「安全增量」勾選啟用。\n"
            "• 伺服器模式不做 class 硬編碼修補（壞一個 class 會全服崩潰，故關閉）；\n"
            "  advancement 只翻顯示文字、JAR 重建自動去簽名，皆有防崩潰保護。\n"
            "• mod 介面、物品 tooltip、死亡訊息是客戶端的事：請玩家安裝客戶端翻譯包。\n\n"
        )
        common = server_note + (
            "通用觀念\n"
            "────────────────────────────\n"
            "• 固定使用 JAR 直接翻譯，不需要資源包或 Paxi。\n"
            "• 輸出含重建後 mods/*.jar、版本 JAR、config/defaultconfigs。\n"
            "• 設定原檔保存在 _backups/；大型 JAR 備份需另勾選。\n"
            "• 低風險 class tooltip 自動翻譯；高風險啟動 class 保留原文。\n\n"
        )
        if mode == "jar_patch":
            detail = (
                "目前模式：JAR 直接翻譯\n"
                "────────────────────────────\n"
                f"① {rp_name.replace('.zip', '')}_模組語言包.zip\n"
                "   → 關閉遊戲後，整包解壓到實例根目錄並覆蓋。\n"
                "   → 內含重建後 mods/*.jar、版本 JAR 與設定檔，不需啟用資源包。\n"
                "   → 若有 TRANSLATOR_RUNTIME_WARNING.txt，先依內容修正 Java。\n"
            )
        else:
            detail = (
                "目前模式：混合模式\n"
                "────────────────────────────\n"
                f"① {rp_name}.zip\n"
                "   → 放進 resourcepacks/，遊戲內啟用；輸出到整合包內時會自動啟用。\n"
                "   → 若 ZIP 內含 config/ 或 defaultconfigs/，再解壓這些資料夾到根目錄。\n\n"
                "② 若啟用低風險 class/JAR 修補，將 Class 補丁 ZIP 解壓到實例根目錄。\n"
                "   原始 mods/ 與版本 JAR 會保存在補丁內的 _backups/。\n"
            )
        self._show_text_dialog("📦 輸出檔案安裝說明", common + detail)

    def _show_text_dialog(self, title, content):
        """深色主題的唯讀文字說明視窗。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg="#0d171f")
        win.geometry("680x560")
        win.transient(self.root)
        box = scrolledtext.ScrolledText(
            win, bg="#0a1219", fg="#d8e2e7", insertbackground="#27d4d1",
            font=("微軟正黑體", 10), relief="flat", bd=0, wrap=tk.WORD,
            padx=14, pady=12)
        box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        box.insert(tk.END, content)
        box.configure(state="disabled")
        tk.Button(win, text="關閉", command=win.destroy,
                  bg="#17262f", fg="#d8e2e7", activebackground="#1f3942",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=("微軟正黑體", 10), padx=24, pady=6,
                  cursor="hand2").pack(pady=(0, 10))

    def show_help(self):
        msg = (
            "使用流程：\n"
            "1. 在「專案」選擇 BMC4 或 Minecraft 實例資料夾。\n"
            "2. 選擇輸出資料夾、Minecraft 版本、翻譯範圍與處理模式。\n"
            "3. 在「翻譯引擎」設定模型與 API Key，先測試連線。\n"
            "4. 按「分析檔案」，完成後再按「開始翻譯」。\n"
            "5. 固定術語會由快取/全域記憶池與翻譯引擎自動累積，不使用白名單詞典。\n"
            "6. 輸出檔案放哪裡？點左側「📦 安裝說明」看詳細步驟。"
        )
        self.log("INFO  已開啟說明。")
        messagebox.showinfo("說明", msg)

    def show_about(self):
        msg = (
            "Minecraft 模組翻譯器\n"
            "版本：v1.2.9\n"
            "預設模型：DeepSeek V4 Flash Free (OpenRouter)\n"
            "支援：JAR 直接翻譯、自動判定、全域記憶池與多 API 模型"
        )
        self.log("INFO  已開啟關於資訊。")
        messagebox.showinfo("關於", msg)

    def setup_ui_v2(self):
        """Workflow-oriented UI based on the generated redesign mockup."""
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor=self.C_ENTRY_BG,
                        background=self.C_ACCENT,
                        bordercolor=self.C_BORDER,
                        lightcolor=self.C_ACCENT,
                        darkcolor=self.C_ACCENT)
        style.configure("TSpinbox",
                        fieldbackground=self.C_ENTRY_BG,
                        foreground=self.C_ENTRY_FG,
                        background=self.C_BORDER,
                        arrowcolor=self.C_TEXT)
        style.configure("Dark.TCombobox",
                        fieldbackground=self.C_ENTRY_BG,
                        foreground=self.C_ENTRY_FG,
                        background=self.C_BORDER,
                        arrowcolor=self.C_TEXT)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        main = tk.Frame(self.root, bg=self.C_BG)
        main.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # Sidebar
        sidebar = tk.Frame(main, bg=self.C_SURFACE, width=190)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text="Minecraft\n模組翻譯器",
                 bg=self.C_SURFACE, fg=self.C_ACCENT,
                 font=("微軟正黑體", 15, "bold"),
                 justify=tk.LEFT).pack(anchor="w", padx=16, pady=(18, 14))
        for text, active in [
            ("專案", True),
            ("翻譯引擎", True),
            ("輸出", True),
            ("快取", False),
            ("記錄", True),
        ]:
            bg = self.C_BORDER if active else self.C_SURFACE
            fg = self.C_TEXT if active else self.C_MUTED
            tk.Label(sidebar, text="  " + text, bg=bg, fg=fg,
                     font=("微軟正黑體", 10),
                     anchor="w", padx=8, pady=8).pack(fill=tk.X, padx=10, pady=2)
        tk.Label(sidebar, text="設定會自動儲存\nAPI Key 僅本機保存",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), justify=tk.LEFT).pack(
                     side=tk.BOTTOM, anchor="w", padx=16, pady=16)

        # Scrollable center area
        center_canvas = tk.Canvas(main, bg=self.C_BG, highlightthickness=0, width=720)
        center_vbar = tk.Scrollbar(main, orient="vertical",
                                   command=center_canvas.yview,
                                   bg=self.C_BORDER, troughcolor=self.C_ENTRY_BG,
                                   activebackground=self.C_ACCENT, relief="flat", width=10)
        center_canvas.configure(yscrollcommand=center_vbar.set)
        center_canvas.grid(row=0, column=1, sticky="nsew")
        center_vbar.grid(row=0, column=2, sticky="ns", padx=(4, 10))
        center = tk.Frame(center_canvas, bg=self.C_BG, width=720, height=1120)
        center.grid_propagate(False)
        center_id = center_canvas.create_window((0, 0), window=center, anchor="nw")
        center.columnconfigure(0, weight=1)
        center.bind("<Configure>", lambda e: center_canvas.configure(
            scrollregion=center_canvas.bbox("all")))
        center_canvas.bind("<Configure>", lambda e: center_canvas.itemconfig(
            center_id, width=e.width))

        def _on_mousewheel(event):
            w = event.widget
            if isinstance(w, (tk.Text, scrolledtext.ScrolledText)):
                return
            center_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        self.root.bind_all("<Button-4>", lambda e: center_canvas.yview_scroll(-1, "units"))
        self.root.bind_all("<Button-5>", lambda e: center_canvas.yview_scroll(1, "units"))

        def label(parent, text, row, col=0, **kwargs):
            return tk.Label(parent, text=text, bg=self.C_SURFACE, fg=self.C_MUTED,
                            font=("微軟正黑體", 8), anchor="w", **kwargs).grid(
                                row=row, column=col, sticky="w", pady=(5, 2))

        # Project panel
        project_outer, project = self._make_card(center, "專案與輸出", "📁")
        project_outer.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        project.columnconfigure(0, weight=1)
        label(project, "Minecraft 遊戲或模組包資料夾", 0)
        self.mod_dir_var = tk.StringVar()
        self._make_entry(project, self.mod_dir_var, width=28).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self._make_browse_btn(project, self.browse_mod_dir).grid(row=1, column=1, sticky="e")
        label(project, "翻譯結果輸出資料夾", 2)
        self.rp_dir_var = tk.StringVar()
        self._make_entry(project, self.rp_dir_var, width=28).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        self._make_browse_btn(project, self.browse_rp_dir).grid(row=3, column=1, sticky="e")
        label(project, "輸出檔案基底名稱（免加 .zip）", 4)
        self.rp_name_var = tk.StringVar(value="Auto_Translated_Mods_zh_tw")
        self._make_entry(project, self.rp_name_var, width=28).grid(row=5, column=0, columnspan=2, sticky="ew")

        mode_row = tk.Frame(project, bg=self.C_SURFACE)
        mode_row.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        self.output_mode_var = tk.StringVar(value="jar_patch")
        tk.Label(mode_row, text="JAR 直接翻譯", bg=self.C_SURFACE, fg=self.C_ACCENT,
                 font=("微軟正黑體", 9, "bold")).pack(side=tk.LEFT, padx=(0, 16))
        self.output_mode_hint = tk.Label(project, text="",
                                         bg=self.C_SURFACE, fg=self.C_MUTED,
                                         font=("微軟正黑體", 8), anchor="w",
                                         wraplength=620, justify=tk.LEFT)
        self.output_mode_hint.grid(row=7, column=0, columnspan=2, sticky="w")

        # Engine panel
        engine_outer, engine_body = self._make_card(center, "翻譯引擎與模型", "⚙️")
        engine_outer.grid(row=1, column=0, sticky="ew", pady=8)
        engine_body.columnconfigure(0, weight=1)
        self.pack_format_var = tk.IntVar(value=15)
        pf_row = tk.Frame(engine_body, bg=self.C_SURFACE)
        pf_row.grid(row=0, column=0, sticky="ew")
        tk.Label(pf_row, text="pack_format", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Spinbox(pf_row, from_=1, to=99, textvariable=self.pack_format_var,
                    width=6, style="TSpinbox").pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(pf_row, text="1.20.x=15  1.21.x=18  1.21.4+=46",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8)).pack(side=tk.LEFT)

        self.engine_var = tk.StringVar(value="google")
        eng_frame = tk.Frame(engine_body, bg=self.C_SURFACE)
        eng_frame.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        for idx, (val, text, clr) in enumerate(self.ENGINES):
            rb = tk.Radiobutton(eng_frame, text=text, variable=self.engine_var, value=val,
                                command=self._on_engine_change,
                                bg=self.C_SURFACE, fg=self.C_TEXT,
                                activebackground=self.C_SURFACE, activeforeground=clr,
                                selectcolor=self.C_SURFACE,
                                font=("微軟正黑體", 8), cursor="hand2")
            rb.grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 18), pady=2)

        self.key_frame = tk.Frame(engine_body, bg=self.C_SURFACE)
        self.key_frame.grid(row=2, column=0, sticky="ew")
        self.key_frame.columnconfigure(0, weight=1)

        def provider_frame():
            frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
            frame.columnconfigure(1, weight=1)
            return frame

        def row_entry(frame, row, title, var, show="", width=28):
            tk.Label(frame, text=title, bg=self.C_SURFACE, fg=self.C_MUTED,
                     font=("微軟正黑體", 8), width=22, anchor="w").grid(
                         row=row, column=0, sticky="w", pady=(4, 0))
            self._make_entry(frame, var, width=width, show=show).grid(
                row=row, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))

        self.google_key_frame = provider_frame()
        self.api_key_var = tk.StringVar()
        row_entry(self.google_key_frame, 0, "Google API Key", self.api_key_var, show="●")

        self.deepl_key_frame = provider_frame()
        self.deepl_key_var = tk.StringVar()
        row_entry(self.deepl_key_frame, 0, "DeepL API Key", self.deepl_key_var, show="●")
        tk.Label(self.deepl_key_frame, text="免費版 50 萬字/月 ｜ https://www.deepl.com/pro-api",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=1, column=0, columnspan=2, sticky="w")

        self.azure_key_frame = provider_frame()
        self.azure_key_var = tk.StringVar()
        self.azure_region_var = tk.StringVar(value="eastasia")
        self.azure_endpoint_var = tk.StringVar()
        row_entry(self.azure_key_frame, 0, "Azure Subscription Key", self.azure_key_var, show="●")
        row_entry(self.azure_key_frame, 1, "Azure Region", self.azure_region_var, width=20)
        row_entry(self.azure_key_frame, 2, "自訂端點", self.azure_endpoint_var)

        self.claude_key_frame = provider_frame()
        self.claude_key_var = tk.StringVar()
        self.claude_model_var = tk.StringVar(value="claude-haiku-4-5-20251001")
        row_entry(self.claude_key_frame, 0, "Anthropic API Key", self.claude_key_var, show="●")
        tk.Label(self.claude_key_frame, text="Claude 模型", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(self.claude_key_frame, textvariable=self.claude_model_var,
                     values=list(self.AI_PROVIDER_PRESETS["anthropic"]["models"]),
                     font=("Consolas", 9), width=30).grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        self.openai_key_frame = provider_frame()
        self.openai_key_var = tk.StringVar()
        self.openai_model_var = tk.StringVar(value="gpt-5.4-mini")
        row_entry(self.openai_key_frame, 0, "OpenAI API Key", self.openai_key_var, show="●")
        tk.Label(self.openai_key_frame, text="GPT 模型", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(self.openai_key_frame, textvariable=self.openai_model_var,
                     values=list(self.AI_PROVIDER_PRESETS["openai"]["models"]),
                     font=("Consolas", 9), width=30).grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        self.market_ai_key_frame = provider_frame()
        self.ai_provider_var = tk.StringVar(value="OpenAI")
        self.ai_auth_mode_var = tk.StringVar(value="api")
        self.ai_api_key_var = tk.StringVar()
        self.ai_model_var = tk.StringVar(value="gpt-5.4-mini")
        self.ai_base_url_var = tk.StringVar(value=self.AI_PROVIDER_PRESETS["openai"]["base_url"])
        self.ai_login_url_var = tk.StringVar(value=self.AI_PROVIDER_PRESETS["openai"]["login_url"])
        tk.Label(self.market_ai_key_frame, text="供應商", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w", pady=(4, 0))
        provider_cb = ttk.Combobox(self.market_ai_key_frame, textvariable=self.ai_provider_var,
                                   values=self._ai_provider_labels(), state="readonly",
                                   font=("微軟正黑體", 9), width=30)
        provider_cb.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        provider_cb.bind("<<ComboboxSelected>>", self._on_ai_provider_change)
        auth_frame = tk.Frame(self.market_ai_key_frame, bg=self.C_SURFACE)
        auth_frame.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
        for text, value in [("API Key", "api"), ("登入頁取得 Key", "login")]:
            tk.Radiobutton(auth_frame, text=text, variable=self.ai_auth_mode_var, value=value,
                           bg=self.C_SURFACE, fg=self.C_TEXT,
                           activebackground=self.C_SURFACE, activeforeground=self.C_ACCENT,
                           selectcolor=self.C_SURFACE,
                           font=("微軟正黑體", 8), cursor="hand2").pack(side=tk.LEFT, padx=(0, 12))
        row_entry(self.market_ai_key_frame, 2, "API Key", self.ai_api_key_var, show="●")
        tk.Label(self.market_ai_key_frame, text="模型", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.ai_model_cb = ttk.Combobox(self.market_ai_key_frame, textvariable=self.ai_model_var,
                                        values=list(self.AI_PROVIDER_PRESETS["openai"]["models"]),
                                        font=("Consolas", 9), width=34)
        self.ai_model_cb.grid(row=3, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        row_entry(self.market_ai_key_frame, 4, "API Base URL", self.ai_base_url_var)
        login_row = tk.Frame(self.market_ai_key_frame, bg=self.C_SURFACE)
        login_row.grid(row=5, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        login_row.columnconfigure(0, weight=1)
        self._make_entry(login_row, self.ai_login_url_var, width=40).grid(row=0, column=0, sticky="ew")
        self._make_action_btn(login_row, "開啟登入頁", self.open_ai_login_page,
                              self.C_BORDER).grid(row=0, column=1, sticky="e", padx=(6, 0))
        self.ai_provider_hint = tk.Label(self.market_ai_key_frame, text="",
                                         bg=self.C_SURFACE, fg=self.C_MUTED,
                                         font=("微軟正黑體", 7), anchor="w",
                                         justify=tk.LEFT, wraplength=680)
        self.ai_provider_hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.local_key_frame = provider_frame()
        self.local_url_var = tk.StringVar(value="http://localhost:1234/v1/chat/completions")
        row_entry(self.local_key_frame, 0, "本地端 AI API 網址", self.local_url_var)

        # Control panel
        control_outer, control = self._make_card(center, "執行控制", "▶")
        control_outer.grid(row=2, column=0, sticky="ew", pady=8)
        control.columnconfigure(0, weight=1)
        thr_row = tk.Frame(control, bg=self.C_SURFACE)
        thr_row.grid(row=0, column=0, sticky="ew")
        self.workers_var = tk.IntVar(value=8)
        tk.Label(thr_row, text="並發執行緒", bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Spinbox(thr_row, from_=1, to=32, textvariable=self.workers_var,
                    width=6, style="TSpinbox").pack(side=tk.LEFT, padx=(0, 10))
        self.workers_hint = tk.Label(thr_row, text="", bg=self.C_SURFACE, fg=self.C_MUTED,
                                     font=("微軟正黑體", 8))
        self.workers_hint.pack(side=tk.LEFT)
        btn_row = tk.Frame(control, bg=self.C_SURFACE)
        btn_row.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.btn_analyze = self._make_action_btn(btn_row, "分析檔案", self.start_analysis, self.C_ACCENT2)
        self.btn_analyze.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_translate = self._make_action_btn(btn_row, "開始翻譯", self.start_translation,
                                                   self.C_SUCCESS, state=tk.DISABLED)
        self.btn_translate.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_pause = self._make_action_btn(btn_row, "暫停", self.pause_process,
                                               self.C_WARN, state=tk.DISABLED)
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_stop = self._make_action_btn(btn_row, "停止", self.stop_process,
                                              self.C_DANGER, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        # Status panel
        status_outer, status = self._make_card(center, "狀態", "📊")
        status_outer.grid(row=3, column=0, sticky="ew", pady=8)
        for col in range(4):
            status.columnconfigure(col, weight=1)
        for idx, (name, value, color) in enumerate([
            ("快取命中", "待分析", self.C_MUTED),
            ("待翻譯", "待分析", self.C_MUTED),
            ("輸出模式", "依設定", self.C_ACCENT2),
            ("API 狀態", "未測試", self.C_WARN),
        ]):
            tk.Label(status, text=name, bg=self.C_SURFACE, fg=self.C_MUTED,
                     font=("微軟正黑體", 8), anchor="w").grid(row=0, column=idx, sticky="w", pady=(4, 0))
            tk.Label(status, text=value, bg=self.C_SURFACE, fg=color,
                     font=("微軟正黑體", 12, "bold"), anchor="w").grid(row=1, column=idx, sticky="w")
        tk.Label(status, text="DeepSeek V4 Flash Free 可在「市面 AI 模型」供應商中選取。",
                 bg=self.C_SURFACE, fg=self.C_MUTED, font=("微軟正黑體", 8),
                 justify=tk.LEFT).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # Bottom progress and logs
        bottom = tk.Frame(self.root, bg=self.C_BG)
        bottom.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        bottom.columnconfigure(0, weight=1)
        progress_row = tk.Frame(bottom, bg=self.C_BG)
        progress_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        progress_row.columnconfigure(0, weight=1)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_row, variable=self.progress_var,
                                            maximum=100, style="Dark.Horizontal.TProgressbar")
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_label = tk.Label(progress_row, text="就緒", bg=self.C_BG,
                                       fg=self.C_MUTED, font=("微軟正黑體", 9),
                                       width=26, anchor="e")
        self.progress_label.grid(row=0, column=1, padx=(8, 0))

        log_outer, log_body = self._make_card(bottom, "系統日誌", "📋")
        log_outer.grid(row=1, column=0, sticky="ew")
        log_body.columnconfigure(0, weight=1)
        self.log_area = scrolledtext.ScrolledText(
            log_body, state='disabled',
            bg=self.C_LOG_BG, fg=self.C_LOG_FG,
            insertbackground=self.C_ACCENT,
            font=("Consolas", 9),
            relief="flat", bd=0,
            wrap=tk.WORD,
            height=8)
        self.log_area.grid(row=0, column=0, sticky="ew")
        self.log_area.vbar.config(
            bg=self.C_BORDER, troughcolor=self.C_ENTRY_BG,
            activebackground=self.C_ACCENT, relief="flat", width=10)

        self._on_ai_provider_change()
        self._on_engine_change()
        self._on_output_mode_change()

        self.log("歡迎使用 Minecraft 批次多引擎翻譯器！\n"
                 "支援：Google GTX / DeepL / Azure / Claude / OpenAI / 市面 AI 模型 / 本地 AI\n"
                 "所有引擎均支援限流自動切換  ｜  設定變更自動儲存\n"
                 "新增支援：Patchouli 手冊 / FTB 任務書(.snbt) / Markdown / 任務 JSON")
        self.load_config()

        for _sv in [self.mod_dir_var, self.rp_dir_var, self.rp_name_var,
                    self.api_key_var, self.deepl_key_var,
                    self.azure_key_var, self.azure_region_var, self.azure_endpoint_var,
                    self.claude_key_var, self.claude_model_var,
                    self.openai_key_var, self.openai_model_var,
                    self.ai_provider_var, self.ai_auth_mode_var,
                    self.ai_api_key_var, self.ai_model_var,
                    self.ai_base_url_var, self.ai_login_url_var,
                    self.engine_var, self.local_url_var,
                    self.pack_format_var, self.workers_var,
                    self.output_mode_var]:
            _sv.trace_add('write', self._schedule_save)

    @classmethod
    def _ai_provider_menu_for_key(cls, key):
        free_keys = {
            "deepseek_v4_flash_free",
            "openrouter_free_router",
            "openrouter_free_models",
            "gemini",
            "groq",
            "custom",
        }
        return "free" if key in free_keys else "paid"

    def _ai_provider_labels(self, menu=None):
        menu = menu or (self.ai_provider_menu_var.get()
                        if hasattr(self, "ai_provider_menu_var") else "free")
        return [
            cfg["label"] for key, cfg in self.AI_PROVIDER_PRESETS.items()
            if self._ai_provider_menu_for_key(key) == menu
        ]

    def _ai_provider_key(self):
        var = getattr(self, "ai_provider_var", None)
        raw = var.get().strip() if var else "OpenAI"
        for key, cfg in self.AI_PROVIDER_PRESETS.items():
            if raw == key or raw == cfg["label"]:
                return key
        return "custom"

    def _ai_provider_config(self):
        return self.AI_PROVIDER_PRESETS.get(
            self._ai_provider_key(), self.AI_PROVIDER_PRESETS["custom"])

    def _sync_ai_provider_menu(self, provider_key=None):
        if not hasattr(self, "ai_provider_menu_var"):
            return
        provider_key = provider_key or self._ai_provider_key()
        self.ai_provider_menu_var.set(self._ai_provider_menu_for_key(provider_key))
        if hasattr(self, "ai_provider_cb"):
            self.ai_provider_cb.configure(values=self._ai_provider_labels())

    def _on_ai_provider_menu_change(self, *args):
        if not hasattr(self, "ai_provider_cb"):
            return
        labels = self._ai_provider_labels()
        self.ai_provider_cb.configure(values=labels)
        if self.ai_provider_var.get() not in labels and labels:
            self.ai_provider_var.set(labels[0])
        self._on_ai_provider_change()

    def _on_ai_provider_change(self, *args):
        if not hasattr(self, "ai_model_cb"):
            return
        self._sync_ai_provider_menu(self._ai_provider_key())
        cfg = self._ai_provider_config()
        models = list(dict.fromkeys(cfg.get("models", ())))
        self.ai_model_cb.configure(values=models)
        if models and self.ai_model_var.get() not in models:
            self.ai_model_var.set(models[0])

        default_url = cfg.get("base_url", "")
        current_url = self.ai_base_url_var.get().strip()
        last_default = getattr(self, "_last_ai_default_url", None)
        # Only overwrite Base URL when empty or still equal to the previous preset
        # default — preserve user/custom/config URLs across provider switches.
        if last_default is None or (not current_url) or current_url == last_default:
            self.ai_base_url_var.set(default_url)
        self._last_ai_default_url = default_url

        self.ai_login_url_var.set(cfg.get("login_url", ""))
        self.ai_provider_hint.config(text=cfg.get("hint", ""))
        self._refresh_api_summary()

    def open_ai_login_page(self):
        cfg = self._ai_provider_config()
        url = self.ai_login_url_var.get().strip() or cfg.get("login_url", "")
        if not url:
            messagebox.showinfo(
                "登入方式",
                "此供應商沒有固定登入頁。請使用本地服務或自行填入 API endpoint。")
            return
        webbrowser.open(url)

    def _on_ai_auth_mode_change(self, *args):
        mode = self.ai_auth_mode_var.get().strip() or "api"
        if mode == "login":
            self.log("INFO  登入取得模式：可開啟供應商頁面管理 API Key；批次翻譯仍需可用 API Key。")
        self._refresh_api_summary()

    def toggle_ai_api_key_visibility(self):
        entry_widget = getattr(self, "ai_api_key_entry", None)
        if not entry_widget:
            return
        self._ai_api_key_visible = not getattr(self, "_ai_api_key_visible", False)
        entry_widget.config(show="" if self._ai_api_key_visible else "●")
        if hasattr(self, "btn_show_ai_key"):
            self.btn_show_ai_key.config(text="隱藏" if self._ai_api_key_visible else "顯示")

    def _ai_api_key_list(self):
        keys = []
        primary = self.ai_api_key_var.get().strip() if hasattr(self, "ai_api_key_var") else ""
        if primary:
            keys.append(primary)
        pool_text = self.ai_api_keys_var.get() if hasattr(self, "ai_api_keys_var") else ""
        for raw in re.split(r"[\r\n,;]+", pool_text or ""):
            key = raw.strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    def edit_ai_key_pool(self):
        win = tk.Toplevel(self.root)
        win.title("API Key 輪詢池")
        win.configure(bg=self.C_SURFACE)
        win.geometry("560x360")
        win.transient(self.root)
        win.grab_set()
        tk.Label(
            win,
            text="每行一組 API Key。翻譯時會保留主 Key 優先，之後在 Key 池中輪詢；單一 Key 無效會自動跳下一組。",
            bg=self.C_SURFACE, fg=self.C_MUTED, wraplength=520, justify=tk.LEFT,
            font=("微軟正黑體", 9)
        ).pack(fill=tk.X, padx=14, pady=(14, 8))
        text_box = scrolledtext.ScrolledText(
            win, height=12, bg="#071016", fg=self.C_TEXT,
            insertbackground=self.C_ACCENT, font=("Consolas", 10),
            relief="flat", bd=0)
        text_box.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))
        text_box.insert("1.0", self.ai_api_keys_var.get())

        btn_row = tk.Frame(win, bg=self.C_SURFACE)
        btn_row.pack(fill=tk.X, padx=14, pady=(0, 14))

        def save_keys():
            value = text_box.get("1.0", tk.END).strip()
            self.ai_api_keys_var.set(value)
            self._schedule_save()
            self.log(f"INFO  API Key 輪詢池已更新：共 {len(self._ai_api_key_list())} 組 Key。")
            win.destroy()

        tk.Button(btn_row, text="儲存", command=save_keys,
                  bg=self.C_ACCENT, fg="#ffffff", relief="flat",
                  padx=18, pady=7, font=("微軟正黑體", 9, "bold")).pack(side=tk.RIGHT)
        tk.Button(btn_row, text="取消", command=win.destroy,
                  bg=self.C_BORDER, fg=self.C_TEXT, relief="flat",
                  padx=18, pady=7, font=("微軟正黑體", 9)).pack(side=tk.RIGHT, padx=(0, 8))

    def normalize_ai_endpoint(self):
        cfg = self._ai_provider_config()
        api_type = cfg.get("api_type", "openai_compatible")
        old = self.ai_base_url_var.get().strip()
        if api_type not in ("openai_compatible",):
            self.log(f"INFO  {cfg.get('label', '目前供應商')} 不使用 OpenAI-compatible 端點整理。")
            return old
        new = normalize_base_url(old, api_type)
        if new and new != old:
            self.ai_base_url_var.set(new)
            self.log(f"INFO  Base URL 已整理：{new}")
        elif new:
            self.log(f"INFO  Base URL 已是標準格式：{new}")
        else:
            self.log("⚠️ Base URL 為空，無法整理。")
        return new

    def test_ai_connection(self):
        if getattr(self, "_ai_test_running", False):
            self.log("INFO  連線測試仍在執行中。")
            return

        cfg = self._ai_provider_config()
        provider_key = self._ai_provider_key()
        label = cfg.get("label", "AI")
        model = self.ai_model_var.get().strip()
        base_url = self.ai_base_url_var.get().strip() or cfg.get("base_url", "")
        if self.auto_normalize_endpoint_var.get() and cfg.get("api_type", "openai_compatible") == "openai_compatible":
            base_url = normalize_base_url(base_url, "openai_compatible")
            if base_url != self.ai_base_url_var.get().strip():
                self.ai_base_url_var.set(base_url)
        api_keys = self._ai_api_key_list()
        api_key = api_keys[0] if api_keys else ""
        local_url = self.local_url_var.get().strip()
        api_type = cfg.get("api_type", "openai_compatible")

        if not model:
            self.log("❌ 連線測試失敗：請先選擇或輸入模型 ID。")
            return
        if api_type != "bing" and not base_url:
            self.log("❌ 連線測試失敗：請先填入 API Base URL。")
            return
        if cfg.get("requires_key", True) and not api_keys:
            self.log(f"❌ 連線測試失敗：{label} 需要 API Key。")
            return
        if self.ai_auth_mode_var.get() == "login":
            self.log("INFO  登入頁模式只協助取得 Key；本次測試仍會使用目前填入的 API Key。")

        self._ai_test_running = True
        self._refresh_api_summary(state="測試中", color=self.C_WARN)
        self._refresh_ai_status_widgets(
            connection="● 測試中", latency="延遲：測試中", model_state="模型狀態：測試中")
        key_note = f" / Key {len(api_keys)} 組" if api_keys else ""
        self.log(f"INFO  測試連線：{label} / {model}{key_note}")
        threading.Thread(
            target=self._test_ai_connection_task,
            args=(cfg, provider_key, label, model, base_url, api_key, local_url, api_keys),
            daemon=True).start()

    def _test_ai_connection_task(self, cfg, provider_key, label, model, base_url, api_key, local_url, api_keys=None):
        try:
            started_at = time.time()
            session = requests.Session()
            session.headers.update({'User-Agent': 'Mozilla/5.0'})
            settings = {
                "google_key": "",
                "deepl_key": "",
                "azure_key": "",
                "azure_region": "eastasia",
                "azure_url": "https://api.cognitive.microsofttranslator.com/translate",
                "claude_key": "",
                "claude_model": "",
                "openai_key": "",
                "openai_model": "",
                "local_url": local_url,
                "ai_provider_key": provider_key,
                "ai_provider_cfg": cfg,
                "ai_api_key": api_key,
                "ai_api_keys": api_keys or [],
                "ai_model": model,
                "ai_base_url": base_url,
                "ai_label": label,
                "should_stop": lambda: False,
            }
            provider = build_provider_registry(session, settings).get("market_ai")
            translations, err = provider(["Stone"])
            latency_text = f"延遲：{int((time.time() - started_at) * 1000)} ms"
            if err:
                self.log(f"❌ 連線測試失敗：{err.lstrip('ERR:')}")
                self.root.after(0, lambda: (
                    self._refresh_ai_status_widgets(
                        connection="● 失敗", latency=latency_text, model_state="模型狀態：錯誤"),
                    self._set_summary_card(
                        "api", "失敗", f"提供者：{label}\n模型：{model}", self.C_DANGER)
                ))
            elif translations:
                self.log(f"✅ 連線測試成功：收到回應 ({str(translations[0])[:40]})")
                self.root.after(0, lambda: (
                    self._refresh_ai_status_widgets(
                        connection="● 已連線", latency=latency_text, model_state="模型狀態：可用"),
                    self._set_summary_card(
                        "api", "正常", f"提供者：{label}\n模型：{model}", self.C_SUCCESS)
                ))
            else:
                self.log("❌ 連線測試失敗：供應商沒有回傳內容。")
                self.root.after(0, lambda: (
                    self._refresh_ai_status_widgets(
                        connection="● 失敗", latency=latency_text, model_state="模型狀態：無回應"),
                    self._set_summary_card(
                        "api", "失敗", f"提供者：{label}\n模型：{model}", self.C_DANGER)
                ))
        finally:
            self._ai_test_running = False

    def setup_ui(self):
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor=self.C_ENTRY_BG,
                        background=self.C_ACCENT,
                        bordercolor=self.C_BORDER,
                        lightcolor=self.C_ACCENT,
                        darkcolor=self.C_ACCENT)
        style.configure("TSpinbox",
                        fieldbackground=self.C_ENTRY_BG,
                        foreground=self.C_ENTRY_FG,
                        background=self.C_BORDER,
                        arrowcolor=self.C_TEXT)

        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=0)   # 捲軸欄
        self.root.rowconfigure(0, weight=1)       # 上方可捲動區
        self.root.rowconfigure(1, weight=1)       # 下方日誌區

        # ── 上方可捲動區：Canvas + 垂直捲軸 ──
        top_canvas = tk.Canvas(self.root, bg=self.C_BG, highlightthickness=0)
        top_vbar   = tk.Scrollbar(self.root, orient="vertical",
                                  command=top_canvas.yview,
                                  bg=self.C_BORDER, troughcolor=self.C_ENTRY_BG,
                                  activebackground=self.C_ACCENT, relief="flat", width=10)
        top_canvas.configure(yscrollcommand=top_vbar.set)
        top_vbar.grid(row=0, column=1, sticky="ns")
        top_canvas.grid(row=0, column=0, sticky="nsew")

        # 內部可捲動框架
        sf    = tk.Frame(top_canvas, bg=self.C_BG)
        sf_id = top_canvas.create_window((0, 0), window=sf, anchor="nw")

        def _on_sf_configure(e):
            top_canvas.configure(scrollregion=top_canvas.bbox("all"))
        sf.bind("<Configure>", _on_sf_configure)

        def _on_canvas_configure(e):
            top_canvas.itemconfig(sf_id, width=e.width)
        top_canvas.bind("<Configure>", _on_canvas_configure)

        # 滑鼠滾輪：Canvas 可捲動（在日誌文字框上不攔截，讓日誌自己捲動）
        def _on_mousewheel(event):
            w = event.widget
            if isinstance(w, (tk.Text, scrolledtext.ScrolledText)):
                return   # 日誌 ScrolledText 自行處理
            top_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        # Linux/Mac 相容（Button-4/5）
        self.root.bind_all("<Button-4>", lambda e: top_canvas.yview_scroll(-1, "units"))
        self.root.bind_all("<Button-5>", lambda e: top_canvas.yview_scroll(1, "units"))

        # ── 標題列 ──
        header = tk.Frame(sf, bg=self.C_BG)
        header.pack(fill=tk.X, padx=16, pady=(16, 8))
        tk.Label(header,
                 text="⛏  Minecraft 全資料夾智能翻譯器",
                 bg=self.C_BG, fg=self.C_ACCENT,
                 font=("微軟正黑體", 16, "bold")).pack(side=tk.LEFT)
        tk.Label(header,
                 text="多引擎極速版",
                 bg=self.C_BG, fg=self.C_MUTED,
                 font=("微軟正黑體", 9)).pack(side=tk.LEFT, padx=(10, 0), pady=(6, 0))

        # ── 卡片 1：路徑設定 ──
        card1_outer, card1 = self._make_collapsible_card(sf, "路徑與輸出設定", "📁", default_open=True)
        card1_outer.pack(fill=tk.X, padx=16, pady=4)

        self._make_label(card1, "① 選擇 Minecraft 遊戲或模組包資料夾").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
        self.mod_dir_var = tk.StringVar()
        self._make_entry(card1, self.mod_dir_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self._make_browse_btn(card1, self.browse_mod_dir).grid(row=1, column=1, sticky="e")
        card1.columnconfigure(0, weight=1)

        self._make_label(card1, "② 選擇翻譯結果 (ZIP) 輸出資料夾").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 3))
        self.rp_dir_var = tk.StringVar()
        self._make_entry(card1, self.rp_dir_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        self._make_browse_btn(card1, self.browse_rp_dir).grid(row=3, column=1, sticky="e")

        self._make_label(card1, "③ 輸出檔案基底名稱  (免加 .zip)").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 3))
        self.rp_name_var = tk.StringVar(value="Auto_Translated_Mods_zh_tw")
        self._make_entry(card1, self.rp_name_var).grid(row=5, column=0, sticky="ew", padx=(0, 8))

        # ── 輸出格式選擇 ──
        self._make_label(card1, "輸出格式選擇").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(10, 3))
        mode_frame = tk.Frame(card1, bg=self.C_SURFACE)
        mode_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.output_mode_var = tk.StringVar(value="jar_patch")
        tk.Label(
            mode_frame,
            text="📦  JAR 直接翻譯  （免資源包 + 自動低風險 class）",
            bg=self.C_SURFACE, fg=self.C_ACCENT,
            font=("微軟正黑體", 9, "bold")
        ).grid(row=0, column=0, sticky="w", pady=2)
        self.output_mode_hint = tk.Label(
            mode_frame,
            text="  直接重建 mods/JAR 與設定；安全 tooltip class 自動納入",
            bg=self.C_SURFACE, fg=self.C_MUTED, font=("微軟正黑體", 8), anchor="w")
        self.output_mode_hint.grid(row=1, column=0, sticky="w")

        # ── 卡片 2：翻譯引擎設定 ──
        card2_outer, card2 = self._make_collapsible_card(sf, "翻譯引擎設定", "⚙️", default_open=False)
        card2_outer.pack(fill=tk.X, padx=16, pady=4)
        card2.columnconfigure(1, weight=1)

        # pack_format
        self._make_label(card2, "④ pack_format 版本").grid(
            row=0, column=0, sticky="w", pady=(0, 3))
        pf_row = tk.Frame(card2, bg=self.C_SURFACE)
        pf_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.pack_format_var = tk.IntVar(value=15)
        ttk.Spinbox(pf_row, from_=1, to=99,
                    textvariable=self.pack_format_var, width=6,
                    style="TSpinbox").pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(pf_row, text="1.19=12   1.20.x=15   1.21.x=18   1.21.4+=46",
                 bg=self.C_SURFACE, fg=self.C_MUTED, font=("微軟正黑體", 8)).pack(side=tk.LEFT)

        # ⑤ 引擎選擇 Radio（兩行排列）
        self._make_label(card2, "⑤ 翻譯引擎選擇").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 3))
        eng_frame = tk.Frame(card2, bg=self.C_SURFACE)
        eng_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.engine_var = tk.StringVar(value="google")
        for idx, (val, label, clr) in enumerate(self.ENGINES):
            col = idx % 3
            row_n = idx // 3
            rb = tk.Radiobutton(
                eng_frame, text=label, variable=self.engine_var, value=val,
                command=self._on_engine_change,
                bg=self.C_SURFACE, fg=self.C_TEXT,
                activebackground=self.C_SURFACE, activeforeground=clr,
                selectcolor=self.C_SURFACE,
                font=("微軟正黑體", 9), cursor="hand2")
            rb.grid(row=row_n, column=col, sticky="w", padx=(0, 20), pady=2)

        # ── API Key 區塊（動態顯示） ──
        self.key_frame = tk.Frame(card2, bg=self.C_SURFACE)
        self.key_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.key_frame.columnconfigure(1, weight=1)

        # Google API Key
        self.google_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.google_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.google_key_frame, text="Google API Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=20, anchor="w").grid(row=0, column=0, sticky="w")
        self.api_key_var = tk.StringVar()
        self._make_entry(self.google_key_frame, self.api_key_var, show="●").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

        # DeepL API Key
        self.deepl_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.deepl_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.deepl_key_frame,
                 text="DeepL API Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=20, anchor="w").grid(row=0, column=0, sticky="w")
        self.deepl_key_var = tk.StringVar()
        self._make_entry(self.deepl_key_frame, self.deepl_key_var, show="●").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(self.deepl_key_frame,
                 text="  免費版 50 萬字/月 ｜ https://www.deepl.com/pro-api",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=1, column=0, columnspan=2, sticky="w")

        # Azure 設定（Key + Region）
        self.azure_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.azure_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.azure_key_frame, text="Azure Subscription Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w")
        self.azure_key_var = tk.StringVar()
        self._make_entry(self.azure_key_frame, self.azure_key_var, show="●").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(self.azure_key_frame, text="Azure Region",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.azure_region_var = tk.StringVar(value="eastasia")
        self._make_entry(self.azure_key_frame, self.azure_region_var, width=20).grid(
            row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
        tk.Label(self.azure_key_frame, text="自訂端點（AI Services 用）",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.azure_endpoint_var = tk.StringVar()
        self._make_entry(self.azure_key_frame, self.azure_endpoint_var).grid(
            row=2, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        tk.Label(self.azure_key_frame,
                 text="  AI Services 資源請填入 https://xxx.cognitiveservices.azure.com  一般 Translator 資源留空即可",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=3, column=0, columnspan=2, sticky="w")

        # Claude API 設定
        self.claude_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.claude_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.claude_key_frame, text="Anthropic API Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w")
        self.claude_key_var = tk.StringVar()
        self._make_entry(self.claude_key_frame, self.claude_key_var, show="●").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(self.claude_key_frame, text="Claude 模型",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.claude_model_var = tk.StringVar(value="claude-haiku-4-5-20251001")
        model_cb_c = ttk.Combobox(self.claude_key_frame,
                                  textvariable=self.claude_model_var,
                                  values=list(self.AI_PROVIDER_PRESETS["anthropic"]["models"]),
                                  font=("Consolas", 9), width=35)
        model_cb_c.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
        tk.Label(self.claude_key_frame,
                 text="  批次 20 條/次，品質最佳，理解 Minecraft 語境",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=2, column=0, columnspan=2, sticky="w")

        # OpenAI API 設定
        self.openai_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.openai_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.openai_key_frame, text="OpenAI API Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w")
        self.openai_key_var = tk.StringVar()
        self._make_entry(self.openai_key_frame, self.openai_key_var, show="●").grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(self.openai_key_frame, text="GPT 模型",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.openai_model_var = tk.StringVar(value="gpt-5.4-mini")
        model_cb_o = ttk.Combobox(self.openai_key_frame,
                                  textvariable=self.openai_model_var,
                                  values=list(self.AI_PROVIDER_PRESETS["openai"]["models"]),
                                  font=("Consolas", 9), width=35)
        model_cb_o.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
        tk.Label(self.openai_key_frame,
                 text="  批次 20 條/次，gpt-5.4-mini / gpt-5.4-nano 適合大量翻譯",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=2, column=0, columnspan=2, sticky="w")

        # 市面 AI 模型（通用 API / 登入入口）
        self.market_ai_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.market_ai_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.market_ai_key_frame, text="供應商",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w")
        self.ai_provider_var = tk.StringVar(value="OpenAI")
        provider_cb = ttk.Combobox(self.market_ai_key_frame,
                                   textvariable=self.ai_provider_var,
                                   values=self._ai_provider_labels(),
                                   state="readonly",
                                   font=("微軟正黑體", 9), width=34)
        provider_cb.grid(row=0, column=1, sticky="w", padx=(4, 0))
        provider_cb.bind("<<ComboboxSelected>>", self._on_ai_provider_change)

        tk.Label(self.market_ai_key_frame, text="使用方式",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        auth_frame = tk.Frame(self.market_ai_key_frame, bg=self.C_SURFACE)
        auth_frame.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
        self.ai_auth_mode_var = tk.StringVar(value="api")
        tk.Radiobutton(auth_frame, text="API Key", variable=self.ai_auth_mode_var, value="api",
                       bg=self.C_SURFACE, fg=self.C_TEXT,
                       activebackground=self.C_SURFACE, activeforeground=self.C_ACCENT,
                       selectcolor=self.C_SURFACE,
                       font=("微軟正黑體", 8), cursor="hand2").pack(side=tk.LEFT, padx=(0, 10))
        tk.Radiobutton(auth_frame, text="登入頁取得 Key", variable=self.ai_auth_mode_var, value="login",
                       bg=self.C_SURFACE, fg=self.C_TEXT,
                       activebackground=self.C_SURFACE, activeforeground=self.C_ACCENT,
                       selectcolor=self.C_SURFACE,
                       font=("微軟正黑體", 8), cursor="hand2").pack(side=tk.LEFT)

        tk.Label(self.market_ai_key_frame, text="API Key",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.ai_api_key_var = tk.StringVar()
        self._make_entry(self.market_ai_key_frame, self.ai_api_key_var, show="●").grid(
            row=2, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))

        tk.Label(self.market_ai_key_frame, text="模型",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.ai_model_var = tk.StringVar(value="gpt-5.4-mini")
        self.ai_model_cb = ttk.Combobox(self.market_ai_key_frame,
                                        textvariable=self.ai_model_var,
                                        values=list(self.AI_PROVIDER_PRESETS["openai"]["models"]),
                                        font=("Consolas", 9), width=42)
        self.ai_model_cb.grid(row=3, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))

        tk.Label(self.market_ai_key_frame, text="API Base URL",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.ai_base_url_var = tk.StringVar(value=self.AI_PROVIDER_PRESETS["openai"]["base_url"])
        self._make_entry(self.market_ai_key_frame, self.ai_base_url_var).grid(
            row=4, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))

        login_row = tk.Frame(self.market_ai_key_frame, bg=self.C_SURFACE)
        login_row.grid(row=5, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))
        login_row.columnconfigure(0, weight=1)
        self.ai_login_url_var = tk.StringVar(value=self.AI_PROVIDER_PRESETS["openai"]["login_url"])
        self._make_entry(login_row, self.ai_login_url_var, width=40).grid(row=0, column=0, sticky="ew")
        self._make_action_btn(login_row, "開啟登入頁", self.open_ai_login_page,
                              self.C_BORDER).grid(row=0, column=1, sticky="e", padx=(6, 0))

        self.ai_provider_hint = tk.Label(
            self.market_ai_key_frame,
            text=self.AI_PROVIDER_PRESETS["openai"]["hint"],
            bg=self.C_SURFACE, fg=self.C_MUTED,
            font=("微軟正黑體", 7), anchor="w", justify=tk.LEFT, wraplength=720)
        self.ai_provider_hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # 本地 AI 設定
        self.local_key_frame = tk.Frame(self.key_frame, bg=self.C_SURFACE)
        self.local_key_frame.columnconfigure(1, weight=1)
        tk.Label(self.local_key_frame, text="本地端 AI API 網址",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 8), width=22, anchor="w").grid(row=0, column=0, sticky="w")
        self.local_url_var = tk.StringVar(value="http://localhost:1234/v1/chat/completions")
        self._make_entry(self.local_key_frame, self.local_url_var).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(self.local_key_frame,
                 text="  相容 OpenAI chat/completions 格式（LM Studio、Ollama…）",
                 bg=self.C_SURFACE, fg=self.C_MUTED,
                 font=("微軟正黑體", 7)).grid(row=1, column=0, columnspan=2, sticky="w")

        # 並發執行緒數
        self._make_label(card2, "⑥ 並發執行緒數").grid(
            row=5, column=0, sticky="w", pady=(8, 3))
        thr_row = tk.Frame(card2, bg=self.C_SURFACE)
        thr_row.grid(row=6, column=0, columnspan=3, sticky="w")
        self.workers_var = tk.IntVar(value=8)
        ttk.Spinbox(thr_row, from_=1, to=32,
                    textvariable=self.workers_var, width=6,
                    style="TSpinbox").pack(side=tk.LEFT, padx=(0, 10))
        self.workers_hint = tk.Label(
            thr_row,
            text="GTX 建議 3~4 ｜ Google/DeepL/Azure 建議 12~16 ｜ AI 建議 4~6",
            bg=self.C_SURFACE, fg=self.C_MUTED, font=("微軟正黑體", 8))
        self.workers_hint.pack(side=tk.LEFT)

        # 初始顯示正確的 key frame
        self._on_engine_change()

        # ── 操作按鈕列 ──
        btn_outer = tk.Frame(sf, bg=self.C_BG)
        btn_outer.pack(fill=tk.X, padx=16, pady=8)

        self.btn_analyze = self._make_action_btn(
            btn_outer, "🔍  分析檔案", self.start_analysis, self.C_ACCENT2)
        self.btn_analyze.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_translate = self._make_action_btn(
            btn_outer, "🌐  開始極速翻譯", self.start_translation,
            self.C_SUCCESS, state=tk.DISABLED)
        self.btn_translate.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_pause = self._make_action_btn(
            btn_outer, "⏸  暫停進度", self.pause_process,
            self.C_WARN, state=tk.DISABLED)
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = self._make_action_btn(
            btn_outer, "🛑  停止進程", self.stop_process,
            self.C_DANGER, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        # ── 進度條 ──
        prog_outer = tk.Frame(sf, bg=self.C_BG)
        prog_outer.pack(fill=tk.X, padx=16, pady=(0, 4))
        prog_outer.columnconfigure(0, weight=1)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            prog_outer, variable=self.progress_var, maximum=100,
            style="Dark.Horizontal.TProgressbar", length=600)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_label = tk.Label(
            prog_outer, text="就緒",
            bg=self.C_BG, fg=self.C_MUTED,
            font=("微軟正黑體", 9), width=26, anchor="e")
        self.progress_label.grid(row=0, column=1, padx=(8, 0))

        # ── 日誌區 ──
        log_outer, log_body = self._make_card(self.root, "系統日誌", "📋")
        log_outer.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=16, pady=(4, 16))
        log_outer.columnconfigure(0, weight=1)
        log_outer.rowconfigure(0, weight=1)
        log_body.columnconfigure(0, weight=1)
        log_body.rowconfigure(0, weight=1)

        self.log_area = scrolledtext.ScrolledText(
            log_body, state='disabled',
            bg=self.C_LOG_BG, fg=self.C_LOG_FG,
            insertbackground=self.C_ACCENT,
            font=("Consolas", 9),
            relief="flat", bd=0,
            wrap=tk.WORD,
            height=14)
        self.log_area.grid(row=0, column=0, sticky="nsew")
        self.log_area.vbar.config(
            bg=self.C_BORDER, troughcolor=self.C_ENTRY_BG,
            activebackground=self.C_ACCENT, relief="flat", width=10)

        self.log("歡迎使用 Minecraft 批次多引擎翻譯器！\n"
                 "支援：Google GTX / DeepL / Azure / Claude / OpenAI / 市面 AI 模型 / 本地 AI\n"
                 "所有引擎均支援限流自動切換  ｜  設定變更自動儲存\n"
                 "新增支援：Patchouli 手冊 / FTB 任務書(.snbt) / Markdown / 任務 JSON")
        self.load_config()

        # ── 自動儲存：任何設定欄位變更後 1.5 秒自動存檔 ──
        for _sv in [self.mod_dir_var, self.rp_dir_var, self.rp_name_var,
                    self.api_key_var, self.deepl_key_var,
                    self.azure_key_var, self.azure_region_var, self.azure_endpoint_var,
                    self.claude_key_var, self.claude_model_var,
                    self.openai_key_var, self.openai_model_var,
                    self.ai_provider_var, self.ai_auth_mode_var,
                    self.ai_api_key_var, self.ai_model_var,
                    self.ai_base_url_var, self.ai_login_url_var,
                    self.engine_var, self.local_url_var,
                    self.pack_format_var, self.workers_var,
                    self.output_mode_var]:
            _sv.trace_add('write', self._schedule_save)

    # ═══════════════════════════════════════════════
    #  引擎切換 → 動態顯示對應 Key 欄位
    # ═══════════════════════════════════════════════
    @staticmethod
    def _normalize_engine_route_value(engine):
        if engine in ("standard_chain", "no_key_chain"):
            return "non_ai_chain"
        return engine

    def _on_engine_change(self):
        engine = self._normalize_engine_route_value(self.engine_var.get())
        if engine != self.engine_var.get():
            self.engine_var.set(engine)

        # 所有 key frame 先隱藏
        for frame in [self.google_key_frame, self.deepl_key_frame,
                      self.azure_key_frame, self.claude_key_frame,
                      self.openai_key_frame, self.market_ai_key_frame,
                      self.local_key_frame]:
            frame.grid_remove()

        # 顯示對應 frame
        mapping = {
            "google": self.google_key_frame,
            "deepl":  self.deepl_key_frame,
            "azure":  self.azure_key_frame,
            "claude": self.claude_key_frame,
            "openai": self.openai_key_frame,
            "market_ai": self.market_ai_key_frame,
            "non_ai_chain": self.market_ai_key_frame,
            "local":  self.local_key_frame,
        }
        frame = mapping.get(engine)
        if frame:
            frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        # 更新執行緒建議文字
        hints = {
            "google": "GTX 建議 8~16（大批次高速；遇 429 自動退避）｜有 API Key 時可更高",
            "deepl":  "DeepL 建議 10~16（免費版有並發限制，勿超過 16）",
            "azure":  "Azure 免費版建議 3~5（超過容易 429 限流）｜付費版可提高至 10~15",
            "claude": "Claude API 建議 8~16（Token 計費；遇 429 再降）",
            "openai": "OpenAI API 建議 8~16（Token 計費；遇 429 再降）",
            "market_ai": "市面 AI 付費建議 8~16（最大 16）；免費／易 429 供應商請自行調低",
            "non_ai_chain": "非 AI 翻譯鏈：僅用 GTX（其他免費通道已移除）",
            "local":  "本地 AI 建議 2~4（受限於本機 GPU/CPU）",
        }
        self.workers_hint.config(text=hints.get(engine, ""))
        self._refresh_engine_route_ui()
        self._refresh_api_summary()

    def _on_engine_route_change(self):
        self._on_engine_change()
        if self.engine_var.get() == "non_ai_chain":
            self.log("INFO  已切換為非 AI 翻譯鏈：僅使用 GTX（Bing/Azure 已停用）；以大批次衝速，限流時自動退避。")

    def _refresh_engine_route_ui(self):
        if not hasattr(self, "ai_provider_menu_row"):
            return
        engine = self._normalize_engine_route_value(self.engine_var.get())
        is_market_ai = engine == "market_ai"
        market_widgets = [
            self.ai_provider_menu_row,
            self.ai_provider_cb,
            self.ai_auth_row,
            self.ai_model_label,
            self.ai_model_row,
            self.ai_key_label,
            self.ai_api_key_entry,
            self.btn_show_ai_key,
            self.btn_open_ai_login,
            self.ai_base_label,
            self.ai_base_entry,
            self.ai_base_help,
        ]
        for widget in market_widgets:
            if is_market_ai:
                widget.grid()
            else:
                widget.grid_remove()
        if hasattr(self, "ai_provider_hint"):
            if is_market_ai:
                cfg = self._ai_provider_config()
                self.ai_provider_hint.config(text=cfg.get("hint", ""))
            else:
                self.ai_provider_hint.config(
                    text="非 AI 模式僅使用 GTX（Bing 免費通道已失效、Azure 易限流）。以大批次提高詞/秒，遇 429 自動退避。")

    # ═══════════════════════════════════════════════
    #  輸出格式切換 → 更新提示文字
    # ═══════════════════════════════════════════════
    SIDEBAR_GUIDE_TEXT = {
        "resource_pack": ("輸出後怎麼用：\n"
                          "① 資源包 ZIP 放進 resourcepacks/ 並啟用\n"
                          "② 若 ZIP 內有 config/ 或 defaultconfigs/，再解壓到遊戲根目錄\n"
                          "③ 預設不改 JAR，啟動崩潰風險最低"),
        "hybrid": ("輸出後怎麼用：\n"
                   "① 先啟用資源包 ZIP\n"
                   "② config/defaultconfigs 再解壓到遊戲根目錄\n"
                   "③ 若有 Class 補丁 ZIP，再解壓至實例根目錄；原檔在 _backups/"),
        "jar_patch": ("輸出後怎麼用：\n"
                      "① 關閉遊戲與啟動器\n"
                      "② 翻譯 ZIP 整包解壓到遊戲根目錄並覆蓋\n"
                      "③ 原始檔可由 _backups/ 還原；不需資源包"),
    }

    def _on_output_mode_change(self):
        mode = self.output_mode_var.get()
        if mode != "jar_patch":
            mode = "jar_patch"
            self.output_mode_var.set(mode)
        self.class_tooltip_patch_var.set(True)
        hints = {
            "resource_pack": "  安全優先：輸出標準資源包；不重包 JAR，不修改 .class",
            "hybrid": "  自動輸出資源/設定；可選低風險 class/JAR 補丁",
            "jar_patch": "  直接重建 mods/JAR + 設定；不需資源包，原始檔附於 _backups/",
        }
        self.output_mode_hint.config(text=hints[mode])
        if hasattr(self, "sidebar_guide_label"):
            self.sidebar_guide_label.config(
                text=self.SIDEBAR_GUIDE_TEXT[mode])
        self._refresh_output_summary()

    # ═══════════════════════════════════════════════
    #  瀏覽資料夾
    # ═══════════════════════════════════════════════
    def browse_mod_dir(self):
        folder = filedialog.askdirectory(title="選擇 Minecraft 資料夾")
        if folder:
            self.mod_dir_var.set(folder)

    def browse_rp_dir(self):
        folder = filedialog.askdirectory(title="選擇 輸出 資料夾")
        if folder:
            self.rp_dir_var.set(folder)

    # ═══════════════════════════════════════════════
    #  流程控制
    # ═══════════════════════════════════════════════
    def stop_process(self):
        if self.is_processing:
            self.stop_requested  = True
            self.pause_requested = False
            self.log("\n⚠️ 已請求停止，正在取消排隊批次並儲存已完成進度...")
            self._set_btn_state(self.btn_stop,  tk.DISABLED)
            self._set_btn_state(self.btn_pause, tk.DISABLED)
            self._current_item_last = 0.0
            self.set_current_item("停止中…正在取消排隊批次並儲存進度")
            if hasattr(self, "progress_label"):
                self.progress_label.config(text="停止中…")

    def pause_process(self):
        if self.is_processing:
            self.stop_requested  = True
            self.pause_requested = True
            self.log("\n⏸️ 已請求暫停，正在取消排隊批次並儲存進度...")
            self._set_btn_state(self.btn_stop,  tk.DISABLED)
            self._set_btn_state(self.btn_pause, tk.DISABLED)
            self._current_item_last = 0.0
            self.set_current_item("暫停中…正在取消排隊批次並儲存進度")
            if hasattr(self, "progress_label"):
                self.progress_label.config(text="暫停中…")
            self._set_summary_card(
                "pending", "暫停中",
                "正在取消網路請求並快速儲存進度",
                self.C_WARN)
            self._refresh_api_summary(state="暫停中", color=self.C_WARN)
            self.save_config()

    # ═══════════════════════════════════════════════
    #  日誌與進度條
    # ═══════════════════════════════════════════════
    LOG_MAX_LINES = 2000   # 日誌區行數上限，超過即裁掉最舊的，避免長任務越跑越卡

    def _start_log_polling(self):
        if self._log_polling:
            return
        self._log_polling = True
        self.root.after(80, self._flush_log)

    def log(self, message):
        try:
            # Worker threads only append data. Tk calls stay on the main thread
            # through the fixed polling callback started after log_area exists.
            with self._log_lock:
                self._log_queue.append(str(message))
        except Exception:
            try:
                encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                safe_message = str(message).encode(
                    encoding, errors="replace").decode(encoding, errors="replace")
                print(safe_message)
            except Exception:
                pass

    def _flush_log(self):
        try:
            lines = []
            with self._log_lock:
                while self._log_queue:
                    lines.append(self._log_queue.popleft())
            if lines:
                self._safe_log("\n".join(lines))
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.after(80, self._flush_log)
            except Exception:
                self._log_polling = False

    def _safe_log(self, message):
        try:
            # 使用者往上捲動閱讀時不要搶捲動位置，只有原本就在底部才跟隨
            follow = self.log_area.yview()[1] >= 0.999
            self.log_area.configure(state='normal')
            self.log_area.insert(tk.END, message + "\n")
            total_lines = int(self.log_area.index('end-1c').split('.')[0])
            if total_lines > self.LOG_MAX_LINES:
                self.log_area.delete('1.0', f'{total_lines - self.LOG_MAX_LINES}.0')
            if follow:
                self.log_area.see(tk.END)
            self.log_area.configure(state='disabled')
        except Exception:
            pass

    def update_progress(self, current, total, text_mode=False):
        percent = (current / total * 100) if total > 0 else 0.0
        try:
            if self.root.winfo_exists():
                self.root.after(0, self._set_progress, percent, current, total, text_mode)
        except Exception:
            pass

    def set_current_item(self, text, force=False):
        """更新進度面板的「目前處理項目」（節流：最快 0.2s 更新一次）"""
        now = time.time()
        if not force and now - getattr(self, "_current_item_last", 0.0) < 0.2:
            return
        self._current_item_last = now

        def apply():
            label = getattr(self, "current_item_label", None)
            if label:
                label.config(text=text)
        try:
            if self.root.winfo_exists():
                self.root.after(0, apply)
        except Exception:
            pass

    def _reset_progress_counters(self):
        self._progress_started_at = time.time()
        self._progress_error_count = 0
        self._progress_translated_count = 0
        self._progress_skipped_count = 0
        self._progress_explicit_counts = False
        self._progress_samples = deque(maxlen=30)

    def _note_progress_error(self, count=1):
        self._progress_error_count = getattr(self, "_progress_error_count", 0) + count

    def _add_progress_counts(self, translated=0, skipped=0, errors=0):
        self._progress_explicit_counts = True
        self._progress_translated_count = (
            getattr(self, "_progress_translated_count", 0) + max(0, translated))
        self._progress_skipped_count = (
            getattr(self, "_progress_skipped_count", 0) + max(0, skipped))
        if errors:
            self._note_progress_error(errors)

    @staticmethod
    def _format_eta(seconds):
        seconds = max(0, int(seconds))
        if seconds >= 3600:
            return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _set_progress(self, percent, current, total, text_mode):
        self.progress_var.set(percent)
        unit = "詞彙" if text_mode else "檔"
        self.progress_label.config(
            text=f"{percent:.1f}%  ({current}/{total} {unit})",
            fg=self.C_ACCENT if percent > 0 else self.C_MUTED)
        stats = getattr(self, "progress_stat_labels", None)
        if not stats:
            return

        if total > 0 and current > 0 and current < total:
            # 滑動視窗速率：用最近 30 個樣本估算，
            # 避免「快取先瞬間衝完、之後打 API 變慢」時 ETA 嚴重失準
            samples = getattr(self, "_progress_samples", None)
            if samples is None:
                samples = self._progress_samples = deque(maxlen=30)
            now = time.time()
            samples.append((now, current))
            rate = None
            if len(samples) >= 2:
                (t1, c1), (t2, c2) = samples[0], samples[-1]
                if t2 > t1 and c2 > c1:
                    rate = (c2 - c1) / (t2 - t1)
            if rate is None:
                elapsed = max(0.1, now - getattr(self, "_progress_started_at", now))
                rate = current / elapsed
            eta = self._format_eta((total - current) / max(rate, 1e-6))
        elif total > 0 and current >= total:
            eta = "00:00"
        else:
            eta = "--:--"

        explicit_counts = getattr(self, "_progress_explicit_counts", False)
        if text_mode and not explicit_counts:
            translated = current
            skipped = 0
        else:
            translated = getattr(self, "_progress_translated_count", 0)
            skipped = getattr(self, "_progress_skipped_count", 0)
        values = {
            "percent": f"{percent:.1f}%",
            "processed": f"{current:,}",
            "translated": f"{translated:,}",
            "skipped": f"{skipped:,}",
            "errors": f"{getattr(self, '_progress_error_count', 0):,}",
            "eta": eta,
        }
        for key, value in values.items():
            label = stats.get(key)
            if label:
                label.config(text=value)

        if text_mode and total > 0:
            remaining = max(0, total - current)
            state = "輸出中" if remaining == 0 else "翻譯中"
            self._set_summary_card(
                "pending", f"{remaining:,}",
                f"已處理：{current:,}/{total:,}\n狀態：{state}",
                self.C_SUCCESS if remaining == 0 else self.C_WARN)

    # ═══════════════════════════════════════════════
    #  編碼與檔案讀取
    # ═══════════════════════════════════════════════
    def safe_decode_bytes(self, b_data):
        # UTF-16 BOM（極少數模組的 datagen 產物）
        if b_data[:2] in (b'\xff\xfe', b'\xfe\xff'):
            try:
                return b_data.decode('utf-16')
            except UnicodeDecodeError:
                pass
        # UTF-8 嚴格優先（utf-8-sig 會自動剝除 BOM；
        # 不能用普通 utf-8 先試——BOM 會「成功」解碼成隱形
        # 留在字串開頭，下游 json.loads 直接拒收）
        try:
            return b_data.decode('utf-8-sig')
        except UnicodeDecodeError:
            pass
        # GBK 與 Big5 雙位元組空間高度重疊：Big5 內容幾乎總能被 GBK「成功」
        # 解碼成亂碼（§a 的 0xA7 還會跟後字節合併、吞掉格式碼）。
        # 不能用「先成功先贏」，改用 CJK 比例評分挑最合理的解碼結果。
        best, best_score = None, float('-inf')
        for enc in ('gbk', 'big5', 'cp950'):
            try:
                cand = b_data.decode(enc)
            except UnicodeDecodeError:
                continue
            cjk = len(self._RE_CJK_CHAR.findall(cand))
            # 私用區/擴展區字元視為亂碼訊號扣分
            rare = sum(1 for ch in cand
                       if 0xE000 <= ord(ch) <= 0xF8FF or 0x3400 <= ord(ch) <= 0x4DBF)
            score = cjk - rare * 3
            if score > best_score:
                best, best_score = cand, score
        if best is not None:
            return best
        for enc in ('cp1252', 'latin-1'):
            try:
                return b_data.decode(enc)
            except UnicodeDecodeError:
                continue
        return b_data.decode('utf-8', errors='ignore')

    def safe_read_file(self, path):
        try:
            with open(path, 'rb') as f:
                return self.safe_decode_bytes(f.read())
        except OSError as e:
            self.log(f"⚠️ 無法讀取檔案 {path}: {e}")
            return ""

    # ═══════════════════════════════════════════════
    #  快取管理
    # ═══════════════════════════════════════════════
    # ── 繁體中文驗證：簡體字（Simplified-only）字元集 ──
    # 僅收錄「在繁體中文中寫法不同」的簡體字，用來偵測誤存的簡體翻譯。
    # 注意：炸/生/解/手 等簡繁相同的字已排除，避免正確翻譯被誤判為簡體而從快取移除。
    _SIMP_ONLY_CHARS = frozenset(
        '\u4eec\u56fd\u65f6\u8fd9\u6765\u8bf4\u5bf9\u8fdb\u73b0\u8fc7'  # 们国时这来说对进现过
        '\u53d1\u673a\u7535\u4e2a\u4ea7\u540e\u7ecf\u5b9e\u52a8\u5b66'  # 发机电个产后经实动学
        '\u957f\u5934\u4e49\u95f4\u4e1c\u95ee\u8fd8\u4ece\u52a1\u5f53'  # 长头义间东问还从务当
        '\u7ec4\u7ec7\u7edf\u8bdd\u8ba4\u8bc6\u6ee1\u5904\u8fb9\u5757'  # 组织统话认识满处边块
        '\u94c1\u9492\u6811\u7ea2\u7eff\u84dd\u5251\u77ff\u6218\u5f00'  # 铁钻树红绿蓝剑矿战开
        '\u5173\u8bbe\u6743\u4e66\u9c7c\u79cd\u6837\u7ea7\u987b\u5458'  # 关设权书鱼种样级须员
        '\u6761\u89c6\u8f7d\u8fbe\u8f6c\u8bb0\u8f83\u62a5\u5e26\u89c4'  # 条视载达转记较报带规
        '\u5c42\u7ebf\u5355\u753b\u94f6\u94fe\u989c\u7b51\u5e01\u7c7b'  # 层线单画银链颜筑币类
        '\u7231\u5c81\u9500\u5f52\u573a\u4ef7\u4f18\u8fd0\u636e\u8bf7'  # 爱岁销归场价优运据请
        '\u53f6\u9635\u79bb\u603b\u7ec8\u968f\u7eb8\u7ee7\u7ea6\u4f20'  # 叶阵离总终随纸继约传
        '\u5bfc\u7ed3\u56fe\u7f51\u70ed\u7b80\u6807\u9009\u8f93\u53f7'  # 导结图网热简标选输号
        '\u8d39\u9891\u4e60\u8282\u9898\u7eaa\u8fde\u8ba1\u6c14\u5e93'  # 费频习节题纪连计气库
        '\u5c06\u5e94\u5c14\u9a6c\u9e1f\u9f99\u7075\u8f6f\u7ec3\u94ae'  # 将应尔马鸟龙灵软练钮
        '\u8bed\u5899\u534e\u4e1a\u7ea0\u8d28\u65e0'                    # 语墙华业纠质无（已移除：炸\u70b8/展\u5c55/宇\u5b87 簡繁相同）
        '\u6d4b\u9009\u4e2a\u53c2\u5907'                                # 测选个参备（已移除：制\u5236/解\u89e3/生\u751f/手\u624b 簡繁相同）
    )
    # 繁體中文辨識字元集（繁體專用，簡體中沒有的字元）
    _TRAD_ONLY_CHARS = frozenset(
        '\u5011\u570b\u6642\u9019\u4f86\u8aaa\u5c0d\u9032\u73fe\u904e'  # 們國時這來說對進現過
        '\u767c\u6a5f\u96fb\u500b\u7522\u5f8c\u7d93\u5be6\u52d5\u5b78'  # 發機電個產後經實動學
        '\u9577\u982d\u7fa9\u9593\u6771\u554f\u9084\u5f9e\u52d9\u7576'  # 長頭義間東問還從務當
        '\u7d44\u7e54\u7d71\u8a71\u8a8d\u8b58\u6e80\u8655\u908a\u584a'  # 組織統話認識滿處邊塊
        '\u9435\u9e51\u6a39\u7d05\u7da0\u85cd\u528d\u7934\u6230\u958b'  # 鐵鑽樹紅綠藍劍礦戰開
        '\u95dc\u8a2d\u6b0a\u66f8\u9b5a\u7a2e\u6a23\u7d1a\u9808\u54e1'  # 關設權書魚種樣級須員
        '\u689d\u8996\u8f09\u9054\u8f49\u8a18\u8f03\u5831\u5e36\u898f'  # 條視載達轉記較報帶規
        '\u5c64\u7dda\u55ae\u756b\u9280\u93c8\u984f\u7bc9\u5e63\u985e'  # 層線單畫銀鏈顏築幣類
        '\u611b\u6b72\u92f8\u6b78\u5834\u50f9\u512a\u904b\u64da\u8acb'  # 愛歲銷歸場價優運據請
        '\u8449\u9663\u96e2\u7e3d\u7d42\u96a8\u7d19\u7e7c\u7d04\u50b3'  # 葉陣離總終隨紙繼約傳
        '\u5c0e\u7d50\u5716\u7db2\u71b1\u7c21\u6a19\u9078\u8f38\u865f'  # 導結圖網熱簡標選輸號
        '\u8cbb\u983b\u7fd2\u7bc0\u984c\u7d00\u9023\u8a08\u6c23\u5eab'  # 費頻習節題紀連計氣庫
        '\u5c07\u61c9\u723e\u99ac\u9ce5\u9f8d\u9748\u8edf\u7df4\u9215'  # 將應爾馬鳥龍靈軟練鈕
        '\u8a9e\u7246\u83ef\u55ae\u696d\u5c55\u7cfe\u8cea\u7121\u5b87'  # 語牆華單業展糾質無宇
    )
    _RE_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

    # ── 簡→繁字元對照表：僅含有確定對應繁體字的簡體字，用於自動轉換 ──
    _SIMP_TO_TRAD_TABLE = str.maketrans(
        # 簡體（來源）
        '们国时这来说对进现过'
        '发机电个产后经实动学'
        '长头义间东问还从务当'
        '组织统话认识满处边块'
        '铁钻树红绿蓝剑矿战开'
        '关设权书鱼种样级须员'
        '条视载达转记较报带规'
        '层线单画银链颜筑币类'
        '爱岁销归场价优运据请'
        '叶阵离总终随纸继约传'
        '导结图网热简标选输号'
        '费频习绝节题纪连内计'
        '气库将应尔马鸟龙灵纠'
        '质无软练钮语墙华业声'
        '车门风联买卖历难观龄'
        '装备防护击灭验证损伤'
        ,
        # 繁體（目標）
        '們國時這來說對進現過'
        '發機電個產後經實動學'
        '長頭義間東問還從務當'
        '組織統話認識滿處邊塊'
        '鐵鑽樹紅綠藍劍礦戰開'
        '關設權書魚種樣級須員'
        '條視載達轉記較報帶規'
        '層線單畫銀鏈顏築幣類'
        '愛歲銷歸場價優運據請'
        '葉陣離總終隨紙繼約傳'
        '導結圖網熱簡標選輸號'
        '費頻習絕節題紀連內計'
        '氣庫將應爾馬鳥龍靈糾'
        '質無軟練鈕語牆華業聲'
        '車門風聯買賣歷難觀齡'
        '裝備防護擊滅驗證損傷'
    )

    _SIMP_TO_TRAD_PHRASES = (
        ('圣骑士', '聖騎士'),
        ('圣骑', '聖騎'),
        ('干枯', '乾枯'),
        ('装备', '裝備'),
        ('防御', '防禦'),
        ('伤害', '傷害'),
        ('护甲', '護甲'),
        ('攻击', '攻擊'),
        ('灭火', '滅火'),
        ('验证', '驗證'),
        ('损伤', '損傷'),
    )

    @staticmethod
    def _to_traditional(text: str) -> str:
        """將文字中的簡體中文字元轉換為繁體中文。"""
        if not text:
            return text
        if _OPENCC_TW is not None:
            try:
                return _OPENCC_TW.convert(text)
            except Exception:
                pass
        for src, dst in ModTranslatorApp._SIMP_TO_TRAD_PHRASES:
            text = text.replace(src, dst)
        return text.translate(ModTranslatorApp._SIMP_TO_TRAD_TABLE)

    @classmethod
    def _is_valid_trad_translation(cls, orig: str, trans: str) -> bool:
        """判斷翻譯結果是否為有效的繁體中文。
        - 值必須含有 CJK 字元
        - 不得只含簡體特有字元而無繁體特有字元（即不能是純簡體中文）
        - 也不能與原文完全相同（未翻譯）
        注意：很多正確繁體中文字（石頭、木材、地牢…）是簡繁共用字，
        不屬於 _SIMP_ONLY_CHARS 也不屬於 _TRAD_ONLY_CHARS，
        只要含有 CJK 且不是「只有簡體特有字、完全沒有繁體特有字」就算合法。"""
        if not isinstance(trans, str) or not trans.strip() or trans == orig:
            return False
        if any(mark in trans for mark in ('�', 'Ã', 'Â', 'â€™', 'â€œ', 'â€')):
            return False
        if trans.strip() == f"{orig.strip()}-繁中":
            return False
        if not cls._RE_CJK.search(trans):
            return False   # 完全沒有 CJK 字元 → 非中文翻譯
        has_simp = any(c in cls._SIMP_ONLY_CHARS for c in trans)
        has_trad = any(c in cls._TRAD_ONLY_CHARS for c in trans)
        # 只有簡體特有字且完全沒有繁體特有字 → 純簡體，拒絕
        # 有繁體特有字 → 繁體，接受
        # 兩者都沒有（全是簡繁共用字）→ 無法判斷，保守接受（不誤殺正確翻譯）
        if has_simp and not has_trad:
            return False
        return True

    @classmethod
    def _looks_untranslated_lang_value(cls, source, translated):
        if not isinstance(source, str) or not isinstance(translated, str):
            return True
        src = source.strip()
        dst = translated.strip()
        if not dst or dst == src or dst == src.strip():
            return True
        if dst == src.split(':')[-1].strip():
            return True
        if cls._RE_CJK.search(dst):
            return False
        # Keep pure IDs, symbols and intentional abbreviations out of the retry set.
        letters = re.findall(r'[A-Za-z]', dst)
        if len(letters) < 4:
            return False
        if cls._RE_NAMESPACE.match(dst) or cls._RE_FILEPATH.match(dst) or cls._RE_COLOR.match(dst):
            return False
        return bool(re.search(r'[A-Za-z]{4,}', dst))

    @classmethod
    def _lang_value_is_mixed(cls, translated):
        """既有 zh 值是否為「混英值」：已含中文、但去掉格式碼後仍殘留 ≥4 字母英文單詞
        （如「Botania 花朵」）。判定與覆蓋檢查的「lang 混英值」一致。
        這類 key 必須重新收集進詞彙池，階段二.八才看得到、才有機會升級成完整中文；
        否則 append 模式會把它們當「已翻譯」跳過，混英永遠留在 JAR 裡。"""
        if not isinstance(translated, str):
            return False
        if not cls._RE_CJK_CHAR.search(translated):
            return False
        return bool(cls._RE_EN_WORD.search(cls._RE_FORMAT.sub('', translated)))

    @classmethod
    def _looks_like_structural_reference(cls, text):
        """判斷字串是否像資源/結構引用，而不是玩家可見文字。

        Prefab、Patchouli、任務書與資料包常把 .nbt/.schem 等檔名放在 JSON/SNBT
        字串值中。這些值一旦被翻譯，遊戲會在執行時讀不到原始檔案而崩潰。
        """
        if not isinstance(text, str):
            return False
        value = text.strip().strip('"\'')
        if not value or '\n' in value or '\r' in value:
            return False
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9+.\-]*://\S+', value):
            return True

        normalized = value.replace('\\', '/')
        if cls._RE_NAMESPACE.match(value) or cls._RE_LANG_KEY_REF.match(value):
            return True

        match = re.search(r'\.([A-Za-z0-9]{2,16})$', normalized)
        if match:
            ext = match.group(1).lower()
            if ext in cls._HIGH_RISK_STRUCTURAL_EXTS:
                return True
            if ext in cls._STRUCTURAL_REF_EXTS and (
                    '/' in normalized or ' ' not in value or cls._RE_FILEPATH.match(normalized)):
                return True

        if '/' in normalized and ' ' not in value:
            return bool(re.fullmatch(r'[A-Za-z0-9_./:\-]+', normalized))
        return False

    @classmethod
    def _lang_value_needs_update(cls, source, translated):
        """判斷既有 zh_tw lang 值是否需要補翻/重翻。"""
        if not isinstance(source, str) or not isinstance(translated, str):
            return True
        if cls._looks_like_structural_reference(source):
            return translated.strip() != source.strip()
        if (Counter(cls._critical_format_tokens(source))
                != Counter(cls._critical_format_tokens(translated))):
            return True
        return (cls._looks_untranslated_lang_value(source, translated)
                or cls._lang_value_is_mixed(translated))

    def load_cache(self):
        self.cache_file = self._current_cache_file()
        cache, messages = load_translation_cache(
            self.cache_file, self._RE_FORMAT, self._is_valid_trad_translation)
        for message in messages:
            self.log(message)
        return cache

    def _current_cache_file(self):
        engine = self._normalize_engine_route_value(self.engine_var.get()) if hasattr(self, "engine_var") else "market_ai"
        ai_engines = {"market_ai", "openai", "claude", "local"}
        return self.cache_file_ai if engine in ai_engines else self.cache_file_std

    def _default_dictionary(self):
        return {}

    def load_dictionary(self):
        return {}

    def _dictionary_exact(self, source):
        return None

    def _apply_dictionary_fixes(self, source, translated):
        return translated

    def _seed_cache_from_dictionary(self, unique_strings):
        return 0

    def save_cache(self, light=False):
        """Persist cache progress without making checkpoints run full review.

        SQLite checkpoints flush pending rows. The in-memory fallback uses an
        atomic pkl replacement. A full save additionally merges and persists
        the global translation memory pool.
        """
        if not self._cache_lock.acquire(blocking=False):
            return   # 已有執行緒在儲存，略過
        try:
            session_keys = set(
                getattr(self, "_session_translated_keys", set()) or set())
            if light:
                sync_cache = getattr(self.translation_cache, "sync", None)
                if callable(sync_cache):
                    # 覆寫既有 key 不會改變 len；checkpoint 仍須 flush pending。
                    sync_cache()
                else:
                    current_len = len(self.translation_cache)
                    # 記憶體 fallback 沒有 dirty revision；本輪有翻譯時不能
                    # 用長度相同推論內容未被覆寫。
                    if (current_len != getattr(self, "_last_light_len", -1)
                            or session_keys):
                        cache_file = self._current_cache_file()
                        tmp_pkl = cache_file + '.pkl.tmp'
                        with open(tmp_pkl, 'wb') as f:
                            pickle.dump(sanitize_value(dict(self.translation_cache)), f,
                                        protocol=pickle.HIGHEST_PROTOCOL)
                        os.replace(tmp_pkl, cache_file + '.pkl')
                self._last_light_len = len(self.translation_cache)
                self.last_save_time = time.time()
            else:
                self.last_save_time = save_translation_cache(
                    self._current_cache_file(), self.translation_cache)
            if self.global_memory_var.get():
                if light:
                    # Mid-run checkpoints must stay cheap. Per-entry OpenCC and
                    # validation over tens of thousands of keys made pause take
                    # minutes. SQLite rows are already durable; merge memory
                    # once during the normal stage-2.5 full save.
                    if session_keys:
                        self._memory_pool_dirty = True
                else:
                    changed = 0
                    # 記憶池已在 _load_translation_memory 的 guard 中快取，
                    # 無需重複載入——只有首次呼叫才會從磁碟讀取
                    if not getattr(self, "translation_memory", None):
                        self._load_translation_memory()
                    merged = getattr(self, "_memory_merged_keys", None)
                    if merged is None:
                        merged = self._memory_merged_keys = set()
                    # Include analyzed cache hits so a paused-and-resumed run can
                    # still populate the global memory pool on final completion.
                    candidate_keys = session_keys | set(
                        getattr(self, "_analysis_unique_strings", set()) or set())
                    cached_candidates = cache_snapshot(
                        self.translation_cache, candidate_keys)
                    for source, target in cached_candidates.items():
                        if source in merged:
                            continue
                        before = self.translation_memory.get(source)
                        if (self._add_memory_pair(source, target)
                                and before != self.translation_memory.get(source)):
                            changed += 1
                        merged.add(source)
                    if session_keys:
                        self._session_translated_keys.difference_update(session_keys)
                    if changed or getattr(self, "_memory_pool_dirty", False):
                        self._save_translation_memory()
                    self._memory_pool_dirty = False
        except OSError as e:
            self.log(f"⚠️ 無法儲存快取: {e}")
        finally:
            self._cache_lock.release()
        if hasattr(self, "_refresh_footer_info"):
            self._refresh_footer_info()

    def _maybe_save_cache(self):
        """每 30 秒自動儲存一次快取，避免高速翻譯時過度 sync 拖慢 UI。"""
        if time.time() - self.last_save_time > 30:
            self.save_cache(light=True)

    def _review_and_fix_cache(self):
        """翻譯後快取複查：
        1. 嘗試用 _to_traditional() 將殘存的簡體字元轉換為繁體
        2. 轉換後仍不合格的條目從快取移除（讓下次重新翻譯）
        3. 記錄統計資訊至日誌
        注意：先取快照再遍歷，防止遍歷期間 worker 執行緒同時修改字典。"""
        self.log("\n--- 快取繁體中文複查 ---")
        if (getattr(self, "stop_requested", False)
                or getattr(self, "pause_requested", False)):
            self.log("ℹ️ 已取消快取複查，保留目前翻譯進度。")
            return
        session_keys = set(getattr(self, "_session_translated_keys", set()) or set())
        if hasattr(self.translation_cache, "bulk_update"):
            snapshot = cache_snapshot(self.translation_cache, session_keys)
            if not snapshot:
                self.log("ℹ️ 本輪沒有新增快取條目，跳過全量複查。")
                return
        else:
            snapshot = dict(self.translation_cache)   # 快照，避免遍歷中被修改
        new_cache, stats = review_and_fix_cache(
            snapshot, self._RE_FORMAT, self._to_traditional,
            self._is_valid_trad_translation,
            should_cancel=lambda: (
                getattr(self, "stop_requested", False)
                or getattr(self, "pause_requested", False)))
        if stats.get("cancelled"):
            self.log("ℹ️ 已中止快取複查；未套用部分掃描結果。")
            return
        # Patchouli 巨集治癒：把歷史翻譯弄壞的 $ ( ) / !~() 修回 $()
        macro_fixed = 0
        for k, v in new_cache.items():
            if isinstance(v, str):
                fixed = self._repair_patchouli_macros(v)
                if fixed != v:
                    new_cache[k] = fixed
                    macro_fixed += 1
        if macro_fixed:
            self.log(f"🩹 Patchouli 巨集治癒：修復 {macro_fixed} 筆快取條目")
        if hasattr(self.translation_cache, "bulk_update"):
            removed_keys = [key for key in snapshot if key not in new_cache]
            changed_pairs = [
                (key, value)
                for key, value in new_cache.items()
                if snapshot.get(key) != value
            ]
            for key in removed_keys:
                if key not in new_cache:
                    self.translation_cache.pop(key, None)
            if changed_pairs:
                self.translation_cache.bulk_update(changed_pairs)
        else:
            self.translation_cache = new_cache
        self.log(f"✅ 複查完成：共 {stats['before']} 筆  "
                 f"已轉換 {stats['converted']} 筆簡體→繁體  "
                 f"移除 {stats['removed']} 筆無效/格式不符條目  "
                 f"保留 {stats['kept']} 筆")
        if stats['removed'] > 0:
            self.log(f"ℹ️  已移除的 {stats['removed']} 筆條目將在下次翻譯時補齊")
        if stats['converted'] or stats['removed'] or macro_fixed or getattr(self, "_memory_pool_dirty", False):
            self.save_cache()
        else:
            self.log("ℹ️ 快取無需修正，略過完整重寫。")
            self.save_cache(light=True)

    # ═══════════════════════════════════════════════
    #  設定檔
    # ═══════════════════════════════════════════════
    @staticmethod
    def _obfuscate(key: str) -> str:
        return core_obfuscate(key)

    @staticmethod
    def _deobfuscate(encoded: str) -> str:
        return core_deobfuscate(encoded)

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                self.mod_dir_var.set(config.get('mod_dir', ''))
                self.rp_dir_var.set(config.get('rp_dir', ''))
                self.rp_name_var.set(config.get('rp_name', 'Auto_Translated_Mods_zh_tw'))
                if hasattr(self, "datapack_name_var"):
                    self.datapack_name_var.set(config.get('datapack_name', ''))
                self.api_key_var.set(self._deobfuscate(config.get('api_key_enc', '')))
                self.deepl_key_var.set(self._deobfuscate(config.get('deepl_key_enc', '')))
                self.azure_key_var.set(self._deobfuscate(config.get('azure_key_enc', '')))
                self.azure_region_var.set(config.get('azure_region', 'eastasia'))
                self.azure_endpoint_var.set(config.get('azure_endpoint', ''))
                self.claude_key_var.set(self._deobfuscate(config.get('claude_key_enc', '')))
                self.claude_model_var.set(config.get('claude_model', 'claude-haiku-4-5-20251001'))
                self.openai_key_var.set(self._deobfuscate(config.get('openai_key_enc', '')))
                self.openai_model_var.set(config.get('openai_model', 'gpt-5.4-mini'))
                self.ai_provider_menu_var.set(config.get('ai_provider_menu', self.ai_provider_menu_var.get()))
                saved_provider = config.get('ai_provider', 'OpenAI')
                if saved_provider in ('argos', 'Argos Translate 離線'):
                    # Argos 已移除（離線庫未隨 EXE 打包，永遠不可用）→ 遷移到 Bing 免費
                    saved_provider = 'Bing 免費翻譯（免 API）'
                    self.log("INFO  Argos Translate 已移除，供應商自動改為「Bing 免費翻譯（免 API）」")
                self.ai_provider_var.set(saved_provider)
                self._sync_ai_provider_menu()
                self._on_ai_provider_change()
                self.ai_auth_mode_var.set(config.get('ai_auth_mode', 'api'))
                self.ai_api_key_var.set(self._deobfuscate(config.get('ai_api_key_enc', '')))
                self.ai_api_keys_var.set(self._deobfuscate(config.get('ai_api_keys_enc', '')))
                self.ai_model_var.set(config.get('ai_model', self.ai_model_var.get()))
                self.ai_base_url_var.set(config.get('ai_base_url', self.ai_base_url_var.get()))
                self.ai_login_url_var.set(config.get('ai_login_url', self.ai_login_url_var.get()))
                self.engine_var.set(self._normalize_engine_route_value(config.get('engine', 'market_ai')))
                self.local_url_var.set(config.get('local_url', 'http://localhost:1234/v1/chat/completions'))
                self.mc_version_var.set('等待自動判定')
                self.workers_var.set(config.get('workers', 8))
                self.output_mode_var.set('jar_patch')
                saved_process_mode = config.get('process_mode', 'append')
                if saved_process_mode == 'force':
                    saved_process_mode = 'append'
                    self.log("INFO  已將上次「強制重翻」自動重置為「補缺」，避免誤跑完整模組包。")
                self.process_mode_var.set(saved_process_mode)
                self.retry_count_var.set(config.get('retry_count', 3))
                self.scope_mod_lang_var.set(config.get('scope_mod_lang', True))
                self.scope_books_var.set(config.get('scope_books', True))
                self.scope_quests_var.set(config.get('scope_quests', True))
                self.datapack_output_var.set(config.get('datapack_output', True))
                self.global_memory_var.set(config.get('global_memory', True))
                self.strict_whitelist_var.set(config.get('strict_whitelist', True))
                self.update_detect_var.set(config.get('update_detect', True))
                self.include_large_backups_var.set(config.get('include_large_backups', False))
                self.class_tooltip_patch_var.set(True)
                self.auto_normalize_endpoint_var.set(config.get('auto_normalize_endpoint', True))
                self._on_mc_version_change()
                self._on_engine_change()
                self._on_output_mode_change()
            except (json.JSONDecodeError, KeyError, OSError):
                self.log("⚠️ 設定檔讀取失敗，使用預設值。")

    def save_config(self):
        try:
            config = {
                'mod_dir':         self.mod_dir_var.get(),
                'rp_dir':          self.rp_dir_var.get(),
                'rp_name':         self.rp_name_var.get(),
                'datapack_name':   self.datapack_name_var.get(),
                'api_key_enc':     self._obfuscate(self.api_key_var.get()),
                'deepl_key_enc':   self._obfuscate(self.deepl_key_var.get()),
                'azure_key_enc':   self._obfuscate(self.azure_key_var.get()),
                'azure_region':    self.azure_region_var.get(),
                'azure_endpoint':  self.azure_endpoint_var.get(),
                'claude_key_enc':  self._obfuscate(self.claude_key_var.get()),
                'claude_model':    self.claude_model_var.get(),
                'openai_key_enc':  self._obfuscate(self.openai_key_var.get()),
                'openai_model':    self.openai_model_var.get(),
                'ai_provider_menu': self.ai_provider_menu_var.get(),
                'ai_provider':     self.ai_provider_var.get(),
                'ai_auth_mode':    self.ai_auth_mode_var.get(),
                'ai_api_key_enc':  self._obfuscate(self.ai_api_key_var.get()),
                'ai_api_keys_enc': self._obfuscate(self.ai_api_keys_var.get()),
                'ai_model':        self.ai_model_var.get(),
                'ai_base_url':     self.ai_base_url_var.get(),
                'ai_login_url':    self.ai_login_url_var.get(),
                'engine':          self._normalize_engine_route_value(self.engine_var.get()),
                'local_url':       self.local_url_var.get(),
                'mc_version':      self.mc_version_var.get(),
                'pack_format':     self.pack_format_var.get(),
                'datapack_format': self.datapack_format_var.get(),
                'workers':         self.workers_var.get(),
                'output_mode':     self.output_mode_var.get(),
                'process_mode':    self.process_mode_var.get(),
                'retry_count':     self.retry_count_var.get(),
                'scope_mod_lang':  self.scope_mod_lang_var.get(),
                'scope_books':     self.scope_books_var.get(),
                'scope_quests':    self.scope_quests_var.get(),
                'datapack_output': self.datapack_output_var.get(),
                'global_memory':   self.global_memory_var.get(),
                'strict_whitelist': self.strict_whitelist_var.get(),
                'update_detect':   self.update_detect_var.get(),
                'include_large_backups': self.include_large_backups_var.get(),
                'class_tooltip_patch': self.class_tooltip_patch_var.get(),
                'auto_normalize_endpoint': self.auto_normalize_endpoint_var.get(),
            }
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except OSError as e:
            self.log(f"⚠️ 設定檔儲存失敗: {e}")

    def _schedule_save(self, *args):
        """防抖動：最後一次設定變更後 1.5 秒自動儲存"""
        if self._save_timer is not None:
            self.root.after_cancel(self._save_timer)
        self._save_timer = self.root.after(1500, self.save_config)

    def _set_btn_state(self, btn, state):
        btn.config(state=state)
        if state == tk.DISABLED:
            btn.config(bg=self.C_BORDER)
        else:
            btn.config(bg=getattr(btn, '_base_color', self.C_BORDER))

    @staticmethod
    def _shutdown_executor_now(executor):
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    @staticmethod
    def _safe_zip_filename(name, default_name):
        raw = (name or "").strip() or default_name
        if raw.lower().endswith(".zip"):
            raw = raw[:-4]
        raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw).strip(" ._")
        if not raw:
            raw = default_name
        return raw + ".zip"

    # ═══════════════════════════════════════════════
    #  分析任務
    # ═══════════════════════════════════════════════
    def _auto_start_analysis(self):
        if self.is_processing:
            self.root.after(500, self._auto_start_analysis)
            return
        mod_dir = self.mod_dir_var.get().strip()
        if not os.path.isdir(mod_dir):
            self.log("⚠️ 自動分析略過：找不到 Minecraft 資料夾。")
            return
        self.log("INFO  自動啟動：開始分析檔案。")
        self.start_analysis()

    def start_analysis(self):
        if self.is_processing:
            return
        mod_dir = self.mod_dir_var.get().strip()
        if not os.path.isdir(mod_dir):
            messagebox.showerror("錯誤", "找不到 Minecraft 資料夾！")
            return

        self.is_processing   = True
        self.stop_requested  = False
        self.pause_requested = False
        self._set_btn_state(self.btn_analyze,   tk.DISABLED)
        self._set_btn_state(self.btn_translate, tk.DISABLED)
        self._set_btn_state(self.btn_stop,      tk.NORMAL)
        self._set_btn_state(self.btn_pause,     tk.NORMAL)
        self.analyzed_jars.clear()
        self.analyzed_book_texts.clear()
        self.analyzed_book_text_repairs.clear()
        self.analyzed_static_assets.clear()
        self.analyzed_class_texts.clear()
        self.analyzed_loose.clear()
        self.analyzed_loose_base.clear()
        self.analyzed_extra.clear()
        self.analyzed_zip_json.clear()
        self.analyzed_jars_zh_base.clear()
        self._reset_progress_counters()
        self.update_progress(0, 0)
        self._analysis_total_strings = 0
        self._analysis_cache_hits = 0
        self._analysis_missing_strings = 0
        self._set_summary_card("cache", "掃描中", "正在分析檔案...", self.C_WARN)
        self._set_summary_card("pending", "掃描中", "完成後會計算待翻譯數", self.C_WARN)
        self._refresh_output_summary()
        self._refresh_api_summary()
        self.save_config()

        threading.Thread(target=self._analyze_task, args=(mod_dir,), daemon=True).start()

    # 語言檔 fallback 優先順序（en_us 優先，其次英語變體，最後簡中）
    _LANG_FALLBACK_ORDER = [
        'en_us.json', 'en_gb.json', 'en_au.json', 'en_ca.json',
        'en_nz.json', 'en_pt.json', 'zh_cn.json',
        'en_us.lang', 'en_gb.lang', 'en_au.lang', 'en_ca.lang',
        'en_nz.lang', 'en_pt.lang', 'zh_cn.lang',
    ]

    @staticmethod
    def _pick_lang_file(lang_dir_lower, all_lower_to_orig):
        """從 lang/ 目錄中依 fallback 順序挑選最佳來源語言檔。
        lang_dir_lower: 'assets/modid/lang/' (小寫，含尾斜線)
        回傳 (orig_fn, lang_name) 或 (None, None)。"""
        for candidate in ModTranslatorApp._LANG_FALLBACK_ORDER:
            key = lang_dir_lower + candidate
            if key in all_lower_to_orig:
                return all_lower_to_orig[key], candidate.replace('.json', '')
        # 找不到英文系或 zh_cn 來源 → 直接跳過，不從俄/葡/日/韓等語言翻譯。
        # 引擎是 en→zh 取向，拿 ru_ru/pt_br 當來源會產生亂碼（如葡語 EU=「我」→歐盟、
        # 殘留西里爾字母），這正是 kubejs 那些只有 ru_ru/pt_br 檔的目錄被翻壞的主因。
        return None, None

    @staticmethod
    def _is_jar_lang_path(fn_lower):
        if not ((fn_lower.endswith('.json') or fn_lower.endswith('.lang')) and '/lang/' in fn_lower):
            return False
        if fn_lower.startswith('assets/'):
            return True
        # Some modpacks keep quest translation lang files inside a bundled
        # resource pack, e.g. packs/i18n/assets/ftb_translations/lang/en_us.json.
        return fn_lower.startswith('packs/i18n/assets/')

    def _scan_single_jar(self, path):
        return core_scan_single_jar(self, path)

    def _analyze_task(self, mod_dir):
        return run_analyze_task(self, mod_dir)

    def _analyze_task_impl(self, mod_dir):
        return run_analyze_task_impl(self, mod_dir)

    # ═══════════════════════════════════════════════
    #  字串過濾與驗證
    # ═══════════════════════════════════════════════
    def should_translate(self, text):
        if not isinstance(text, str):
            return False
        text = text.strip()
        if len(text) <= 1:
            return False
        if self._RE_PATCHOULI_CONTROL_TOKEN.fullmatch(text):
            return False
        if text.lower() in {'true', 'false', 'null', 'none', 'default'}:
            return False
        if self._RE_NUMBER.match(text):
            return False
        if self._RE_HEX_ID.match(text):
            return False
        if self._looks_like_structural_reference(text):
            return False
        if self._RE_NAMESPACE.match(text):
            return False
        if self._RE_LANG_KEY_REF.match(text):
            return False
        if self._RE_FILEPATH.match(text) and " " not in text:
            return False
        if self._RE_DOTPATH.match(text) and " " not in text:
            return False
        if self._RE_BRACED_LANG_KEY.match(text):
            return False
        if self._RE_SNAKE_KEY.match(text):
            return False
        # 後續語言判斷只看玩家可見文字；Patchouli 巨集、格式碼、佔位符
        # 內部的 registry id / URL 不應讓已翻譯中文被誤判為仍需補翻。
        text_visible = self._RE_FORMAT.sub('', text)
        # 已是中文的字串不再送翻：CJK 比例過半且沒有 4 字母以上英文單詞
        cjk_count = len(self._RE_CJK_CHAR.findall(text_visible))
        if cjk_count:
            visible = [ch for ch in text_visible if not ch.isspace()]
            if visible and cjk_count / len(visible) > 0.5 \
                    and not self._RE_EN_WORD.search(text_visible):
                return False
        if self._RE_COLOR.match(text):
            return False
        if self._RE_ALLCAPS.match(text):
            return False
        if self._RE_NONWORD.match(text):
            return False
        if not self._RE_HASALPHA.search(text):
            return False
        # 模組條件運算式（FTB Quests 格式：or(mod(...))、not(item(...)) 等）不翻譯
        if self._RE_MOD_CONDITION.search(text):
            return False
        # 去除 Minecraft 格式碼後，確認仍有足夠可翻譯文字（至少 2 字元）
        # 防止 " &a"、"§9§l"、"<class>/<title>" 等純格式/樣板字串浪費 API 配額
        # 純網址不翻；Patchouli 的 $(l:https://...) 巨集先視為格式碼移除，
        # 否則整段書本可見文字會因巨集內網址被跳過。
        if '://' in text_visible:
            return False
        # locale 代碼（en_us、zh_tw…）：模組拿來判斷語言，翻了直接壞功能
        if re.fullmatch(r'[a-z]{2,3}_[a-z]{2,3}', text):
            return False
        # 無空格的底線格式模板（SCAN_%s-%s 之類的檔名/ID 模板）不翻
        if ' ' not in text and '_' in text and self._RE_PLACEHOLDER.search(text):
            return False
        # 鍵盤快捷鍵不翻（CTRL + ALT + C、Ctrl + A、Shift + %s…）
        if self._RE_KEY_CHORD.match(text):
            return False
        text_clean = text_visible.strip()
        if len(text_clean) < 2:
            return False
        # 去格式碼後必須還有 ≥2 字母的連續英文（或 CJK）才值得送翻：
        # "§d- X: §6%1$s" 殘留 "- X:"、"X: %s, Y: %s" 這類純座標模板全部跳過
        if not (self._RE_ALPHA_RUN2.search(text_clean)
                or self._RE_CJK_CHAR.search(text_clean)):
            return False
        # 含格式碼/佔位符的模板，去碼後只剩一個單位縮寫
        # （≤3 字母如 HP/DPS/mB/ms/FE，或 mBtl 這種小寫開頭的 camel 單位）
        # → 數值模板不翻，玩家慣用英文縮寫
        if self._RE_FORMAT.search(text) and re.fullmatch(
                r'[^A-Za-z]*(?:[A-Za-z]{1,3}|[a-z]{1,2}[A-Z][A-Za-z]{0,3})[^A-Za-z]*',
                text_clean):
            return False
        # 逐字變色動畫字（§6F§6l§6a§6w…）：每個字母都被色碼隔開，無法翻譯也不該翻
        if text.count('§') + text.count('”') >= 5:
            segments = [p for p in self._RE_FORMAT.split(text) if p]
            visible = [p.strip() for p in segments
                       if p.strip() and not self._RE_FORMAT.fullmatch(p)]
            if visible and all(len(p) <= 1 for p in visible):
                return False
        return True

    def _find_mixed_translations(self, unique_strings):
        """找出「譯文已含中文、但仍殘留 ≥4 字母英文單詞」的快取條目
        （如 Brightsteel 板、Ultimerite 頭盔）——免費引擎保留自創詞的產物，
        AI 引擎可以重翻成完整中文。"""
        mixed = []
        for s in unique_strings:
            if not self._cache_has_usable_translation(s):
                continue
            tr = self.translation_cache.get(s)
            if not isinstance(tr, str) or self._RE_CJK_CHAR.search(s):
                continue
            if not self._RE_CJK_CHAR.search(tr):
                continue   # 純英文殘留交給一般重試，不歸這裡
            # 去掉格式碼後檢查殘留英文單詞
            leftover = self._RE_FORMAT.sub('', tr)
            if self._RE_EN_WORD.search(leftover):
                mixed.append(s)
        return mixed

    def _retranslate_mixed(self, mixed):
        """用目前（AI）引擎重翻混英條目；任何一筆失敗就還原原譯文，
        只會更好、不會更差。回傳改善筆數。"""
        mixed = _bounded_post_translation_engine_items(self, mixed)
        if not mixed:
            return 0
        backup = {s: self.translation_cache[s] for s in mixed if s in self.translation_cache}
        for s in mixed:
            self.translation_cache.pop(s, None)
        self.batch_translate_missing(list(mixed))
        improved = 0
        for s in mixed:
            new = self.translation_cache.get(s)
            if not new:
                self.translation_cache[s] = backup[s]      # 失敗 → 還原混英版
                continue
            leftover = self._RE_FORMAT.sub('', new)
            if self._RE_EN_WORD.search(leftover) and not self._RE_EN_WORD.search(
                    self._RE_FORMAT.sub('', backup[s])):
                self.translation_cache[s] = backup[s]      # 沒有比較好 → 還原
            elif new != backup[s]:
                improved += 1
        self.log(f"✨ 混英補翻：{improved}/{len(mixed)} 筆譯文升級為完整中文")
        return improved

    _RE_MIXED_PHRASE = re.compile(r"[A-Za-z][A-Za-z0-9'\- ]{2,40}[A-Za-z0-9]")

    def _upgrade_mixed_phrases(self, mixed):
        """非 AI 鏈的混英升級。成因：自創詞嵌在長句中會被免費引擎保留；
        但實測「單獨送翻」翻得動（Botania→博塔尼亞、Terrasteel Sword→泰鋼劍）。
        做法：把混英譯文裡殘留的英文片語抽出、單獨送鏈上翻譯、逐一替換回去。
        格式碼/佔位符按構造完全不經手，整條結果仍須通過驗證才入快取。"""
        plans = {}
        phrases = set()
        for s in mixed:
            tr = self.translation_cache.get(s)
            if not isinstance(tr, str):
                continue
            found = []
            for is_code, part in self._split_by_format_tokens(tr):
                if is_code:
                    continue
                for m in self._RE_MIXED_PHRASE.finditer(part):
                    ph = m.group(0).strip()
                    if self._RE_EN_WORD.search(ph) and self.should_translate(ph):
                        found.append(ph)
            if found:
                plans[s] = (tr, found)
                phrases.update(found)
        if not plans:
            return 0
        need = [p for p in sorted(phrases)
                if not self._cache_has_usable_translation(p) and self.get_translation(p) == p]
        need = _bounded_post_translation_engine_items(self, need)
        if need:
            self.log(f"   抽出 {len(phrases)} 個英文片語，其中 {len(need)} 個送翻")
            self.batch_translate_missing(need)
        upgraded = 0
        for s, (tr, found) in plans.items():
            # 只在「純文字段」內替換；格式碼/巨集（§、%s、$(...)）按構造原樣拼回，
            # 不可能被字界誤匹配（如 $(thing) 裡的 thing）。
            parts = self._split_by_format_tokens(tr)
            for ph in sorted(set(found), key=len, reverse=True):
                zh = self.get_translation(ph)
                # 片語必須翻成「全中文」才替換，保證只升不降
                if (zh and zh != ph and self._RE_CJK_CHAR.search(zh)
                        and not self._RE_EN_WORD.search(zh)):
                    pat = re.compile(
                        r"(?<![A-Za-z0-9])" + re.escape(ph) + r"(?![A-Za-z0-9])")
                    repl = zh.replace("\\", "\\\\")
                    parts = [(is_code, part if is_code else pat.sub(repl, part))
                             for is_code, part in parts]
            new_tr = ''.join(part for _, part in parts)
            if new_tr == tr:
                continue
            validated = self.validate_translation(s, new_tr)
            if validated != s and self._RE_CJK_CHAR.search(validated):
                self.translation_cache[s] = validated
                upgraded += 1
        self.log(f"✨ 混英片語升級：{upgraded}/{len(plans)} 筆（非 AI 鏈）")
        return upgraded

    @classmethod
    def _split_by_format_tokens(cls, text):
        """把字串切成 [(是否為格式碼, 片段)]，格式碼一字不動。"""
        parts = []
        pos = 0
        for m in cls._RE_FORMAT.finditer(text):
            if m.start() > pos:
                parts.append((False, text[pos:m.start()]))
            parts.append((True, m.group(0)))
            pos = m.end()
        if pos < len(text):
            parts.append((False, text[pos:]))
        return parts

    def _retry_segment_mode(self, heavy_strings):
        """格式碼密集字串的救援：只把「格式碼之間的純文字段」送翻，
        格式碼/佔位符原樣保留在原位。專有名詞段（如 Sylvi）翻不動就保留英文，
        與整合包既有的混排風格一致。回傳救回的字串數。"""
        plans = {}
        seg_need = set()
        for s in heavy_strings:
            parts = self._split_by_format_tokens(s)
            texts = [t.strip() for is_code, t in parts
                     if not is_code and self._RE_EN_WORD.search(t)]
            if not texts:
                continue
            plans[s] = parts
            for t in texts:
                if not self._cache_has_usable_translation(t) and self.should_translate(t):
                    seg_need.add(t)
        if not plans:
            return 0
        seg_need = _bounded_post_translation_engine_items(self, seg_need)
        if seg_need:
            self.log(f"   切出 {len(seg_need)} 個純文字段送翻（格式碼不經過引擎）")
            self.batch_translate_missing(seg_need)
        rescued = 0
        for s, parts in plans.items():
            out = []
            improved = False
            for is_code, t in parts:
                if is_code:
                    out.append(t)
                    continue
                core = t.strip()
                if not core or not self._RE_EN_WORD.search(core):
                    out.append(t)
                    continue
                tr = self.get_translation(core)
                if tr and tr.strip() and tr != core:
                    lead = t[:len(t) - len(t.lstrip())]
                    trail = t[len(t.rstrip()):]
                    out.append(lead + tr.strip() + trail)
                    improved = True
                else:
                    out.append(t)
            if not improved:
                continue
            assembled = ''.join(out)
            validated = self.validate_translation(s, assembled)
            if validated != s and validated.strip():
                self.translation_cache[s] = validated
                rescued += 1
        self.log(f"🧩 分段翻譯救回 {rescued}/{len(plans)} 筆")
        return rescued

    def fix_placeholders(self, text):
        return core_fix_placeholders(text)

    @staticmethod
    def _repair_patchouli_macros(text):
        return core_repair_patchouli_macros(text)

    @staticmethod
    def _mask_format(text):
        """將格式符號替換成唯一佔位符，回傳 (masked_text, mapping)"""
        return core_mask_format(text, ModTranslatorApp._RE_FORMAT)

    @staticmethod
    def _unmask_format(text, mapping):
        """將翻譯結果中的佔位符還原為原始格式符號（容錯空白）"""
        return core_unmask_format(text, mapping)

    @staticmethod
    def _clean_json_text(text):
        """清理非標準 JSON 文字（移除注解、尾逗號、控制字元）。"""
        return core_clean_json_text(text)

    def validate_translation(self, orig, trans):
        if not isinstance(trans, str):
            return orig
        if self._looks_like_structural_reference(orig):
            return orig
        # 自動將簡體字元轉為繁體，確保快取內容一律為繁體中文
        trans = self._to_traditional(trans)
        # Placeholder 驗證必須「雙向 + 內容相符」（不只數量）：
        # - 原文 0 個、譯文多出 %s → TranslatableFormatException 渲染時崩潰
        # - %s 被換成 %d → IllegalFormatConversionException
        # 用 Counter 比對種類與數量，順序變化（%1$s 重排）仍合法
        if (Counter(self._RE_PLACEHOLDER.findall(orig))
                != Counter(self._RE_PLACEHOLDER.findall(trans))):
            return orig
        # 格式代碼數量驗證：§a / &l 等代碼數量必須完全相符，防止遊戲崩潰
        # （與快取層、驗證層統一用 critical tokens，排除羅馬數字等裝飾性 token）
        orig_fmts = self._critical_format_tokens(orig)
        if orig_fmts:
            trans_fmts = self._critical_format_tokens(trans)
            if Counter(orig_fmts) != Counter(trans_fmts):
                return orig
        if len(orig) > 0 and len(trans) > len(orig) * 5:
            return orig
        return trans

    # ═══════════════════════════════════════════════
    #  字串收集
    # ═══════════════════════════════════════════════
    def _collect_strings_json(self, data, string_set, preserve_technical_keys=False, strict_context=False,
                              _text_parent=False):
        # _text_parent 規則與 process_json_data 一致：strict 模式下 list 內裸字串
        # 只有所屬 key 是文字欄位才收集，避免把 advancement requirements 等技術字串送翻
        if isinstance(data, dict):
            for k, v in data.items():
                if preserve_technical_keys and self._is_technical_data_key(k):
                    continue
                if (strict_context and isinstance(v, str)
                        and not self._is_strict_text_key(k)
                        and not (_text_parent
                                 and str(k).lower() == "translate"
                                 and self._component_translate_value_is_literal(v))):
                    continue
                self._collect_strings_json(v, string_set, preserve_technical_keys, strict_context,
                                           _text_parent=self._is_strict_text_key(k))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and strict_context and not _text_parent:
                    continue
                self._collect_strings_json(item, string_set, preserve_technical_keys, strict_context,
                                           _text_parent)
        elif isinstance(data, str) and self.should_translate(data):
            # JSON 文字元件值只收集其 text 片段（整句收集會把 JSON 結構送翻）
            component = self._json_text_component_obj(data)
            if component is not None:
                for fragment in self._walk_json_text_values(component):
                    if self.should_translate(fragment):
                        string_set.add(fragment)
            else:
                string_set.add(data)

    def _collect_untranslated_visible_json_strings(
            self, source_data, output_data, preserve_technical_keys=False,
            strict_context=False, _text_parent=False):
        """Return source strings whose translated structured JSON still renders
        as English/untranslated.

        This mirrors _collect_strings_json/process_json_data so the output stage
        can safely retry Patchouli/book JSON without touching structural IDs
        such as category/type/item/resource locations.
        """
        missing = []

        def add_if_needed(source, translated):
            if not isinstance(source, str) or not self.should_translate(source):
                return
            component = self._json_text_component_obj(source)
            if component is not None:
                output_text = translated if isinstance(translated, str) else ""
                output_component = self._json_text_component_obj(output_text)
                output_values = list(self._walk_json_text_values(output_component)) if output_component is not None else [output_text]
                output_joined = "\n".join(v for v in output_values if isinstance(v, str))
                for fragment in self._walk_json_text_values(component):
                    if (isinstance(fragment, str) and self.should_translate(fragment)
                            and (fragment in output_joined
                                 or self._lang_value_needs_update(fragment, output_joined))):
                        missing.append(fragment)
                return
            if (not isinstance(translated, str)
                    or self._lang_value_needs_update(source, translated)):
                missing.append(source)

        if isinstance(source_data, dict):
            output_dict = output_data if isinstance(output_data, dict) else {}
            for key, value in source_data.items():
                if preserve_technical_keys and self._is_technical_data_key(key):
                    continue
                key_is_text = self._is_strict_text_key(key)
                if (strict_context and isinstance(value, str)
                        and not key_is_text
                        and not (_text_parent
                                 and str(key).lower() == "translate"
                                 and self._component_translate_value_is_literal(value))):
                    continue
                missing.extend(self._collect_untranslated_visible_json_strings(
                    value, output_dict.get(key), preserve_technical_keys,
                    strict_context, _text_parent=key_is_text))
        elif isinstance(source_data, list):
            output_list = output_data if isinstance(output_data, list) else []
            for idx, item in enumerate(source_data):
                if isinstance(item, str) and strict_context and not _text_parent:
                    continue
                translated_item = output_list[idx] if idx < len(output_list) else None
                missing.extend(self._collect_untranslated_visible_json_strings(
                    item, translated_item, preserve_technical_keys,
                    strict_context, _text_parent))
        elif isinstance(source_data, str):
            add_if_needed(source_data, output_data)

        return missing

    def _repair_structured_book_json_output(
            self, source_data, zh_base, merged_data, process_book_data,
            path_label=""):
        """Report untranslated book fields without doing network I/O.

        Translation and retries finish before packaging. Retrying here once per
        JSON made archive generation non-deterministic and could stall for a
        provider timeout after the progress bar had already reached 100%.
        """
        missing = self._collect_untranslated_visible_json_strings(
            source_data, merged_data, strict_context=True)
        missing = sorted(set(missing))
        if not missing or self.stop_requested:
            return merged_data

        label = f" {path_label}" if path_label else ""
        self.log(
            f"  ⚠️{label}: 手冊 JSON 尚有 {len(missing):,} 個可見欄位未翻；"
            "打包階段不發送網路翻譯，已保留原值並交由失敗報告追蹤")
        return merged_data

    def _book_text_translatable_paragraphs(self, content):
        """產出書本 txt「會被輸出端查找」的精確段落字串——
        與 process_book_text_content 的重組邏輯完全相同（唯一真相來源）。
        收集端必須用這個：舊版收集按『單行』切、輸出按『段落』合併，
        兩套 key 對不上 → 段落永遠不在快取 → 書本永遠輸出英文。"""
        if not isinstance(content, str):
            return []
        paragraphs = []
        normalized = self._normalize_inline_book_markers(content)
        for part in re.split(r'(<NEWLINE>)', normalized):
            if not part or part == '<NEWLINE>':
                continue
            para = self._normalize_book_text_block(part)
            if para and not self._has_cjk_text(para) and self.should_translate(para):
                paragraphs.append(para)
        return paragraphs

    def _book_text_has_effective_translation(self, source_content, translated_content):
        """Avoid writing an English custom-book TXT file into zh_tw output.

        Book TXT output is generated after stage 2 from the translation cache. If
        a paragraph never entered the cache, process_book_text_content would
        otherwise return the English source and make the resource pack look
        complete while the in-game book still stays untranslated.
        """
        source_paragraphs = self._book_text_translatable_paragraphs(source_content)
        if not source_paragraphs:
            return True
        if not isinstance(translated_content, str) or not translated_content.strip():
            return False
        if not self._has_cjk_text(translated_content):
            return False
        unchanged = sum(1 for paragraph in source_paragraphs
                        if paragraph and paragraph in translated_content)
        return unchanged < len(source_paragraphs)

    @staticmethod
    def _book_text_display_width(text):
        width = 0.0
        for ch in text:
            code = ord(ch)
            if ch in "\t\r\n":
                width += 1.0
            elif code < 128:
                width += 0.55
            elif 0xFF61 <= code <= 0xFF9F:
                width += 0.6
            else:
                width += 1.0
        return width

    def _wrap_book_text_line(self, text, max_width=13.0):
        """Wrap CJK-heavy custom book TXT lines so they stay inside the page."""
        if not isinstance(text, str) or self._book_text_display_width(text) <= max_width:
            return text
        lines = []
        buf = ""
        width = 0.0
        break_after = set("，。！？；：、,.!?;:)]}）】」』")
        break_before = set("([{（【「『")
        for ch in text:
            ch_width = self._book_text_display_width(ch)
            if buf and width + ch_width > max_width and ch not in break_after:
                lines.append(buf.rstrip())
                buf = ""
                width = 0.0
            buf += ch
            width += ch_width
            if width >= max_width and ch in break_after:
                lines.append(buf.rstrip())
                buf = ""
                width = 0.0
            elif buf and buf[-1] in break_before and width > max_width - 2:
                lines.append(buf.rstrip())
                buf = ""
                width = 0.0
        if buf:
            lines.append(buf.rstrip())
        return "\r\n".join(line for line in lines if line)

    @staticmethod
    def _paginate_book_text_block(text, max_lines=8):
        """Split custom book TXT blocks into safe page-sized chunks."""
        if not isinstance(text, str) or not text:
            return text
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        pages = []
        for i in range(0, len(lines), max_lines):
            page = "\r\n".join(line for line in lines[i:i + max_lines] if line.strip())
            if page:
                pages.append(page)
        return "<NEWLINE>\r\n".join(pages)

    @staticmethod
    def _book_text_tokens(content):
        """Split custom book TXT content while preserving <NEWLINE> page controls."""
        if not isinstance(content, str):
            return []
        tokens = []
        parts = re.split(r'(<NEWLINE>)', content.replace('\r\n', '\n').replace('\r', '\n'))
        for part in parts:
            if not part:
                continue
            if part == "<NEWLINE>":
                tokens.append(("marker", part))
                continue
            for line in part.split('\n'):
                if line.strip():
                    tokens.append(("text", line))
        return tokens

    @staticmethod
    def _join_book_text_lines(lines):
        """Keep book page separators as standalone lines so mods do not render them."""
        cleaned = []
        previous_marker = False
        for raw in lines:
            line = str(raw).strip()
            if not line:
                continue
            if line == "<NEWLINE>":
                if not previous_marker:
                    cleaned.append(line)
                else:
                    cleaned.append(line)
                previous_marker = True
                continue
            cleaned.append(line)
            previous_marker = False
        return "\r\n".join(cleaned)

    @classmethod
    def _normalize_book_text_block(cls, block):
        """Merge previously wrapped book TXT lines back into a paragraph before reflow."""
        if not isinstance(block, str):
            return ""
        lines = [line.strip() for line in block.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                 if line.strip()]
        if not lines:
            return ""
        has_cjk = any(cls._has_cjk_text(line) for line in lines)
        if has_cjk:
            return "".join(lines)
        return " ".join(lines)

    @staticmethod
    def _has_cjk_text(text):
        return core_has_cjk_text(text)

    @staticmethod
    def _read_u2(data, offset):
        return int.from_bytes(data[offset:offset + 2], 'big'), offset + 2

    @staticmethod
    def _read_u4(data, offset):
        return int.from_bytes(data[offset:offset + 4], 'big'), offset + 4

    @classmethod
    def _decode_mutf8(cls, raw):
        return core_decode_mutf8(raw)

    @classmethod
    def _class_utf8_entries(cls, data):
        return core_class_utf8_entries(data)

    @classmethod
    def _is_hardcoded_lore_string(cls, text):
        return core_is_hardcoded_lore_string(text)

    @staticmethod
    def _mutf8_encode(text):
        return core_mutf8_encode(text)

    def _patch_class_hardcoded_strings(self, data, replacements):
        return core_patch_class_hardcoded_strings(data, replacements)

    def extract_all_unique_strings(self):
        return core_extract_all_unique_strings(self)

    # ═══════════════════════════════════════════════
    #  批次翻譯（萬能引擎池：任何引擎限流均可自動切換）
    # ═══════════════════════════════════════════════
    @staticmethod
    def _estimate_text_cost(text):
        """估算單筆翻譯成本，用字元長度近似 token 與格式碼複雜度。"""
        if not isinstance(text, str):
            return 0
        return len(text) + sum(1 for _ in ModTranslatorApp._RE_FORMAT.finditer(text)) * 12

    def _translation_batch_limits(self, engine, primary_id, ai_model):
        model = (ai_model or "").lower()
        if engine in ('claude',):
            return 80, 18000
        if engine in ('openai', 'market_ai', 'local'):
            if any(k in model for k in ('claude', 'sonnet', 'opus')):
                return 80, 18000
            if any(k in model for k in ('deepseek', 'kimi', 'moonshot', 'grok', 'qwen')):
                return 40, 9000
            if any(k in model for k in ('gpt-5', 'gpt-4', 'o4', 'o3')):
                return 50, 11000
            return 32, 7500
        if primary_id == 'gtx':
            # 大批次：同樣 QPS 下更高詞/秒，比狂加請求更不易觸發限流
            return 128, 9200
        if primary_id == 'bing':
            # Bing 端點可穩定吃較大的批次；小批次會產生太多請求，
            # 在第二階段救援時容易把吞吐拉低到 20~40 詞/秒。
            return 320, 48000
        if primary_id in ('azure', 'deepl', 'google_api'):
            return 220, 32000
        return 45, 6500

    def _make_translation_chunks(self, items, engine, primary_id, ai_model,
                                 forced_size=None):
        if forced_size:
            return [items[i:i + forced_size] for i in range(0, len(items), forced_size)]
        max_items, max_cost = self._translation_batch_limits(engine, primary_id, ai_model)
        chunks = []
        chunk = []
        cost = 0
        for item in items:
            item_cost = max(1, self._estimate_text_cost(item))
            if chunk and (len(chunk) >= max_items or cost + item_cost > max_cost):
                chunks.append(chunk)
                chunk = []
                cost = 0
            chunk.append(item)
            cost += item_cost
        if chunk:
            chunks.append(chunk)
        return chunks

    def batch_translate_missing(self, missing_strings, _force_chunk_size=None,
                                _preferred_engine=None):
        return core_batch_translate_missing(
            self, missing_strings, _force_chunk_size, _preferred_engine)

    # ═══════════════════════════════════════════════
    #  翻譯查詢與套用
    # ═══════════════════════════════════════════════
    def _cache_has_usable_translation(self, text):
        """本輪是否可使用快取中的翻譯。

        強制重翻時不刪 shelve 快取，而是把分析出的字串列為本輪忽略。
        只有本輪新翻譯成功並加入 _session_translated_keys 後，才視為可用。
        """
        if not isinstance(text, str):
            return False
        # force 模式下仍允許快取 fallback：翻譯引擎未翻到的字串，
        # 輸出時會從快取取回，避免產生未翻譯的輸出
        translated = self.translation_cache.get(text)
        return self._is_valid_trad_translation(text, translated)

    def get_translation(self, text):
        use_memory = self.global_memory_var.get()
        memory = self._load_translation_memory() if use_memory else None
        ignored = getattr(self, "_force_ignore_cache_strings", None)
        session_keys = getattr(self, "_session_translated_keys", set())
        for candidate in self._translation_lookup_candidates(text):
            hit = None
            # force 模式下：如果本輪已翻譯（在 session_keys），直接用新翻譯。
            # 如果本輪未翻到（不在 session_keys），仍允許從快取 fallback，
            # 避免翻譯引擎未翻到的字串變成未翻譯的原文輸出。
            if candidate in session_keys:
                hit = self.translation_cache.get(candidate)
            elif not ignored or candidate not in ignored:
                hit = self.translation_cache.get(candidate)
            else:
                # force 模式下未翻譯的字串，也允許快取 fallback
                hit = self.translation_cache.get(candidate)
            # 記憶池同樣允許 fallback
            if not hit and memory is not None:
                hit = memory.get(candidate)
            if not hit:
                continue
            hit = self._restyle_swapped_hit(text, candidate, hit)
            hit = self.validate_translation(text, hit)
            return sanitize_text(self._rewrap_quoted_translation(text, candidate, hit))
        return text

    @staticmethod
    def _restyle_swapped_hit(source, candidate, translated):
        """只有當命中的是 §/” 互換產生的「變體候選」時，才把譯文的格式碼
        風格轉回原文方向。直接命中不做任何改寫——否則譯文裡的合法全形引號
        （如 按”attack”鍵）會被誤改成 §a 顏色碼。"""
        if not isinstance(translated, str) or candidate == source:
            return translated
        if '§' in source and candidate == source.replace('§', '”'):
            return re.sub(r'”(?=[0-9a-fk-orx])', '§', translated)
        if '”' in source and candidate == source.replace('”', '§'):
            return re.sub(r'§(?=[0-9a-fk-orx])', '”', translated)
        return translated

    @staticmethod
    def _decode_snbt_string(raw_text):
        """Decode the inside of a SNBT/JSON-style quoted string."""
        if not isinstance(raw_text, str):
            return raw_text
        try:
            return json.loads(f'"{raw_text}"')
        except (json.JSONDecodeError, TypeError):
            # SNBT files sometimes contain non-JSON escapes such as \'.
            safe_text = raw_text.replace("\\'", "'")
            try:
                return json.loads(f'"{safe_text}"')
            except (json.JSONDecodeError, TypeError):
                return safe_text

    @staticmethod
    def _unwrap_outer_quotes(text):
        if not isinstance(text, str) or len(text) < 2:
            return None
        quote_pairs = {
            '"': '"',
            "'": "'",
            "“": "”",
            "「": "」",
            "『": "』",
        }
        end_quote = quote_pairs.get(text[0])
        if end_quote and text.endswith(end_quote):
            return text[1:-1], text[0], end_quote
        return None

    def _translation_lookup_candidates(self, text):
        candidates = []
        seen = set()

        def add(value):
            if isinstance(value, str) and value not in seen:
                seen.add(value)
                candidates.append(value)

        add(text)
        add(self._decode_snbt_string(text))
        for value in list(candidates):
            unwrapped = self._unwrap_outer_quotes(value)
            if unwrapped:
                add(unwrapped[0])
        for value in list(candidates):
            if not isinstance(value, str):
                continue
            # Some class constants surface Minecraft formatting codes as §,
            # while decompiled/logged strings can appear as ”. Treat both as
            # equivalent for cache/dictionary lookup, but keep the original
            # string as the replacement key when patching class files.
            if "§" in value:
                add(value.replace("§", "”"))
            if "”" in value:
                add(value.replace("”", "§"))
        return candidates

    @classmethod
    def _component_translate_value_is_literal(cls, value):
        """Minecraft text component 的 translate 通常是 lang key。
        只有整合包把英文句子誤放在 translate 欄位時才視為可翻譯文字。"""
        if not isinstance(value, str):
            return False
        text = cls._RE_FORMAT.sub('', value).strip()
        if not text:
            return False
        # Modded advancement keys often contain camelCase after the namespace,
        # e.g. advancement.enigmaticlegacy:discoverSpellstone. They are lang
        # references, not player-facing English, regardless of letter case.
        if re.fullmatch(r'[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+', text):
            return False
        if cls._RE_NAMESPACE.match(text):
            return False
        if cls._RE_LANG_KEY_REF.match(text):
            return False
        if cls._RE_FILEPATH.match(text) and " " not in text:
            return False
        if cls._RE_DOTPATH.match(text) and " " not in text:
            return False
        if cls._RE_SNAKE_KEY.match(text):
            return False
        return bool(cls._RE_ALPHA_RUN2.search(text) or cls._RE_CJK_CHAR.search(text))

    @classmethod
    def _json_text_component_obj(cls, text):
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not (stripped.startswith("{") or stripped.startswith("[")):
            return None
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return None
        def has_text_node(node):
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    return True
                if cls._component_translate_value_is_literal(node.get("translate")):
                    return True
                return any(has_text_node(v) for v in node.values())
            if isinstance(node, list):
                return any(has_text_node(v) for v in node)
            return False
        return obj if has_text_node(obj) else None

    @classmethod
    def _walk_json_text_values(cls, node):
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                yield text
            translate = node.get("translate")
            if cls._component_translate_value_is_literal(translate):
                yield translate
            for value in node.values():
                yield from cls._walk_json_text_values(value)
        elif isinstance(node, list):
            for value in node:
                yield from cls._walk_json_text_values(value)

    def _translate_json_text_component_obj(self, node):
        if isinstance(node, dict):
            new_node = dict(node)
            text = new_node.get("text")
            if isinstance(text, str) and self.should_translate(text):
                new_node["text"] = self.get_translation(text)
            translate = new_node.get("translate")
            if (self._component_translate_value_is_literal(translate)
                    and self.should_translate(translate)):
                new_node["translate"] = self.get_translation(translate)
            for key, value in list(new_node.items()):
                if key not in ("text", "translate"):
                    new_node[key] = self._translate_json_text_component_obj(value)
            return new_node
        if isinstance(node, list):
            return [self._translate_json_text_component_obj(value) for value in node]
        return node

    def _translate_json_text_component_string(self, text):
        obj = self._json_text_component_obj(text)
        if obj is None:
            return None
        translated = self._translate_json_text_component_obj(obj)
        return json.dumps(translated, ensure_ascii=False, separators=(",", ":"))

    def _rewrap_quoted_translation(self, original, matched_candidate, translated):
        if not isinstance(translated, str):
            return translated
        unwrapped = self._unwrap_outer_quotes(original)
        if unwrapped and matched_candidate == unwrapped[0]:
            _, left, right = unwrapped
            return f"{left}{translated}{right}"
        decoded = self._decode_snbt_string(original)
        decoded_unwrapped = self._unwrap_outer_quotes(decoded)
        if decoded_unwrapped and matched_candidate == decoded_unwrapped[0]:
            _, left, right = decoded_unwrapped
            return f"{left}{translated}{right}"
        return translated

    @classmethod
    def _is_technical_data_key(cls, key):
        return isinstance(key, str) and key.lower() in cls._SNBT_NO_TRANS_KEYS

    @classmethod
    def _is_strict_text_key(cls, key):
        if not isinstance(key, str):
            return False
        k = key.strip().lower()
        if not k:
            return False
        if k in cls._SNBT_NO_TRANS_KEYS:
            return False
        if k in cls._STRICT_TEXT_KEYS:
            return True
        return any(k.endswith(suffix) for suffix in cls._STRICT_TEXT_SUFFIXES)

    @classmethod
    def _repair_ftbq_type_value(cls, key, value):
        if isinstance(key, str) and key.lower() == "type" and isinstance(value, str):
            return cls._FTBQ_TYPE_REPAIR.get(value.strip(), value)
        return value

    def _load_official_minecraft_zh_base(self, mc_dir, path_in_jar):
        """Load the local Mojang zh_tw asset for the vanilla minecraft lang file."""
        norm_path = str(path_in_jar).replace('\\', '/').lower()
        if norm_path != 'assets/minecraft/lang/en_us.json':
            return {}

        cache_key = (os.path.abspath(mc_dir), norm_path)
        cache = getattr(self, "_official_lang_base_cache", None)
        if cache is None:
            cache = {}
            self._official_lang_base_cache = cache
        if cache_key in cache:
            return cache[cache_key]

        result = {}
        try:
            version_json = os.path.join(mc_dir, os.path.basename(mc_dir) + '.json')
            if not os.path.exists(version_json):
                for name in os.listdir(mc_dir):
                    candidate = os.path.join(mc_dir, name)
                    if not name.lower().endswith('.json') or not os.path.isfile(candidate):
                        continue
                    with open(candidate, 'r', encoding='utf-8') as f:
                        if json.load(f).get('assetIndex'):
                            version_json = candidate
                            break
            with open(version_json, 'r', encoding='utf-8') as f:
                version_data = json.load(f)
            asset_id = version_data.get('assetIndex', {}).get('id')
            if not asset_id:
                cache[cache_key] = result
                return result

            cur = os.path.abspath(mc_dir)
            mc_root = None
            while True:
                if os.path.basename(cur).lower() == '.minecraft':
                    mc_root = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
            if not mc_root and os.path.basename(os.path.dirname(mc_dir)).lower() == 'versions':
                mc_root = os.path.dirname(os.path.dirname(mc_dir))
            if not mc_root:
                cache[cache_key] = result
                return result

            index_path = os.path.join(mc_root, 'assets', 'indexes', asset_id + '.json')
            with open(index_path, 'r', encoding='utf-8') as f:
                index_data = json.load(f)
            obj = index_data.get('objects', {}).get('minecraft/lang/zh_tw.json')
            if not obj or not obj.get('hash'):
                cache[cache_key] = result
                return result
            h = obj['hash']
            asset_path = os.path.join(mc_root, 'assets', 'objects', h[:2], h)
            with open(asset_path, 'r', encoding='utf-8') as f:
                official = json.load(f)
            if isinstance(official, dict):
                result = official
        except Exception:
            result = {}

        cache[cache_key] = result
        return result

    @staticmethod
    def _combine_lang_bases(source_data, official_base, existing_base):
        merged = dict(official_base or {})
        if not isinstance(existing_base, dict):
            return merged
        for key, value in existing_base.items():
            source_value = source_data.get(key) if isinstance(source_data, dict) else None
            if isinstance(source_value, str) and isinstance(value, str):
                if not value.strip() or value == source_value or value == key:
                    continue
            merged[key] = value
        return merged

    def _filter_lang_output_entries(self, source_data, output_data):
        cleaned, dropped = drop_untranslated_lang_entries(source_data, output_data)
        if not isinstance(source_data, dict) or not isinstance(cleaned, dict):
            return cleaned, dropped, 0
        filtered = {}
        format_dropped = 0
        for key, value in cleaned.items():
            source_value = source_data.get(key)
            if isinstance(source_value, str) and isinstance(value, str):
                src_fmts = self._critical_format_tokens(source_value)
                val_fmts = self._critical_format_tokens(value)
                if Counter(src_fmts) != Counter(val_fmts):
                    format_dropped += 1
                    continue
                # placeholder 種類與數量必須完全一致（雙向），這是執行期崩潰的最後防線
                if (Counter(self._RE_PLACEHOLDER.findall(source_value))
                        != Counter(self._RE_PLACEHOLDER.findall(value))):
                    format_dropped += 1
                    continue
            filtered[key] = value
        return filtered, dropped + format_dropped, format_dropped

    @classmethod
    def _critical_format_tokens(cls, text):
        return [
            token for token in cls._RE_FORMAT.findall(text)
            if not cls._RE_ROMAN_TOKEN.fullmatch(token)
        ]

    def process_json_data(self, data, preserve_technical_keys=False, strict_context=False,
                          _text_parent=False):
        # _text_parent：strict 模式下，list 內的裸字串只有在「所屬 key 是文字欄位」時才翻譯。
        # 否則 advancement 的 requirements（criterion 名稱陣列）等技術字串會被翻成中文，
        # 導致 "Unknown required criterion" → 進度全數載入失敗 → 遊戲崩潰。
        if isinstance(data, dict):
            new_data = {}
            for k, v in data.items():
                if self.stop_requested:
                    break
                if preserve_technical_keys and self._is_technical_data_key(k):
                    new_data[k] = self._repair_ftbq_type_value(k, v)
                    continue
                if (strict_context and isinstance(v, str)
                        and not self._is_strict_text_key(k)
                        and not (_text_parent
                                 and str(k).lower() == "translate"
                                 and self._component_translate_value_is_literal(v))):
                    new_data[k] = self._repair_ftbq_type_value(k, v)
                elif isinstance(v, str) and self.should_translate(v):
                    # lang 值若本身是 JSON 文字元件（如 ironfurnaces 更新訊息），
                    # 整句送翻會弄壞 JSON 結構 → 改用元件級翻譯（只動 text 欄位）
                    component = self._translate_json_text_component_string(v)
                    new_data[k] = component if component is not None else self.get_translation(v)
                elif isinstance(v, str) and self._has_cjk_text(v):
                    # 已是中文（常見於僅有 zh_cn 的模組）：簡→繁，勿原樣留下再被 drop
                    new_data[k] = self._to_traditional(v)
                elif isinstance(v, (dict, list)):
                    new_data[k] = self.process_json_data(
                        v, preserve_technical_keys, strict_context,
                        _text_parent=self._is_strict_text_key(k))
                else:
                    new_data[k] = v
            return new_data
        elif isinstance(data, list):
            new_data = []
            for item in data:
                if self.stop_requested:
                    break
                if isinstance(item, str):
                    if ((not strict_context or _text_parent)
                            and self.should_translate(item)):
                        new_data.append(self.get_translation(item))
                    elif ((not strict_context or _text_parent)
                            and self._has_cjk_text(item)):
                        new_data.append(self._to_traditional(item))
                    else:
                        new_data.append(item)
                elif isinstance(item, (dict, list)):
                    new_data.append(self.process_json_data(
                        item, preserve_technical_keys, strict_context, _text_parent))
                else:
                    new_data.append(item)
            return new_data
        return data

    def process_origin_json_display_fields(self, data):
        """Origins datapack JSON: only top-level name/description are player-facing.
        Nested modifier.name fields are technical labels and should stay unchanged."""
        if not isinstance(data, dict):
            return data
        new_data = dict(data)
        for key in ("name", "description"):
            value = new_data.get(key)
            if isinstance(value, str) and self.should_translate(value):
                new_data[key] = self.get_translation(value)
        return new_data

    def _collect_mmorpg_json_display_strings(self, data, string_set):
        """Mine and Slash datapack JSON: collect only safe player-facing fields."""
        if isinstance(data, dict):
            for key, value in data.items():
                key_l = str(key).lower()
                if key_l in ("loc_name", "flavor_text") and isinstance(value, str):
                    if self.should_translate(value):
                        string_set.add(value)
                    continue
                if isinstance(value, (dict, list)):
                    self._collect_mmorpg_json_display_strings(value, string_set)
        elif isinstance(data, list):
            for item in data:
                self._collect_mmorpg_json_display_strings(item, string_set)

    def process_mmorpg_json_display_fields(self, data):
        """Translate Mine and Slash display labels without touching gameplay IDs."""
        if isinstance(data, dict):
            new_data = {}
            for key, value in data.items():
                key_l = str(key).lower()
                if key_l in ("loc_name", "flavor_text") and isinstance(value, str):
                    if self.should_translate(value):
                        new_data[key] = self.get_translation(value)
                    elif self._has_cjk_text(value):
                        new_data[key] = self._to_traditional(value)
                    else:
                        new_data[key] = value
                elif isinstance(value, (dict, list)):
                    new_data[key] = self.process_mmorpg_json_display_fields(value)
                else:
                    new_data[key] = value
            return new_data
        if isinstance(data, list):
            return [self.process_mmorpg_json_display_fields(value) for value in data]
        return data

    @staticmethod
    def _normalize_inline_book_markers(content):
        if not isinstance(content, str):
            return content
        text = content.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'(?<!\A)(?<!\n)<NEWLINE>', '', text)
        text = re.sub(r'<NEWLINE>(?!\n|$)', '', text)
        return text

    @staticmethod
    def _book_text_leading_markers(path_in_jar, content):
        path = (path_in_jar or "").replace("\\", "/").lower()
        if "/assets/alexsmobs/book/animal_dictionary/" not in "/" + path:
            return 0
        if re.match(r'^\s*(?:<NEWLINE>\s*)+', content or ""):
            return 0
        name = path.rsplit("/", 1)[-1]
        return 4 if name == "moose.txt" else 3

    def _translate_text_component(self, value):
        """就地翻譯 JSON 文字元件的 'text' 字面值，支援
        字串 / {'text':...,'extra':[...]} / 元件陣列三種形態。
        'translate' 預設視為 client 語言 key；只有值本身是英文句子/標題時才翻譯。
        回傳翻譯後的值。"""
        if isinstance(value, str):
            if self.should_translate(value):
                return self.get_translation(value)
            return value
        if isinstance(value, list):
            return [self._translate_text_component(item) for item in value]
        if isinstance(value, dict):
            txt = value.get("text")
            if isinstance(txt, str) and self.should_translate(txt):
                value["text"] = self.get_translation(txt)
            translate = value.get("translate")
            if (self._component_translate_value_is_literal(translate)
                    and self.should_translate(translate)):
                value["translate"] = self.get_translation(translate)
            extra = value.get("extra")
            if isinstance(extra, list):
                value["extra"] = [self._translate_text_component(item) for item in extra]
            return value
        return value

    def _process_advancement_json(self, data):
        return core_process_advancement_json(self, data)

    @staticmethod
    def _is_advancement_json_path(path):
        return '/advancements/' in (path or '').replace('\\', '/').lower()

    def process_book_text_content(self, content, path_in_jar=None):
        # 排版規則（依官方 zh_cn 書本檔實證）：
        # 模組會把「實體換行」當成空格接回去，只有 <NEWLINE> 標記才是真正的換行。
        # 因此 CJK 譯文必須「每行文字後跟一個 <NEWLINE>」，否則整段會被接成超寬長行
        # 導致文字超出書頁、互相重疊。官方 zh_cn 每行約 16 個全形字。
        if not isinstance(content, str) or not content:
            return content
        output_lines = []

        leading_markers = self._book_text_leading_markers(path_in_jar, content)
        if leading_markers:
            output_lines.extend(["<NEWLINE>"] * leading_markers)

        normalized_content = self._normalize_inline_book_markers(content)
        skip_next_marker = False   # 文字行已自帶結尾標記時，跳過緊跟的來源標記（冪等）
        for part in re.split(r'(<NEWLINE>)', normalized_content):
            if not part:
                continue
            if part == "<NEWLINE>":
                if skip_next_marker:
                    skip_next_marker = False
                    continue
                output_lines.append("<NEWLINE>")
                continue
            skip_next_marker = False

            stripped = self._normalize_book_text_block(part)
            if not stripped:
                continue
            if self._has_cjk_text(stripped):
                text = self._to_traditional(stripped)
            elif self.should_translate(stripped):
                translated = self.get_translation(stripped)
                if translated and translated.strip() and translated.strip() != stripped:
                    text = translated.strip()
                else:
                    text = stripped
            else:
                text = stripped

            # If an older generated file embedded the marker inside translated text,
            # normalize it back into real book page controls before wrapping.
            text = re.sub(r'\s*<NEWLINE>\s*', '\n<NEWLINE>\n', text)
            for segment in text.splitlines():
                segment = segment.strip()
                if not segment:
                    continue
                if segment == "<NEWLINE>":
                    output_lines.append("<NEWLINE>")
                    continue
                if not self._has_cjk_text(segment):
                    # 英文（未翻譯）維持原樣，交給模組自己以空格斷行
                    output_lines.append(segment)
                    continue
                wrapped = self._wrap_book_text_line(segment, max_width=16.0)
                for line in wrapped.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    output_lines.append("<NEWLINE>")
                    skip_next_marker = True
        # 檔案結尾：最後一行文字後不留標記（與官方 zh_cn 檔一致）
        if (len(output_lines) >= 2 and output_lines[-1] == "<NEWLINE>"
                and output_lines[-2] != "<NEWLINE>"):
            output_lines.pop()
        return self._join_book_text_lines(output_lines)

    @staticmethod
    def _snbt_escape(text: str) -> str:
        return core_snbt_escape(text)

    # Forge 舊式 cfg 的字串清單區塊開頭：S:Names < / S:Suffixes < / S:swords < ...
    _RE_APOTH_BLOCK_START = re.compile(r'^S:[^<>\s]+(?:\s+[^<>]*)?<\s*$')

    @classmethod
    def _iter_apoth_name_lines(cls, content):
        """走訪 Apotheosis names.cfg，對 S:Xxx < ... > 區塊內的每一行
        產出 (原始行, 條目文字或 None)。條目為 None 表示該行是結構/註解，不可動。"""
        in_block = False
        for line in content.replace('\r\n', '\n').split('\n'):
            stripped = line.strip()
            if not in_block:
                if cls._RE_APOTH_BLOCK_START.match(stripped):
                    in_block = True
                yield line, None
                continue
            if stripped == '>':
                in_block = False
                yield line, None
                continue
            if not stripped or stripped.startswith('#'):
                yield line, None
                continue
            yield line, stripped

    def process_apoth_names_cfg(self, content):
        return core_process_apoth_names_cfg(self, content)

    @staticmethod
    def _snbt_structure_signature(content):
        return core_snbt_structure_signature(content)

    def process_text_file(self, content):
        return core_process_text_file(self, content)

    def _snbt_key_for_value(self, content: str, match_start: int):
        return core_snbt_key_for_value(self, content, match_start)

    def _snbt_skip_by_key(self, content: str, match_start: int) -> bool:
        return core_snbt_skip_by_key(self, content, match_start)

    def process_md_file(self, content):
        return core_process_md_file(self, content)

    # ═══════════════════════════════════════════════
    #  翻譯主流程
    # ═══════════════════════════════════════════════
    def start_translation(self):
        if self.is_processing:
            return
        rp_dir = self.rp_dir_var.get().strip()
        if not os.path.isdir(rp_dir):
            messagebox.showerror("錯誤", "找不到輸出資料夾！")
            return

        # 若尚未分析（或分析結果全空），拒絕翻譯
        if (not self.analyzed_jars and not self.analyzed_loose
                and not self.analyzed_book_texts
                and not self.analyzed_book_text_repairs
                and not self.analyzed_class_texts
                and not self.analyzed_extra and not self.analyzed_zip_json):
            messagebox.showerror("錯誤",
                "尚未分析或未找到任何可翻譯檔案！\n請先點「🔍 分析檔案」。")
            return

        current_class_choice = bool(self.class_tooltip_patch_var.get())
        analyzed_class_choice = getattr(
            self, '_scan_class_tooltip_patch', current_class_choice)
        if current_class_choice != analyzed_class_choice:
            messagebox.showerror(
                "需要重新分析",
                "低風險 class/JAR 修補選項已變更。\n"
                "請重新按一次「分析檔案」，避免使用舊掃描結果。")
            return

        pack_format = self.pack_format_var.get()
        if not (1 <= pack_format <= 99):
            messagebox.showerror("錯誤", "pack_format 必須介於 1 ~ 99 之間！")
            return
        datapack_format = self.datapack_format_var.get()
        if not (1 <= datapack_format <= 99):
            messagebox.showerror("錯誤", "Data Pack pack_format 必須介於 1 ~ 99 之間！")
            return
        if not (self.scope_mod_lang_var.get() or self.scope_books_var.get()
                or self.scope_quests_var.get()):
            messagebox.showerror("錯誤", "至少要勾選一個翻譯範圍。")
            return

        rp_name = self._safe_zip_filename(
            self.rp_name_var.get(), "Auto_Translated_Mods_zh_tw")

        self.is_processing   = True
        self.stop_requested  = False
        self.pause_requested = False
        self._set_btn_state(self.btn_analyze,   tk.DISABLED)
        self._set_btn_state(self.btn_translate, tk.DISABLED)
        self._set_btn_state(self.btn_stop,      tk.NORMAL)
        self._set_btn_state(self.btn_pause,     tk.NORMAL)
        self._reset_progress_counters()
        self.update_progress(0, 0)
        self._set_summary_card(
            "pending", "準備中",
            f"總計：{getattr(self, '_analysis_total_strings', 0):,}\n狀態：提取詞彙中",
            self.C_WARN)
        self._refresh_output_summary()
        self._refresh_api_summary(state="執行中", color=self.C_WARN)
        self.save_config()

        threading.Thread(
            target=self._translate_task,
            args=(rp_dir, rp_name, pack_format, self.output_mode_var.get()),
            daemon=True).start()

    def _translate_task(self, rp_dir, rp_name, pack_format, output_mode="jar_patch"):
        return run_translate_task(self, rp_dir, rp_name, pack_format, output_mode)

    def _maybe_install_resource_pack_to_instance(self, pack_path, rp_dir, mc_dir):
        """If the user outputs into the instance, install and enable the pack.

        This keeps normal "export somewhere else" behavior untouched, but when
        the output folder is the modpack root or its resourcepacks folder, the
        generated pack is made active immediately so translations are not left
        in an ignored assets/ folder or disabled resource pack.
        """
        try:
            rp_dir_abs = os.path.abspath(rp_dir)
            mc_dir_abs = os.path.abspath(mc_dir)
            resourcepacks_dir = os.path.join(mc_dir_abs, "resourcepacks")
            if os.path.normcase(rp_dir_abs) == os.path.normcase(mc_dir_abs):
                os.makedirs(resourcepacks_dir, exist_ok=True)
                target = os.path.join(resourcepacks_dir, os.path.basename(pack_path))
                if os.path.normcase(os.path.abspath(pack_path)) != os.path.normcase(os.path.abspath(target)):
                    shutil.copy2(pack_path, target)
                    pack_path = target
            elif os.path.normcase(rp_dir_abs) != os.path.normcase(os.path.abspath(resourcepacks_dir)):
                return

            pack_id = "file/" + os.path.basename(pack_path)
            changed = False
            for rel in ("options.txt", os.path.join("config", "defaultoptions", "options.txt")):
                options_path = os.path.join(mc_dir_abs, rel)
                if self._enable_pack_in_options(options_path, pack_id):
                    changed = True
            if changed:
                self.log(f"✅ 已自動啟用資源包：{pack_id}")
                self.log("   已更新 options.txt / defaultoptions，語言固定為 zh_tw")
        except Exception as e:
            self.log(f"⚠️ 自動啟用資源包失敗：{e}")

    def _enable_pack_in_options(self, options_path, pack_id):
        if not os.path.exists(options_path):
            return False
        temp_fd = None
        temp_path = None
        try:
            import tempfile

            with open(options_path, "rb") as f:
                original_data = f.read()
            original_text = original_data.decode("utf-8", errors="surrogateescape")
            lines = original_text.splitlines(keepends=True)
            out = []
            saw_packs = False
            saw_lang = False
            preferred_newline = next(
                (ending for line in lines
                 for ending in ("\r\n", "\n", "\r")
                 if line.endswith(ending)),
                os.linesep)
            for original_line in lines:
                ending = next(
                    (value for value in ("\r\n", "\n", "\r")
                     if original_line.endswith(value)), "")
                line = original_line[:-len(ending)] if ending else original_line
                if line.startswith("resourcePacks:"):
                    saw_packs = True
                    packs = self._parse_options_pack_list(line[len("resourcePacks:"):])
                    if "vanilla" not in packs:
                        packs.insert(0, "vanilla")
                    if "mod_resources" not in packs:
                        insert_at = 1 if packs and packs[0] == "vanilla" else 0
                        packs.insert(insert_at, "mod_resources")
                    if pack_id in packs:
                        packs = [p for p in packs if p != pack_id]
                    packs.append(pack_id)
                    new_line = "resourcePacks:" + json.dumps(packs, ensure_ascii=False, separators=(",", ":"))
                    out.append(new_line + ending)
                elif line.startswith("lang:"):
                    saw_lang = True
                    new_line = "lang:zh_tw"
                    out.append(new_line + ending)
                else:
                    out.append(original_line)

            additions = []
            if not saw_packs:
                additions.append(
                    "resourcePacks:" + json.dumps(
                        ["vanilla", "mod_resources", pack_id],
                        ensure_ascii=False, separators=(",", ":")))
            if not saw_lang:
                additions.append("lang:zh_tw")

            new_text = "".join(out)
            if additions:
                had_final_newline = original_text.endswith(("\r\n", "\n", "\r"))
                if new_text and not new_text.endswith(("\r\n", "\n", "\r")):
                    new_text += preferred_newline
                new_text += preferred_newline.join(additions)
                if had_final_newline:
                    new_text += preferred_newline

            new_data = new_text.encode("utf-8", errors="surrogateescape")
            if new_data == original_data:
                return False

            backup_path = options_path + ".translator.bak"
            backup_created = False
            try:
                with open(backup_path, "xb") as backup_file:
                    backup_created = True
                    backup_file.write(original_data)
                    backup_file.flush()
                    os.fsync(backup_file.fileno())
            except FileExistsError:
                pass
            except OSError:
                if backup_created:
                    try:
                        os.remove(backup_path)
                    except OSError:
                        pass
                return False

            options_dir = os.path.dirname(os.path.abspath(options_path))
            temp_fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(options_path)}.translator.",
                suffix=".tmp", dir=options_dir)
            temp_file = os.fdopen(temp_fd, "wb")
            temp_fd = None
            with temp_file:
                temp_file.write(new_data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, options_path)
            temp_path = None
            return True
        except OSError:
            return False
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _parse_options_pack_list(raw):
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return [str(v) for v in value]
        except Exception:
            pass
        return re.findall(r'"((?:\\.|[^"\\])*)"', raw)

    # ═══════════════════════════════════════════════
    #  執行緒安全對話框
    # ═══════════════════════════════════════════════
    def _ask_proceed_from_thread(self, title, msg):
        """從工作執行緒安全地在主執行緒彈出 Yes/No 確認框，回傳使用者選擇。
        若 5 分鐘內無回應則預設拒絕（False）。"""
        result = [False]
        event  = threading.Event()
        def _show():
            result[0] = messagebox.askyesno(title, msg, parent=self.root)
            event.set()
        self.root.after(0, _show)
        event.wait(timeout=300)
        return result[0]

    # ═══════════════════════════════════════════════
    #  階段 2.5：翻譯品質驗證
    # ═══════════════════════════════════════════════
    def _verify_translations(self, unique_strings):
        return core_verify_translations(self, unique_strings)

    def _write_failed_items_report(self, untranslated, fmt_issues, context="translation_validation"):
        return core_write_failed_items_report(self, untranslated, fmt_issues, context)

    # ═══════════════════════════════════════════════
    #  安全覆蓋模式：Paxi/OpenLoader 與設定檔，不重建客戶端模組 JAR
    # ═══════════════════════════════════════════════
    @staticmethod
    def _mixin_targets_client_renderer(text):
        return mixin_targets_client_renderer(text)

    @staticmethod
    def _jar_launch_risk_reasons(jar_path):
        """Return startup-transformer risk markers for a JAR.

        JAR direct mode rebuilds archives to inject zh_tw assets. That is usually
        safe for normal content mods, but mods participating in early launch
        transformation (renderer Mixins/CoreMod/AccessTransformer/ModLauncher
        services) are disproportionately represented in startup crashes. Skip
        those JARs and keep the game bootable; their untranslated strings can
        still be handled later by safer loose/config paths when available.
        """
        return jar_launch_risk_reasons(jar_path)

    @staticmethod
    def _jar_rewrite_is_high_risk(risk_reasons):
        return jar_rewrite_is_high_risk(risk_reasons)

    def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
        """完整重建 JAR 並注入 inject（{內部路徑: bytes}）。
        修改了已簽名 JAR 的既有條目時必須去除簽名：
        留著舊的 .SF/.RSA digest 會讓 JVM 驗簽拋 SecurityException。
        去簽名（變成未簽名 JAR）只會被 Forge 記一行警告，是業界標準做法。
        回傳是否有移除簽名。"""
        return rebuild_jar_with_inject(
            jar_path, temp_jar, inject, self._RE_JAR_SIG)

    @staticmethod
    def _has_openloader_resources(mc_dir):
        return has_openloader_resources(mc_dir)

    def _write_openloader_resource_overlay(self, combined, tmpdir, overlay_rel, inject):
        return core_write_openloader_resource_overlay(self, combined, tmpdir, overlay_rel, inject)

    def _build_class_inject_for_jar(self, jar_path, class_files):
        return core_build_class_inject_for_jar(self, jar_path, class_files)

    def _generate_class_patch_jars(self, rp_dir, rp_name, mc_dir):
        return core_generate_class_patch_jars(self, rp_dir, rp_name, mc_dir)

    def _generate_jar_patches(self, rp_dir, rp_name, mc_dir):
        return core_generate_jar_patches(self, rp_dir, rp_name, mc_dir)

    def cleanup_on_exit(self):
        """視窗關閉時清理資源，特別是關閉 shelve 快取。"""
        try:
            # 關閉 shelve 快取
            if hasattr(self, 'translation_cache') and hasattr(self.translation_cache, 'close'):
                try:
                    self.translation_cache.close()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.root.destroy()

    def on_closing(self):
        """視窗關閉事件處理。"""
        if self.is_processing:
            # 正在處理中，先停止
            self.stop_requested = True
            self.log("正在停止處理，請稍候...")
            # 給 1 秒讓執行緒反應
            self.root.after(1000, self.cleanup_on_exit)
        else:
            self.cleanup_on_exit()

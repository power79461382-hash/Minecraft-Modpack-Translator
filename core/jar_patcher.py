import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from typing import Dict, Iterable, Pattern, Set, Tuple

from core.analysis_scan import (
    is_minecraft_version_root_jar,
    java_runtime_warning_lines,
)


def mixin_targets_client_renderer(text: str) -> bool:
    """判斷 Mixin 設定是否觸及 Minecraft 用戶端渲染核心。"""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    renderer_markers = (
        'net.minecraft.client.renderer',
        'net/minecraft/client/renderer',
        'levelrenderer',
        'gamerenderer',
        'itemrenderer',
        'entityrenderer',
        'rendertype',
        'rendersystem',
        'lighttexture',
        'outlinebuffersource',
        'client.renderer',
        'client/render',
    )
    return any(marker in lowered for marker in renderer_markers)


def jar_launch_risk_reasons(jar_path: str) -> Tuple[str, ...]:
    """回傳 JAR 啟動期重包風險標記。"""
    reasons: Set[str] = set()
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            for name in jar.namelist():
                n = name.replace('\\', '/').lower()
                base = n.rsplit('/', 1)[-1]
                if (n.endswith('.mixins.json') or n.endswith('.mixin.json')
                        or ('mixin' in base and n.endswith('.json'))):
                    reasons.add('mixin')
                    try:
                        mixin_text = jar.read(name).decode('utf-8', 'ignore')
                    except Exception:
                        mixin_text = ''
                    if mixin_targets_client_renderer(mixin_text):
                        reasons.add('renderer-mixin')
                elif 'coremod' in n:
                    reasons.add('coremod')
                elif 'accesstransformer' in n or n.endswith('_at.cfg') or n.endswith('/at.cfg'):
                    reasons.add('access-transformer')
                elif (n.startswith('meta-inf/services/cpw.mods.modlauncher')
                      or n.startswith('meta-inf/services/org.spongepowered')):
                    reasons.add('modlauncher-service')
                if len(reasons) >= 2:
                    break
    except (OSError, zipfile.BadZipFile):
        reasons.add('unreadable-jar')
    return tuple(sorted(reasons))


def jar_rewrite_is_high_risk(risk_reasons: Iterable[str]) -> bool:
    """判斷 JAR 是否不應直接重包。"""
    reasons = set(risk_reasons or ())
    return bool(reasons & {
        'access-transformer',
        'coremod',
        'modlauncher-service',
        'renderer-mixin',
        'unreadable-jar',
    })


def _strip_manifest_digest_headers(manifest: bytes) -> Tuple[bytes, bool]:
    """Remove digest attributes and their continuation lines from a manifest."""
    cleaned_lines = []
    skip_continuations = False
    removed = False
    for line in manifest.splitlines(keepends=True):
        if line.startswith(b' '):
            if skip_continuations:
                removed = True
                continue
            cleaned_lines.append(line)
            continue

        skip_continuations = False
        header_name = line.split(b':', 1)[0].strip().lower()
        is_digest_header = (
            header_name == b'digest-algorithms'
            or re.fullmatch(
                rb'[a-z0-9][a-z0-9-]*-digest'
                rb'(?:-manifest(?:-main-attributes)?)?',
                header_name,
                flags=re.IGNORECASE,
            ) is not None
        )
        if is_digest_header:
            skip_continuations = True
            removed = True
            continue
        cleaned_lines.append(line)
    return b''.join(cleaned_lines), removed


def rebuild_jar_with_inject(jar_path: str, temp_jar: str,
                            inject: Dict[str, bytes],
                            jar_sig_re: Pattern[str]) -> bool:
    """完整重建 JAR 並注入翻譯資源；必要時移除過期簽名。"""
    with zipfile.ZipFile(jar_path, 'r') as src_jar, \
            zipfile.ZipFile(temp_jar, 'w') as dst_jar:
        src_names = {i.filename for i in src_jar.infolist()}
        modifies_existing = any(p in src_names for p in inject)
        stripped_sig = False
        for item in src_jar.infolist():
            if item.filename in inject:
                continue
            if modifies_existing and jar_sig_re.match(item.filename):
                stripped_sig = True
                continue
            data_bytes = src_jar.read(item)
            if (modifies_existing
                    and item.filename.upper() == 'META-INF/MANIFEST.MF'):
                data_bytes, removed_digests = _strip_manifest_digest_headers(
                    data_bytes)
                stripped_sig = stripped_sig or removed_digests
            dst_jar.writestr(item, data_bytes)
        for inj_path, inj_bytes in inject.items():
            dst_jar.writestr(inj_path, inj_bytes)
    return stripped_sig


def has_openloader_resources(mc_dir: str) -> bool:
    """偵測整合包是否有 OpenLoader resources 能安全覆蓋資源。"""
    config_resources = os.path.join(mc_dir, 'config', 'openloader', 'resources')
    if os.path.isdir(config_resources):
        return True
    mods_dir = os.path.join(mc_dir, 'mods')
    try:
        for name in os.listdir(mods_dir):
            low = name.lower()
            if low.endswith('.jar') and ('open-loader' in low or 'openloader' in low):
                return True
    except OSError:
        pass
    return False


def has_paxi(mc_dir: str) -> bool:
    """Return whether the modpack can auto-load Paxi resource/data overlays."""
    mods_dir = os.path.join(mc_dir, 'mods')
    try:
        return any(
            name.lower().endswith('.jar') and name.lower().startswith('paxi-')
            for name in os.listdir(mods_dir)
        )
    except OSError:
        return False


def find_packaged_mod_jars(archive_path: str):
    """Return any top-level mod JAR embedded in a translation package.

    Client output uses overlays only. Even a seemingly resource-only JAR is
    rejected because it can contain nested executables or invalid metadata.
    """
    try:
        with zipfile.ZipFile(archive_path, 'r') as package:
            mod_jars = {
                normalized
                for info in package.infolist()
                for normalized in (info.filename.replace('\\', '/'),)
                if normalized.lower().startswith('mods/')
                and normalized.lower().endswith('.jar')
                and normalized.count('/') == 1
            }
    except (OSError, zipfile.BadZipFile):
        return ['<invalid-translation-package>']
    return sorted(mod_jars, key=lambda name: (name.casefold(), name))


@contextmanager
def atomic_zip_output(final_path, should_commit, validate=None, state=None):
    """Write a ZIP beside its destination and replace only after validation."""
    output_dir = os.path.dirname(os.path.abspath(final_path)) or os.curdir
    os.makedirs(output_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(final_path)}.",
        suffix='.tmp',
        dir=output_dir)
    os.close(fd)
    committed = False
    if state is not None:
        state['committed'] = False
        state['temp_path'] = temp_path
    try:
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            yield archive
        if not should_commit():
            return
        if validate is not None and not validate(temp_path):
            return
        with open(temp_path, 'r+b') as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        committed = True
        if state is not None:
            state['committed'] = True
    finally:
        if not committed:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@contextmanager
def atomic_zip_output_group(final_paths, should_commit, validate=None, state=None):
    """Atomically replace a set of ZIPs, restoring every old file on failure."""
    records = []
    archives = {}
    seen = set()
    committed = False
    if state is not None:
        state['committed'] = False
        state['temp_paths'] = []
        state['recovery_backups'] = []

    try:
        for raw_path in final_paths:
            final_path = os.path.abspath(raw_path)
            canonical = os.path.normcase(final_path)
            if canonical in seen:
                raise ValueError(f"duplicate atomic ZIP destination: {final_path}")
            seen.add(canonical)
            output_dir = os.path.dirname(final_path) or os.curdir
            os.makedirs(output_dir, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(final_path)}.",
                suffix='.tmp',
                dir=output_dir)
            os.close(fd)
            record = {
                'raw': raw_path,
                'final': final_path,
                'temp': temp_path,
                'backup': None,
                'preserve_backup': False,
                'archive': zipfile.ZipFile(
                    temp_path, 'w', zipfile.ZIP_DEFLATED),
            }
            records.append(record)
            archives[raw_path] = record['archive']
            if state is not None:
                state['temp_paths'].append(temp_path)

        try:
            yield archives
        finally:
            for record in records:
                archive = record.get('archive')
                if archive is not None:
                    archive.close()
                    record['archive'] = None

        if not should_commit():
            return
        if validate is not None:
            for record in records:
                if not validate(record['temp']):
                    return
        for record in records:
            with open(record['temp'], 'r+b') as handle:
                handle.flush()
                os.fsync(handle.fileno())

        # Keep copies of every previous output until all replacements succeed.
        for record in records:
            if not os.path.exists(record['final']):
                continue
            output_dir = os.path.dirname(record['final']) or os.curdir
            fd, backup_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(record['final'])}.",
                suffix='.bak',
                dir=output_dir)
            os.close(fd)
            shutil.copy2(record['final'], backup_path)
            record['backup'] = backup_path

        replaced = []
        try:
            for record in records:
                os.replace(record['temp'], record['final'])
                record['temp'] = None
                replaced.append(record)
        except Exception as exc:
            rollback_errors = []
            for record in reversed(replaced):
                try:
                    if record['backup'] is not None:
                        os.replace(record['backup'], record['final'])
                        record['backup'] = None
                    else:
                        os.remove(record['final'])
                except OSError as rollback_exc:
                    restored_by_copy = False
                    if record['backup'] is not None:
                        try:
                            shutil.copy2(record['backup'], record['final'])
                            restored_by_copy = True
                        except OSError as copy_exc:
                            record['preserve_backup'] = True
                            if state is not None:
                                state['recovery_backups'].append(
                                    record['backup'])
                            rollback_errors.append(
                                f"{rollback_exc}; copy fallback: {copy_exc}; "
                                f"backup: {record['backup']}")
                    else:
                        rollback_errors.append(str(rollback_exc))
                    if restored_by_copy:
                        continue
            if rollback_errors:
                raise RuntimeError(
                    "ZIP 群組替換失敗，且舊輸出回復不完整："
                    + "; ".join(rollback_errors)) from exc
            raise

        committed = True
        if state is not None:
            state['committed'] = True
    finally:
        for record in records:
            archive = record.get('archive')
            if archive is not None:
                try:
                    archive.close()
                except Exception:
                    pass
            for key in ('temp', 'backup'):
                path = record.get(key)
                if key == 'backup' and record.get('preserve_backup'):
                    continue
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        if state is not None and not committed:
            state['committed'] = False


def split_paxi_safe_inject(inject: Dict[str, bytes]):
    """Split generated patches into safe Paxi overlays and residual JAR edits.

    Resource assets are client-side overrides. Advancement and Patchouli JSON
    are data-pack resources. Some modpacks fail to apply Patchouli book JSON
    from Paxi datapacks, so Patchouli data resources are also mirrored to the
    equivalent resource-pack assets path as a harmless fallback.
    """
    resources = {}
    data = {}
    residual = {}
    for raw_path, payload in (inject or {}).items():
        path = raw_path.replace('\\', '/')
        lower = path.lower()
        if lower.startswith('assets/'):
            resources[path] = payload
        elif (lower.startswith('data/') and lower.endswith('.json')
              and ('/advancements/' in lower or '/patchouli_books/' in lower)):
            data[path] = payload
            asset_fallback = patchouli_data_resource_fallback_path(path)
            if asset_fallback:
                resources[asset_fallback] = payload
        else:
            residual[path] = payload
    return resources, data, residual


class PaxiOverlayAccumulator:
    """Collect overlay entries and serialize merged language JSON once."""

    def __init__(self):
        self._raw_entries: Dict[str, bytes] = {}
        self._language_entries: Dict[str, dict] = {}

    def __bool__(self):
        return bool(self._raw_entries or self._language_entries)

    def __len__(self):
        return len(self._raw_entries) + len(self._language_entries)

    def add(self, path: str, payload: bytes) -> bool:
        normalized = path.replace('\\', '/')
        collision = (
            normalized in self._raw_entries
            or normalized in self._language_entries)
        lower = normalized.lower()
        parsed = None
        if '/lang/' in lower and lower.endswith('.json'):
            try:
                candidate = json.loads(payload.decode('utf-8-sig'))
            except (UnicodeError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate

        if parsed is not None:
            existing = self._language_entries.get(normalized)
            if existing is None:
                self._language_entries[normalized] = dict(parsed)
            else:
                existing.update(parsed)
            self._raw_entries.pop(normalized, None)
            return collision

        self._language_entries.pop(normalized, None)
        self._raw_entries[normalized] = payload
        return collision

    def serialized_items(self):
        entries = dict(self._raw_entries)
        for path, data in self._language_entries.items():
            entries[path] = json.dumps(
                data, ensure_ascii=False, indent=2).encode('utf-8')
        return sorted(entries.items())


def merge_paxi_overlay_entry(target, path: str,
                             payload: bytes) -> bool:
    """Add an overlay entry, merging duplicate JSON language dictionaries."""
    if isinstance(target, PaxiOverlayAccumulator):
        return target.add(path, payload)

    normalized = path.replace('\\', '/')
    existing = target.get(normalized)
    if existing is None:
        target[normalized] = payload
        return False

    lower = normalized.lower()
    if '/lang/' in lower and lower.endswith('.json'):
        try:
            old_data = json.loads(existing.decode('utf-8-sig'))
            new_data = json.loads(payload.decode('utf-8-sig'))
        except (UnicodeError, json.JSONDecodeError):
            pass
        else:
            if isinstance(old_data, dict) and isinstance(new_data, dict):
                old_data.update(new_data)
                target[normalized] = json.dumps(
                    old_data, ensure_ascii=False, indent=2).encode('utf-8')
                return True

    target[normalized] = payload
    return True


def build_paxi_overlay_zip(pack_format, description,
                           overlay: PaxiOverlayAccumulator) -> bytes:
    """Build one replaceable Paxi pack ZIP to prevent stale extracted files."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as pack:
        pack.writestr(
            'pack.mcmeta',
            json_bytes(pack_mcmeta(pack_format, description)))
        for path, payload in overlay.serialized_items():
            pack.writestr(path, payload)
    return output.getvalue()


def patchouli_data_resource_fallback_path(path: str):
    path = (path or '').replace('\\', '/')
    lower = path.lower()
    if not (lower.startswith('data/') and '/patchouli_books/' in lower
            and lower.endswith('.json')):
        return None
    parts = path.split('/')
    if len(parts) < 4 or parts[2].lower() != 'patchouli_books':
        return None
    return 'assets/' + '/'.join(parts[1:])


def paxi_load_order_bytes(mc_dir: str, ordering_filename: str, pack_name: str) -> bytes:
    """Build a Paxi load-order JSON that keeps existing entries and appends ours."""
    existing = []
    order_path = os.path.join(mc_dir, 'config', 'paxi', ordering_filename)
    try:
        with open(order_path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        raw_order = loaded.get('loadOrder') if isinstance(loaded, dict) else None
        if isinstance(raw_order, list):
            existing = [item for item in raw_order if isinstance(item, str) and item.strip()]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        existing = []
    merged = [item for item in existing if item != pack_name]
    if pack_name:
        merged.append(pack_name)
    return json.dumps({"loadOrder": merged}, ensure_ascii=False, indent=4).encode('utf-8')


from translation_packager import (
    defaultconfigs_mirror_path,
    ftbq_lang_snbt_role,
    json_bytes,
    lang_bytes,
    load_lang_content,
    load_json_content,
    loose_zh_tw_path,
    merge_jar_lang_data,
    merge_structured_json_with_existing_zh,
    merge_with_existing_zh,
    pack_mcmeta,
    parse_legacy_lang_content,
    safe_utf8_bytes,
    is_localized_manual_resource,
    translated_fallback_paths,
    translated_lang_path,
    translated_repair_fallback_paths,
)


def write_openloader_resource_overlay(self, combined, tmpdir, overlay_rel, inject):
    overlay_inject = {
        path.replace('\\', '/'): data
        for path, data in inject.items()
        if path.replace('\\', '/').startswith('assets/')
    }
    if not overlay_inject:
        return 0
    temp_zip = os.path.join(tmpdir, os.path.basename(overlay_rel))
    with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as overlay_zip:
        overlay_zip.writestr(
            'pack.mcmeta',
            json_bytes(pack_mcmeta(
                getattr(
                    self, '_detected_resource_pack_format',
                    self.pack_format_var.get()),
                "自動翻譯：高風險 JAR 安全覆蓋")))
        for path, data in overlay_inject.items():
            overlay_zip.writestr(path, data)
    combined.write(temp_zip, overlay_rel, compress_type=zipfile.ZIP_STORED)
    return len(overlay_inject)

def build_class_inject_for_jar(self, jar_path, class_files):
    """為單一 JAR 建立 class 硬編碼字串的注入表（含 placeholder/格式碼驗證）。"""
    inject = {}
    jar_name = os.path.basename(jar_path)
    with zipfile.ZipFile(jar_path, 'r') as class_src:
        for path_in_jar, strings in class_files.items():
            if self.stop_requested:
                break
            replacements = {}
            for text in strings:
                translated = self.get_translation(text)
                if not (translated and translated != text
                        and self._is_valid_trad_translation(text, translated)):
                    continue
                # class 字串常被 String.format 使用：%s/§ 等 token
                # 數量或種類不符會在執行期拋 IllegalFormatException
                # （用 Counter：%1$s/%2$s 重排在 Java 合法，不可因順序丟棄）
                if (Counter(self._RE_PLACEHOLDER.findall(text))
                        != Counter(self._RE_PLACEHOLDER.findall(translated))):
                    continue
                if (Counter(self._critical_format_tokens(text))
                        != Counter(self._critical_format_tokens(translated))):
                    continue
                replacements[text] = translated
            if replacements:
                patched, changed = self._patch_class_hardcoded_strings(
                    class_src.read(path_in_jar), replacements)
                if changed:
                    inject[path_in_jar] = patched
                    self.log(f"🧩 {jar_name}: class 硬編碼敘述 {path_in_jar} 修補 {changed} 筆")
    return inject

def generate_class_patch_jars(self, rp_dir, rp_name, mc_dir):
    """Build the opt-in low-risk class/JAR patch used by hybrid mode."""
    base_name = rp_name.replace('.zip', '')
    patch_zip_path = os.path.join(rp_dir, base_name + '_Class硬編碼補丁.zip')

    def remove_stale_patch():
        try:
            os.remove(patch_zip_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(
                f"無法移除舊 class 補丁 {patch_zip_path}: {exc}") from exc

    if bool(getattr(self, '_server_mode', False)):
        remove_stale_patch()
        self.log("🛡️ 伺服器模式不修改 class，已略過 class/JAR 補丁。")
        return ""
    if not bool(getattr(self, '_scan_class_tooltip_patch', False)):
        remove_stale_patch()
        self.log("ℹ️ 低風險 class/JAR 修補未啟用。")
        return ""
    if not self.analyzed_class_texts:
        remove_stale_patch()
        self.log("ℹ️ 沒有偵測到需要修補的 class 硬編碼字串，免出 JAR 補丁。")
        return ""
    jar_count = 0
    atomic_state = {}

    def validate_patch(path):
        try:
            with zipfile.ZipFile(path, 'r') as package:
                return package.testzip() is None
        except (OSError, zipfile.BadZipFile):
            return False

    self.log("\n--- 階段四：生成 class 硬編碼補丁（僅資源包覆蓋不到的字串） ---")
    with tempfile.TemporaryDirectory() as tmpdir, \
         atomic_zip_output(
             patch_zip_path,
             lambda: jar_count > 0 and not self.stop_requested,
             validate=validate_patch,
             state=atomic_state) as combined:
        for jar_path, class_files in self.analyzed_class_texts.items():
            if self.stop_requested:
                break
            jar_name = os.path.basename(jar_path)
            jar_rel = os.path.relpath(jar_path, mc_dir).replace('\\', '/')
            if jar_rel.startswith('..'):
                jar_rel = 'mods/' + jar_name
            allow_root_jar = bool(getattr(self, '_allow_root_jar', False))
            if (not allow_root_jar
                    and is_minecraft_version_root_jar(mc_dir, jar_path)):
                self.log(
                    f"  ⚠️ {jar_name}: 客戶端版本主 JAR 不可重包，已略過")
                continue
            risk_reasons = self._jar_launch_risk_reasons(jar_path)
            if self._jar_rewrite_is_high_risk(risk_reasons):
                self.log(
                    f"  🛡️ {jar_name}: 跳過 class 補丁"
                    f"（啟動期高風險：{', '.join(risk_reasons)}）")
                continue
            try:
                inject = self._build_class_inject_for_jar(jar_path, class_files)
            except (OSError, zipfile.BadZipFile, KeyError) as e:
                self.log(f"⚠️ {jar_name}: class 硬編碼掃描失敗: {e}")
                continue
            if not inject:
                continue
            try:
                combined.write(jar_path, '_backups/' + jar_rel,
                               compress_type=zipfile.ZIP_STORED)
            except OSError as e:
                self.log(f"  ⚠️ 備份失敗 {jar_rel}: {e}")
                continue
            temp_jar = os.path.join(tmpdir, jar_name)
            try:
                if self._rebuild_jar_with_inject(jar_path, temp_jar, inject):
                    self.log(f"  🔏 {jar_name}: 已移除過期簽名（避免驗簽崩潰）")
            except Exception as e:
                self.log(f"  ⚠️ {jar_name} 處理失敗：{e}")
                continue
            combined.write(temp_jar, jar_rel, compress_type=zipfile.ZIP_STORED)
            jar_count += 1
            self.log(f"  ✅ {jar_name}（class 修補 {len(inject)} 個檔案）")
    if jar_count and not self.stop_requested:
        if not atomic_state.get('committed'):
            raise RuntimeError("Class/JAR 補丁驗證失敗，舊輸出已保留")
        self.log(f"📦 Class 硬編碼補丁：{patch_zip_path}")
        self.log(
            "   解壓到遊戲實例根目錄即可；mods/ 與版本 JAR 的原檔"
            "保存在 _backups/")
        return patch_zip_path if atomic_state.get('committed') else ""
    elif not jar_count:
        remove_stale_patch()
        self.log("ℹ️ 所有 class 硬編碼字串皆無有效翻譯，未產生 JAR 補丁。")
    return ""

def translation_package_path(rp_dir, rp_name):
    """Build the final package name without duplicating its Chinese suffix."""
    base_name = os.path.splitext(os.path.basename(rp_name))[0]
    suffix = '_模組語言包'
    if not base_name.endswith(suffix):
        base_name += suffix
    return os.path.join(rp_dir, base_name + '.zip')


def generate_jar_patches(self, rp_dir, rp_name, mc_dir):
    base_name = os.path.splitext(os.path.basename(rp_name))[0]
    combined_zip_path = translation_package_path(rp_dir, rp_name)

    total_tasks  = (
        sum(1 for lf in self.analyzed_jars.values()
            for p in lf if self._scope_allows_analyzed_path("jar", p))
        + sum(1 for p in self.analyzed_loose
              if self._scope_allows_analyzed_path("loose", p))
        + sum(1 for bf in self.analyzed_book_texts.values()
              for p in bf if self._scope_allows_analyzed_path("book_txt", p))
        + sum(1 for bf in self.analyzed_book_text_repairs.values()
              for p in bf if self._scope_allows_analyzed_path("book_txt", p))
        + sum(len(strings)
              for files in self.analyzed_class_texts.values()
              for strings in files.values())
        + sum(1 for t, p in self.analyzed_extra
              if self._scope_allows_extra(t, p))
        + len(self.analyzed_zip_json))
    current_task = 0
    jar_count    = 0
    cfg_count    = 0
    backup_count = 0
    skipped_large_backup_count = 0
    skipped_risky_jars = []
    openloader_available = self._has_openloader_resources(mc_dir)
    openloader_overlay_count = 0
    paxi_available = has_paxi(mc_dir)
    client_safe_mode = not bool(getattr(self, '_server_mode', False))
    paxi_resource_overlay = PaxiOverlayAccumulator()
    paxi_data_overlay = PaxiOverlayAccumulator()
    safe_overlay_base = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]+', '_', f'{base_name}_自動翻譯覆蓋').strip(' ._')
    paxi_resource_name = safe_overlay_base + '.zip'
    paxi_data_name = safe_overlay_base + '.zip'
    paxi_resource_rel = f'config/paxi/resourcepacks/{paxi_resource_name}'
    paxi_data_rel = f'config/paxi/datapacks/{paxi_data_name}'
    resource_pack_format = getattr(
        self, '_detected_resource_pack_format', None)
    if resource_pack_format is None:
        resource_pack_format = self.pack_format_var.get()
    data_pack_format = getattr(self, '_detected_data_pack_format', None)
    if data_pack_format is None:
        data_format_var = getattr(self, 'datapack_format_var', None)
        data_pack_format = (
            data_format_var.get()
            if data_format_var is not None
            else resource_pack_format)
    resource_pack_format = int(resource_pack_format)
    data_pack_format = int(data_pack_format)

    def is_safe_text_resource_only(inject_map):
        """Only player-facing text resources; no class/config/recipe rewrites."""
        if not inject_map:
            return False
        for out_path in inject_map:
            p = out_path.replace('\\', '/').lower()
            if p.endswith('.class') or p.startswith(('config/', 'defaultconfigs/', 'meta-inf/')):
                return False
            if p.startswith('assets/'):
                if (
                    '/lang/' in p
                    or '/patchouli_books/' in p
                    or '/book/' in p
                    or p.endswith('/book.json')
                    or is_localized_manual_resource(p)
                ):
                    continue
                return False
            if p.startswith('data/') and p.endswith('.json'):
                if '/patchouli_books/' in p or '/advancements/' in p:
                    continue
                return False
            return False
        return True

    atomic_state = {}

    def validate_output_package(path):
        if not client_safe_mode:
            return True
        unsafe_mod_jars = find_packaged_mod_jars(path)
        if not unsafe_mod_jars:
            return True
        self.log(
            "⛔ 輸出安全稽核失敗：客戶端翻譯包含禁止的 mods/*.jar："
            + ", ".join(unsafe_mod_jars))
        return False

    # ── 全部輸出先寫同目錄暫存 ZIP，完整驗證後才原子替換 ──
    with tempfile.TemporaryDirectory() as tmpdir, \
         atomic_zip_output(
             combined_zip_path,
             lambda: (not self.stop_requested
                      and (jar_count > 0 or cfg_count > 0)),
             validate=validate_output_package,
             state=atomic_state) as combined:

        def archive_rel_path(abs_path):
            rel_path = os.path.relpath(abs_path, mc_dir).replace('\\', '/')
            if rel_path.startswith('..') or os.path.isabs(rel_path):
                rel_path = os.path.join('mods', os.path.basename(abs_path)).replace('\\', '/')
            return rel_path

        def is_mod_archive_rel(rel_path):
            normalized = rel_path.replace('\\', '/').lower()
            return normalized.endswith('.jar') and (
                normalized.startswith('mods/') or '/' not in normalized)

        def temp_archive_path(index, rel_path):
            ext = os.path.splitext(rel_path)[1] or '.jar'
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', rel_path).strip(' ._')
            if not safe_name:
                safe_name = f'archive_{index}{ext}'
            if not safe_name.lower().endswith(ext.lower()):
                safe_name += ext
            return os.path.join(tmpdir, f'{index:05d}_{safe_name}')

        def add_original_backup(abs_path, rel_path):
            nonlocal backup_count, skipped_large_backup_count
            if not abs_path or not os.path.exists(abs_path):
                return
            normalized_rel = rel_path.replace('\\', '/')
            is_large_archive_backup = normalized_rel.lower().endswith(('.jar', '.zip'))
            include_large_backups = bool(
                getattr(getattr(self, 'include_large_backups_var', None), 'get', lambda: False)())
            if is_large_archive_backup and not include_large_backups:
                skipped_large_backup_count += 1
                return
            backup_rel = '_backups/' + normalized_rel
            try:
                combined.write(abs_path, backup_rel, compress_type=zipfile.ZIP_STORED)
                backup_count += 1
            except OSError as e:
                self.log(f"  ⚠️ 備份失敗 {rel_path}: {e}")

        def count_scoped_jar_tasks(jar_path, lang_files):
            return (
                sum(1 for p in lang_files
                    if self._scope_allows_analyzed_path("jar", p))
                + sum(1 for p in self.analyzed_book_texts.get(jar_path, {})
                      if self._scope_allows_analyzed_path("book_txt", p))
                + sum(1 for p in self.analyzed_book_text_repairs.get(jar_path, {})
                      if self._scope_allows_analyzed_path("book_txt", p))
                + sum(len(strings)
                      for strings in self.analyzed_class_texts.get(jar_path, {}).values())
            )

        jar_paths_to_patch = []
        seen_jar_paths = set()
        for jar_map in (
                self.analyzed_jars,
                self.analyzed_book_texts,
                self.analyzed_book_text_repairs,
                self.analyzed_class_texts):
            for jar_path in jar_map:
                if jar_path not in seen_jar_paths:
                    seen_jar_paths.add(jar_path)
                    jar_paths_to_patch.append(jar_path)
        jar_paths_to_patch.sort(key=lambda path: (
            archive_rel_path(path).casefold(), archive_rel_path(path)))

        for archive_index, jar_path in enumerate(jar_paths_to_patch, 1):
            if self.stop_requested:
                break
            lang_files = self.analyzed_jars.get(jar_path, {})
            jar_name = os.path.basename(jar_path)
            jar_rel = archive_rel_path(jar_path)
            temp_jar = temp_archive_path(archive_index, jar_rel)
            is_mod_archive = is_mod_archive_rel(jar_rel)
            normalized_jar_rel = jar_rel.replace('\\', '/').lower()
            is_paxi_mod_archive = (
                normalized_jar_rel.startswith('mods/')
                and normalized_jar_rel.endswith('.jar'))

            allow_root_jar = bool(getattr(self, '_allow_root_jar', False))
            if (not allow_root_jar
                    and is_minecraft_version_root_jar(mc_dir, jar_path)):
                current_task += count_scoped_jar_tasks(jar_path, lang_files)
                self.update_progress(current_task, total_tasks)
                self.log(
                    f"  ⚠️ {jar_name}: 客戶端版本主 JAR 不可重包，已略過")
                continue

            risk_reasons = self._jar_launch_risk_reasons(jar_path)
            high_risk_rewrite = self._jar_rewrite_is_high_risk(risk_reasons)

            # 建立翻譯注入表（zh_tw 路徑 → 翻譯後的 bytes）
            inject = {}
            for path_in_jar, lang_data in lang_files.items():
                if self.stop_requested:
                    break
                if not self._scope_allows_analyzed_path("jar", path_in_jar):
                    continue
                zh_base = (self.analyzed_jars_zh_base
                           .get(jar_path, {})
                           .get(path_in_jar, {}))
                if self.process_mode_var.get() == "force":
                    # force 模式不應丟棄 zh_cn fallback，
                    # 只是不管 zh_tw 是否已存在都重新翻譯
                    pass
                else:
                    official_base = self._load_official_minecraft_zh_base(mc_dir, path_in_jar)
                    if official_base:
                        zh_base = self._combine_lang_bases(lang_data, official_base, zh_base)
                if self._is_advancement_json_path(path_in_jar):
                    merged_data = self._process_advancement_json(lang_data)
                elif self._is_structured_book_json_path(path_in_jar):
                    def process_book_data(data, preserve=False, strict=False):
                        return self.process_json_data(data, preserve, True)

                    merged_data = merge_structured_json_with_existing_zh(
                        lang_data, zh_base, process_book_data, self._to_traditional,
                        value_needs_update=self._lang_value_needs_update)
                    if hasattr(self, "_repair_structured_book_json_output"):
                        merged_data = self._repair_structured_book_json_output(
                            lang_data, zh_base, merged_data, process_book_data,
                            f"{jar_name}:{path_in_jar}")
                else:
                    def process_jar_data(data, preserve=False, strict=False, _path=path_in_jar):
                        return self.process_json_data(
                            data, preserve, strict or self._is_structured_book_json_path(_path))

                    merged_data = merge_jar_lang_data(
                        lang_data, zh_base, process_jar_data, self._to_traditional)
                zh_path = translated_lang_path(path_in_jar)
                if not zh_path:
                    current_task += 1
                    self.update_progress(current_task, total_tasks)
                    continue
                if '/lang/' in path_in_jar:
                    merged_data, dropped, format_dropped = self._filter_lang_output_entries(lang_data, merged_data)
                    if dropped:
                        extra = f"（含 {format_dropped:,} 個格式碼異常）" if format_dropped else ""
                        self.log(f"  ℹ️ {jar_name}: 略過 {dropped:,} 個尚未翻譯的語言項目{extra}")
                    if not merged_data:
                        self.log(
                            f"  ⚠️ {jar_name}: {path_in_jar} 沒有可用繁中譯文，"
                            "略過語言檔注入，避免輸出空 zh_tw")
                        current_task += 1
                        self.update_progress(current_task, total_tasks)
                        continue
                translated_payload = lang_bytes(path_in_jar, merged_data)
                inject[zh_path] = translated_payload
                for fallback_path in translated_fallback_paths(path_in_jar):
                    inject[fallback_path] = translated_payload
                current_task += 1
                self.update_progress(current_task, total_tasks)

            for path_in_jar, content in self.analyzed_book_texts.get(jar_path, {}).items():
                if self.stop_requested:
                    break
                if not self._scope_allows_analyzed_path("book_txt", path_in_jar):
                    continue
                zh_path = translated_lang_path(path_in_jar)
                if not zh_path:
                    current_task += 1
                    self.update_progress(current_task, total_tasks)
                    continue
                translated_text = self.process_book_text_content(content, path_in_jar)
                if (hasattr(self, "_book_text_has_effective_translation")
                        and not self._book_text_has_effective_translation(content, translated_text)):
                    self.log(
                        f"  ⚠️ {jar_name}: 書本 TXT 尚無有效譯文，略過 {path_in_jar}，"
                        "避免輸出英文 zh_tw")
                    current_task += 1
                    self.update_progress(current_task, total_tasks)
                    continue
                translated_payload = safe_utf8_bytes(translated_text)
                inject[zh_path] = translated_payload
                for fallback_path in translated_fallback_paths(path_in_jar):
                    inject[fallback_path] = translated_payload
                current_task += 1
                self.update_progress(current_task, total_tasks)

            for path_in_jar, content in self.analyzed_book_text_repairs.get(jar_path, {}).items():
                if self.stop_requested:
                    break
                if not self._scope_allows_analyzed_path("book_txt", path_in_jar):
                    continue
                fixed_payload = safe_utf8_bytes(self.process_book_text_content(content, path_in_jar))
                inject[path_in_jar] = fixed_payload
                for fallback_path in translated_repair_fallback_paths(path_in_jar):
                    inject[fallback_path] = fixed_payload
                current_task += 1
                self.update_progress(current_task, total_tasks)

            class_files = self.analyzed_class_texts.get(jar_path, {})
            if class_files:
                if client_safe_mode:
                    class_count = sum(len(strings) for strings in class_files.values())
                    current_task += class_count
                    self.update_progress(current_task, total_tasks)
                    self.log(
                        f"  🛡️ {jar_name}: 客戶端安全模式不修改任何 class；"
                        f"{class_count} 筆硬編碼文字保留原文。")
                elif not is_mod_archive:
                    class_count = sum(len(strings) for strings in class_files.values())
                    current_task += class_count
                    self.update_progress(current_task, total_tasks)
                    self.log(
                        f"  🛡️ {jar_rel}: 非 mods/根目錄 JAR，跳過 class tooltip 修補，"
                        "只輸出語言與書本資源。")
                elif high_risk_rewrite:
                    class_count = sum(len(strings) for strings in class_files.values())
                    current_task += class_count
                    self.update_progress(current_task, total_tasks)
                    self.log(
                        f"  🛡️ {jar_name}: 跳過 class tooltip 修補"
                        f"（啟動期高風險：{', '.join(risk_reasons)}）")
                else:
                    try:
                        class_inject = self._build_class_inject_for_jar(jar_path, class_files)
                    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
                        class_inject = {}
                        self.log(f"⚠️ {jar_name}: class tooltip 修補失敗，已跳過：{e}")
                    if class_inject:
                        inject.update(class_inject)
                    current_task += sum(len(strings) for strings in class_files.values())
                    self.update_progress(current_task, total_tasks)

            if self.stop_requested or not inject:
                continue

            # Client translation must never replace executable mod archives.
            # Paxi/OpenLoader can safely provide text resources without touching
            # bytecode, signatures, JarJar metadata, or Mixin containers.
            if client_safe_mode and is_mod_archive:
                overlaid_count = 0
                overlay_targets = []
                if paxi_available and is_paxi_mod_archive:
                    resource_safe, data_safe, residual = split_paxi_safe_inject(
                        inject)
                    for path, payload in resource_safe.items():
                        merge_paxi_overlay_entry(
                            paxi_resource_overlay, path, payload)
                    for path, payload in data_safe.items():
                        merge_paxi_overlay_entry(
                            paxi_data_overlay, path, payload)
                    overlaid_count = len(resource_safe) + len(data_safe)
                    if resource_safe:
                        overlay_targets.append(paxi_resource_rel)
                    if data_safe:
                        overlay_targets.append(paxi_data_rel)
                    inject = residual
                elif openloader_available:
                    overlay_name = (
                        f"{base_name}_安全資源覆蓋_"
                        f"{os.path.splitext(jar_name)[0]}.zip")
                    overlay_name = re.sub(
                        r'[<>:"/\\|?*\x00-\x1f]', '_', overlay_name)
                    overlay_rel = f"config/openloader/resources/{overlay_name}"
                    written = self._write_openloader_resource_overlay(
                        combined, tmpdir, overlay_rel, inject)
                    if written:
                        openloader_overlay_count += 1
                        cfg_count += 1
                        overlaid_count = written
                        overlay_targets.append(overlay_rel)
                        inject = {
                            path: payload
                            for path, payload in inject.items()
                            if not path.replace('\\', '/').lower().startswith(
                                'assets/')
                        }

                if overlaid_count:
                    self.log(
                        f"  📦 {jar_name}: {overlaid_count} 個文字資源改由"
                        "安全覆蓋提供，原始模組 JAR 不修改。")
                if inject:
                    reasons = tuple(risk_reasons) or (
                        'client-safe-no-jar-rewrite',)
                    overlay_rel = ' + '.join(overlay_targets) or None
                    skipped_risky_jars.append(
                        (jar_rel, reasons, overlay_rel))
                    self.log(
                        f"  🛡️ {jar_name}: 客戶端安全模式禁止重包模組 JAR；"
                        f"{len(inject)} 個無安全覆蓋通道的項目保留原文。")
                continue

            # Paxi can load assets as a normal high-priority resource pack.
            # Keep pure language/manual changes out of source archives: this
            # avoids rewriting hundreds of JARs and never touches their code or
            # signatures. Advancement JSON can use a Paxi datapack; Patchouli
            # data stays in its source archive for older-version compatibility.
            if paxi_available and is_paxi_mod_archive:
                resource_safe, data_safe, _residual = split_paxi_safe_inject(
                    inject)
                original_asset_paths = {
                    path.replace('\\', '/')
                    for path in inject
                    if path.replace('\\', '/').lower().startswith('assets/')
                }
                overlay_assets = {
                    path: payload
                    for path, payload in resource_safe.items()
                    if path in original_asset_paths
                }
                original_advancement_paths = {
                    path.replace('\\', '/')
                    for path in inject
                    if (path.replace('\\', '/').lower().startswith('data/')
                        and '/advancements/' in path.replace('\\', '/').lower()
                        and path.replace('\\', '/').lower().endswith('.json'))
                }
                overlay_advancements = {
                    path: payload
                    for path, payload in data_safe.items()
                    if path in original_advancement_paths
                }
                if overlay_assets or overlay_advancements:
                    merged_count = 0
                    for path, payload in overlay_assets.items():
                        if merge_paxi_overlay_entry(
                                paxi_resource_overlay, path, payload):
                            merged_count += 1
                    for path, payload in overlay_advancements.items():
                        merge_paxi_overlay_entry(
                            paxi_data_overlay, path, payload)
                    offloaded_paths = (
                        original_asset_paths | original_advancement_paths)
                    inject = {
                        path: payload
                        for path, payload in inject.items()
                        if path.replace('\\', '/') not in offloaded_paths
                    }
                    suffix = (f"，合併 {merged_count} 個同路徑語言檔"
                              if merged_count else "")
                    details = []
                    if overlay_assets:
                        details.append(f"assets 文字 {len(overlay_assets)}")
                    if overlay_advancements:
                        details.append(f"advancement {len(overlay_advancements)}")
                    self.log(
                        f"  📦 {jar_name}: {', '.join(details)} 個資源"
                        f"改由 Paxi 安全覆蓋{suffix}")
                    if not inject:
                        continue

            safe_text_resource_only = is_safe_text_resource_only(inject)
            if high_risk_rewrite and safe_text_resource_only:
                self.log(
                    f"  🛡️ {jar_name}: 偵測為啟動期高風險 JAR，但本次只注入語言/手冊/Patchouli/成就文字資源，"
                    "允許安全重包；仍不修改 class/config/recipe。")
                high_risk_rewrite = False

            if (high_risk_rewrite and paxi_available
                    and is_paxi_mod_archive):
                resource_safe, data_safe, residual = split_paxi_safe_inject(inject)
                if resource_safe:
                    for path, payload in resource_safe.items():
                        merge_paxi_overlay_entry(
                            paxi_resource_overlay, path, payload)
                if data_safe:
                    for path, payload in data_safe.items():
                        merge_paxi_overlay_entry(
                            paxi_data_overlay, path, payload)
                if resource_safe or data_safe:
                    overlay_targets = []
                    if resource_safe:
                        overlay_targets.append(paxi_resource_rel)
                    if data_safe:
                        overlay_targets.append(paxi_data_rel)
                    self.log(
                        f"  🛡️ {jar_name}: 高風險 JAR 保持原檔，改由 Paxi 安全覆蓋"
                        f"（資源 {len(resource_safe)}、資料 {len(data_safe)}）")
                    inject = residual
                    skipped_risky_jars.append(
                        (jar_rel, risk_reasons, ' + '.join(overlay_targets)))
                    if not inject:
                        continue

            if high_risk_rewrite:
                if openloader_available:
                    overlay_name = (
                        f"{base_name}_高風險JAR覆蓋_"
                        f"{os.path.splitext(jar_name)[0]}.zip")
                    overlay_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', overlay_name)
                    overlay_rel = f"config/openloader/resources/{overlay_name}"
                    try:
                        written = self._write_openloader_resource_overlay(
                            combined, tmpdir, overlay_rel, inject)
                    except Exception as e:
                        written = 0
                        self.log(f"  ⚠️ {jar_name}: OpenLoader 覆蓋包產生失敗：{e}")
                    if written:
                        openloader_overlay_count += 1
                        cfg_count += 1
                        skipped_risky_jars.append((jar_rel, risk_reasons, overlay_rel))
                        self.log(
                            f"  🛡️ {jar_name}: 不重包高風險 JAR，改用 OpenLoader 覆蓋"
                            f"（{written} 個資源）")
                        continue
                if not any(item[0] == jar_rel for item in skipped_risky_jars):
                    skipped_risky_jars.append((jar_rel, risk_reasons, None))
                self.log(
                    f"  🛡️ {jar_name}: 跳過 JAR 重包"
                    f"（啟動期高風險：{', '.join(risk_reasons)}）")
                continue

            if not inject:
                self.log(f"  ⚠️ {jar_name}: 沒有可寫入的翻譯內容，略過重包")
                continue

            add_original_backup(jar_path, jar_rel)

            # 複製 JAR 並注入翻譯語言檔（完整重建，確保無重複 entry）
            try:
                if self._rebuild_jar_with_inject(jar_path, temp_jar, inject):
                    self.log(f"  🔏 {jar_name}: 已移除過期簽名（修改簽名 JAR 的標準做法，避免驗簽崩潰）")
            except Exception as e:
                self.log(f"  ⚠️ {jar_name} 處理失敗：{e}")
                continue

            # 將修改後的 JAR 依原始相對路徑加入合併包；
            # mods/foo.jar 保持在 mods/，resourcepacks/datapacks ZIP/JAR 保持在原資料夾。
            combined.write(temp_jar, jar_rel,
                           compress_type=zipfile.ZIP_STORED)
            jar_count += 1
            archive_kind = "模組 JAR" if is_mod_archive else "資源/資料包"
            self.log(f"  ✅ {jar_rel}（{archive_kind}，注入 {len(inject)} 個語言/書本檔）")

        # ── 散落 en_us.json（非 JAR 內）+ 附加檔案 (snbt/json/md) → 同一個合併包 ──
        written_cfg_paths = set()
        # 散落語言檔：不在 JAR 內、直接存在資料夾的 en_us.json
        for path in self.analyzed_loose:
            if self.stop_requested:
                break
            if not self._scope_allows_analyzed_path("loose", path):
                continue
            rel_path  = os.path.relpath(path, mc_dir).replace('\\', '/')
            zh_tw_rel = loose_zh_tw_path(rel_path)
            try:
                content = self.safe_read_file(path)
                if not content.strip():
                    continue
                data = load_lang_content(content, path, self._clean_json_text)
                # 若有既有 zh_tw 基底，只翻譯缺少的 key，再合併輸出
                zh_base = self.analyzed_loose_base.get(path)
                if self.process_mode_var.get() == "force":
                    zh_base = None
                merged = merge_with_existing_zh(
                    data, zh_base,
                    self.process_json_data, self._to_traditional,
                    value_needs_update=self._lang_value_needs_update)
                merged, dropped, format_dropped = self._filter_lang_output_entries(data, merged)
                if dropped:
                    extra = f"（含 {format_dropped:,} 個格式碼異常）" if format_dropped else ""
                    self.log(f"  ℹ️ {zh_tw_rel}: 略過 {dropped:,} 個尚未翻譯的語言項目{extra}")
                if not merged:
                    self.log(f"  ⚠️ {zh_tw_rel}: 沒有可用繁中譯文，略過輸出，避免空語言檔")
                    current_task += 1
                    self.update_progress(current_task, total_tasks)
                    continue
                combined.writestr(zh_tw_rel, lang_bytes(path, merged))
                cfg_count += 1
            except (json.JSONDecodeError, OSError) as e:
                self.log(f"⚠️ 略過 {zh_tw_rel}: {e}")
            current_task += 1
            self.update_progress(current_task, total_tasks)

        # SNBT / JSON / MD → 合併包
        for file_type, path in self.analyzed_extra:
            if self.stop_requested:
                break
            if not self._scope_allows_extra(file_type, path):
                continue
            rel_path  = os.path.relpath(path, mc_dir).replace('\\', '/')
            is_assets = rel_path.startswith('assets/')
            try:
                content = self.safe_read_file(path)
                if not content.strip():
                    continue
                if file_type == 'snbt':
                    translated = self.process_text_file(content)
                    # 語言檔模式（quests/lang/en_us.snbt）：輸出 zh_tw.snbt，不覆寫來源
                    is_lang, _lc, zh_snbt = ftbq_lang_snbt_role(rel_path)
                    out_rel = zh_snbt if is_lang else rel_path
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(out_rel, safe_utf8_bytes(translated))
                    if not is_assets:
                        cfg_count += 1
                        if out_rel.startswith('config/'):
                            written_cfg_paths.add(out_rel)
                    config_rel = defaultconfigs_mirror_path(out_rel)
                    if config_rel:
                        if config_rel not in written_cfg_paths:
                            combined.writestr(config_rel, safe_utf8_bytes(translated))
                            cfg_count += 1
                            written_cfg_paths.add(config_rel)
                elif file_type == 'json':
                    data = load_json_content(content, self._clean_json_text)
                    translated_bytes = json_bytes(
                        self.process_json_data(
                            data, preserve_technical_keys=True,
                            strict_context=self.strict_whitelist_var.get()))
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(rel_path, translated_bytes)
                    if not is_assets:
                        cfg_count += 1
                elif file_type == 'mns_json':
                    data = load_json_content(content, self._clean_json_text)
                    translated_bytes = json_bytes(
                        self.process_mmorpg_json_display_fields(data))
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(rel_path, translated_bytes)
                    cfg_count += 1
                elif file_type == 'apoth_names':
                    translated = self.process_apoth_names_cfg(content)
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(rel_path, safe_utf8_bytes(translated))
                    cfg_count += 1
                    self.log(f"  🏷️ Apotheosis 命名表已翻譯：{rel_path}")
                elif file_type == 'md':
                    translated = self.process_md_file(content)
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(rel_path, safe_utf8_bytes(translated))
                    if not is_assets:
                        cfg_count += 1
                elif file_type == 'lang':
                    data = parse_legacy_lang_content(content)
                    translated_bytes = lang_bytes(
                        path, self.process_json_data(
                            data, preserve_technical_keys=True,
                            strict_context=self.strict_whitelist_var.get()))
                    add_original_backup(path, f"quests_bak/{rel_path}")
                    combined.writestr(rel_path, translated_bytes)
                    if not is_assets:
                        cfg_count += 1
            except Exception as e:
                self.log(f"⚠️ 略過 {rel_path}: {e}")
            current_task += 1
            self.update_progress(current_task, total_tasks)

        zip_groups = {}
        for zip_path, internal_path in self.analyzed_zip_json:
            zip_groups.setdefault(zip_path, []).append(internal_path)

        sorted_zip_paths = sorted(zip_groups, key=lambda path: (
            os.path.relpath(path, mc_dir).replace('\\', '/').casefold(),
            os.path.relpath(path, mc_dir).replace('\\', '/')))
        for zip_path in sorted_zip_paths:
            internal_paths = zip_groups[zip_path]
            if self.stop_requested:
                break
            rel_path = os.path.relpath(zip_path, mc_dir).replace('\\', '/')
            temp_zip = os.path.join(tmpdir, f"patched_{len(zip_groups)}_{os.path.basename(zip_path)}")
            internal_set = set(internal_paths)
            patched_count = 0
            normalized_rel = rel_path.lower()
            paxi_source_kind = None
            if paxi_available:
                if normalized_rel.startswith('config/paxi/resourcepacks/'):
                    paxi_source_kind = 'resource'
                elif normalized_rel.startswith('config/paxi/datapacks/'):
                    paxi_source_kind = 'data'

            expected_prefix = (
                'assets/' if paxi_source_kind == 'resource' else 'data/')
            can_offload_paxi_source = (
                paxi_source_kind is not None
                and all(
                    path.replace('\\', '/').lower().startswith(expected_prefix)
                    and path.replace('\\', '/').lower().endswith('.json')
                    for path in internal_set
                ))
            if can_offload_paxi_source:
                target_overlay = (
                    paxi_resource_overlay
                    if paxi_source_kind == 'resource'
                    else paxi_data_overlay)
                try:
                    with zipfile.ZipFile(zip_path, 'r') as src_zip:
                        for internal_path in sorted(internal_set):
                            try:
                                content = self.safe_decode_bytes(
                                    src_zip.read(internal_path))
                                obj = load_json_content(
                                    content, self._clean_json_text)
                                translated = self.process_origin_json_display_fields(obj)
                                merge_paxi_overlay_entry(
                                    target_overlay, internal_path,
                                    json_bytes(translated))
                                patched_count += 1
                            except Exception as e:
                                self.log(
                                    f"  ⚠️ Paxi ZIP 內 JSON 略過 "
                                    f"{os.path.basename(zip_path)}::{internal_path}: {e}")
                except (OSError, zipfile.BadZipFile) as e:
                    self.log(f"⚠️ Paxi ZIP 處理失敗 {rel_path}: {e}")
                if patched_count:
                    self.log(
                        f"  📦 {os.path.basename(zip_path)}: "
                        f"{patched_count} 個內部 JSON 改由小型 Paxi 覆蓋，"
                        "略過來源 ZIP 重建")
                current_task += len(internal_paths)
                self.update_progress(current_task, total_tasks)
                continue

            try:
                with zipfile.ZipFile(zip_path, 'r') as src_zip, \
                     zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as dst_zip:
                    for item in src_zip.infolist():
                        data = src_zip.read(item)
                        if item.filename in internal_set and item.filename.lower().endswith('.json'):
                            try:
                                content = self.safe_decode_bytes(data)
                                obj = load_json_content(content, self._clean_json_text)
                                translated = self.process_origin_json_display_fields(obj)
                                data = json_bytes(translated)
                                patched_count += 1
                            except Exception as e:
                                self.log(f"  ⚠️ ZIP 內 JSON 略過 {os.path.basename(zip_path)}::{item.filename}: {e}")
                        dst_zip.writestr(item, data)
                if patched_count:
                    add_original_backup(zip_path, f"datapacks_bak/{rel_path}")
                    combined.write(temp_zip, rel_path, compress_type=zipfile.ZIP_STORED)
                    cfg_count += 1
                    self.log(f"  ✅ {os.path.basename(zip_path)}（翻譯 ZIP 內 Origins JSON {patched_count} 個）")
            except Exception as e:
                self.log(f"⚠️ ZIP 處理失敗 {rel_path}: {e}")
            current_task += len(internal_paths)
            self.update_progress(current_task, total_tasks)

        if self.scope_mod_lang_var.get():
            synthetic_entries = {
                'assets/mc_modpack_translator/lang/zh_tw.json':
                    json_bytes(self.SYNTHETIC_LANG_ZH_TW),
                'assets/additionalentityattributes/lang/zh_tw.json':
                    json_bytes(self.ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW),
            }
            if paxi_available:
                for path, payload in synthetic_entries.items():
                    merge_paxi_overlay_entry(
                        paxi_resource_overlay, path, payload)
                self.log(
                    "📄 合成 lang 已加入 Paxi 資源覆蓋；"
                    "不再寫入無效的遊戲根 assets/。")
            else:
                self.log(
                    "🛡️ 未偵測到 Paxi，略過合成 lang 根 assets/；"
                    "請改用資源包輸出，避免產生遊戲不會載入的檔案。")

        active_paxi_files = []
        obsolete_paxi_directories = []
        if paxi_resource_overlay:
            combined.writestr(
                paxi_resource_rel,
                build_paxi_overlay_zip(
                    resource_pack_format,
                    '自動翻譯：Paxi 安全資源覆蓋',
                    paxi_resource_overlay),
                compress_type=zipfile.ZIP_STORED)
            cfg_count += 1
            active_paxi_files.append(paxi_resource_rel)
            obsolete_paxi_directories.append(
                f'config/paxi/resourcepacks/{safe_overlay_base}')
            self.log(
                f"📦 Paxi 資源覆蓋：{len(paxi_resource_overlay)} 個語言／書本資源")
            combined.writestr(
                'config/paxi/resourcepack_load_order.json',
                paxi_load_order_bytes(
                    mc_dir, 'resourcepack_load_order.json', paxi_resource_name))

        if paxi_data_overlay:
            combined.writestr(
                paxi_data_rel,
                build_paxi_overlay_zip(
                    data_pack_format,
                    '自動翻譯：Paxi 安全資料覆蓋',
                    paxi_data_overlay),
                compress_type=zipfile.ZIP_STORED)
            cfg_count += 1
            active_paxi_files.append(paxi_data_rel)
            obsolete_paxi_directories.append(
                f'config/paxi/datapacks/{safe_overlay_base}')
            self.log(
                f"📦 Paxi 資料覆蓋：{len(paxi_data_overlay)} 個成就／手冊資料")
            combined.writestr(
                'config/paxi/datapack_load_order.json',
                paxi_load_order_bytes(
                    mc_dir, 'datapack_load_order.json', paxi_data_name))

        if active_paxi_files:
            combined.writestr(
                '_translator/PAXI_OVERLAY_MANIFEST.json',
                json_bytes({
                    'schema': 1,
                    'active_files': active_paxi_files,
                    'obsolete_directories': obsolete_paxi_directories,
                }))
            cleanup_lines = [
                '新版翻譯覆蓋已改用單一 ZIP；之後覆蓋同名 ZIP 即可完整更新。',
                '若曾套用舊版，請手動刪除下列同名資料夾，避免殘留翻譯：',
                *[f'- {path}' for path in obsolete_paxi_directories],
                '請勿刪除同層其他 Paxi 資料夾或 ZIP。',
            ]
            combined.writestr(
                '_translator/PAXI_OVERLAY_CLEANUP.txt',
                safe_utf8_bytes('\r\n'.join(cleanup_lines) + '\r\n'))

        runtime_warnings = java_runtime_warning_lines(
            getattr(self, '_java_runtime_compatibility_report', None))
        if client_safe_mode and runtime_warnings:
            combined.writestr(
                'TRANSLATOR_RUNTIME_WARNING.txt',
                safe_utf8_bytes(
                    '\r\n'.join(line.strip() for line in runtime_warnings)
                    + '\r\n'))
            self.log(
                "⚠️ Java 版本警告已寫入 TRANSLATOR_RUNTIME_WARNING.txt；"
                "翻譯器未修改遊戲或啟動器。")

        if backup_count:
            combined.writestr(
                '_backups/README_RESTORE.txt',
                ("此資料夾保存套用翻譯前的原始檔。\n"
                 "若要還原，請在已解壓此翻譯包的 Minecraft 根目錄執行 RESTORE_BACKUP.bat。\n"
                 "mods/ 會還原到 mods/；quests_bak 會依原本 config/defaultconfigs 路徑還原。\n").encode('utf-8'))
            combined.writestr(
                '_backups/RESTORE_BACKUP.bat',
                ("@echo off\r\n"
                 "setlocal\r\n"
                 "cd /d \"%~dp0\\..\"\r\n"
                 "if exist \"_backups\\mods\" xcopy /E /I /Y \"_backups\\mods\\*\" \"mods\\\"\r\n"
                 "if exist \"_backups\\quests_bak\" xcopy /E /I /Y \"_backups\\quests_bak\\*\" \".\\\"\r\n"
                 "echo Restore complete.\r\n"
                 "pause\r\n").encode('utf-8'))

        if skipped_risky_jars:
            if client_safe_mode:
                report_name = 'SKIPPED_MOD_JAR_REWRITES.txt'
                report_lines = [
                    "客戶端安全模式禁止重建或覆蓋任何 mods/*.jar。",
                    "可安全載入的 assets 與 data 文字已改用 Paxi/OpenLoader 覆蓋。",
                    "沒有安全覆蓋通道的內容保留原文，避免遊戲啟動崩潰。",
                    "",
                ]
            else:
                report_name = 'SKIPPED_HIGH_RISK_JARS.txt'
                report_lines = [
                    "以下 JAR 含 Mixin/CoreMod/AccessTransformer/ModLauncher 啟動期轉換，",
                    "為避免翻譯器重包後觸發啟動崩潰，已保留原始 JAR 不修改。",
                    "若模組包有 Paxi，assets 語言/書本與 advancement/Patchouli 資料會改用安全覆蓋。",
                    "若只有 OpenLoader，assets 文字會改寫到 config/openloader/resources/。",
                    "class 硬編碼與其他高風險內容仍維持原文。",
                    "",
                ]
            for item in skipped_risky_jars:
                rel_path, reasons = item[0], item[1]
                overlay_rel = item[2] if len(item) > 2 else None
                suffix = f" -> 安全覆蓋: {overlay_rel}" if overlay_rel else " -> 已跳過"
                report_lines.append(f"- {rel_path}  ({', '.join(reasons)}){suffix}")
            combined.writestr(
                report_name,
                safe_utf8_bytes("\r\n".join(report_lines) + "\r\n"))

    if ((jar_count > 0 or cfg_count > 0)
            and not self.stop_requested
            and not atomic_state.get('committed')):
        return ""

    if not self.stop_requested:
        if jar_count > 0 or cfg_count > 0:
            server_mode = getattr(self, "_server_mode", False)
            self.log(f"\n🎉 合併翻譯包生成完畢！")
            if server_mode:
                self.log("📦 伺服器翻譯包路徑（mods + config + defaultconfigs）：")
            else:
                self.log(
                    "📦 客戶端安全覆蓋路徑（Paxi/OpenLoader + config；"
                    "不含 mods/*.jar）：")
            self.log(f"   {combined_zip_path}")
            if skipped_risky_jars:
                if server_mode:
                    self.log(
                        f"   🛡️ 已保留 {len(skipped_risky_jars)} 個啟動期高風險 JAR 原檔，"
                        "安全文字資源已盡量改由 Paxi/OpenLoader 覆蓋；詳見報告")
                else:
                    self.log(
                        f"   🛡️ {len(skipped_risky_jars)} 個模組仍保留原始 JAR；"
                        "無安全通道的文字保留原文，詳見報告")
            if openloader_overlay_count:
                self.log(
                    f"   📦 已為 {openloader_overlay_count} 個高風險 JAR 產生 OpenLoader 安全覆蓋包")
            self.log(f"\n📋 使用方式：")
            if server_mode:
                self.log(f"   ① 停止伺服器")
                self.log(f"   ② 將 {os.path.basename(combined_zip_path)} 解壓到「伺服器根目錄」")
                self.log(f"      ZIP 內含 mods/（advancement 翻譯）、config/（任務書/命名表），覆蓋即套用")
                if backup_count:
                    self.log(f"   ③ ZIP 內含 _backups/ 原始備份 {backup_count} 個，可執行 RESTORE_BACKUP.bat 還原")
                if skipped_large_backup_count:
                    self.log(f"   ℹ️ 已略過 {skipped_large_backup_count} 個大型 JAR 備份（可在安全增量勾選「大型 JAR 備份」啟用）")
                self.log(f"   ④ 重新啟動伺服器 → 任務書/怪物命名/進度文字全員生效（玩家免裝補丁）")
                self.log(f"   ℹ️ mod 介面/tooltip/死亡訊息屬客戶端範疇，請玩家另外安裝客戶端翻譯包")
            else:
                self.log(f"   將 {os.path.basename(combined_zip_path)} 解壓到「遊戲根目錄」")
                self.log(
                    "   ZIP 僅含 config/defaultconfigs/Paxi/OpenLoader 安全覆蓋；"
                    "不含 mods/*.jar，也不修改 .class")
                if backup_count:
                    self.log(f"   ZIP 內含 _backups/ 原始備份 {backup_count} 個，可執行 RESTORE_BACKUP.bat 還原")
                self.log(f"   重啟遊戲即生效（不需啟用資源包）")
            mode_title, _ = self._output_mode_summary()
            self._last_output_path = combined_zip_path
            self._set_summary_card(
                "output", mode_title,
                f"語言：zh_tw\n已產生：{os.path.basename(combined_zip_path)}",
                self.C_SUCCESS)
            self._refresh_api_summary()
        else:
            self.log(f"\n⚠️ 未找到可翻譯的 JAR 或任務書，合併翻譯包未產生")
            self._set_summary_card(
                "output", "未產生",
                "沒有可寫入的翻譯內容",
                self.C_WARN)
            return ""
    return combined_zip_path if (
        not self.stop_requested
        and (jar_count > 0 or cfg_count > 0)
        and atomic_state.get('committed')
    ) else ""

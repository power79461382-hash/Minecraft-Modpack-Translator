import json
import os
import time
import zipfile

from core.json_loader import load_json_lenient


def run_coverage_task(self, mod_dir):
    # 伺服器目錄走專屬檢查：lang/書本/手冊是「客戶端」渲染的內容，
    # 伺服器端缺它們是刻意設計（dedicated server 只認 en_us，翻了玩家也看不到），
    # 只檢查伺服器真正同步給全員的東西：任務書、Apotheosis 命名表、advancement 顯示文字
    if os.path.exists(os.path.join(mod_dir, 'server.properties')):
        self._coverage_task_server(mod_dir)
        return
    self.log("\n--- 🔍 翻譯覆蓋檢查（直接掃描 mods 成品，不需開啟遊戲） ---")
    mods_dir = os.path.join(mod_dir, 'mods')
    if not os.path.isdir(mods_dir):
        mods_dir = mod_dir
    rows = []          # (未翻總數, jar 名, 統計 dict, 樣本)
    targets = [(f, os.path.join(mods_dir, f))
               for f in sorted(os.listdir(mods_dir)) if f.lower().endswith('.jar')]
    # 內建資源包（含 paxi 載入的）也要檢查：它們的 en_us 覆寫會蓋過翻譯
    for rp_rel in ('resourcepacks', os.path.join('config', 'paxi', 'resourcepacks')):
        rp_dir_p = os.path.join(mod_dir, rp_rel)
        if os.path.isdir(rp_dir_p):
            for zn in sorted(os.listdir(rp_dir_p)):
                if zn.lower().endswith('.zip'):
                    targets.append((rp_rel.replace(os.sep, '/') + '/' + zn,
                                    os.path.join(rp_dir_p, zn)))
    for idx, (jar_name, jar_path) in enumerate(targets):
        if self.stop_requested:
            break
        self.set_current_item(f"覆蓋檢查 {idx + 1}/{len(targets)}：{jar_name}")
        stats = {"lang 缺 key": 0, "lang 英文殘留": 0, "lang 混英值": 0,
                 "書本缺 zh_tw": 0, "書本英文殘留": 0, "手冊缺 zh_tw": 0}
        samples = []
        try:
            with zipfile.ZipFile(jar_path) as z:
                lower = {i.filename.lower(): i.filename for i in z.infolist()}
                for fn_l, fn in lower.items():
                    if fn_l.endswith('/lang/en_us.json') and fn_l.startswith('assets/'):
                        zh_l = fn_l[:-len('en_us.json')] + 'zh_tw.json'
                        try:
                            en = load_json_lenient(
                                self.safe_decode_bytes(z.read(fn)), self._clean_json_text)
                            zh = (load_json_lenient(
                                self.safe_decode_bytes(z.read(lower[zh_l])), self._clean_json_text)
                                  if zh_l in lower else {})
                        except (json.JSONDecodeError, KeyError, OSError):
                            continue
                        if not isinstance(en, dict):
                            continue
                        for k, v in en.items():
                            if not isinstance(v, str) or not self.should_translate(v):
                                continue
                            zv = zh.get(k) if isinstance(zh, dict) else None
                            if not isinstance(zv, str) or not zv.strip():
                                stats["lang 缺 key"] += 1
                                if len(samples) < 5:
                                    samples.append(f"缺 key {k} = {v[:50]!r}")
                            elif (len(zv) >= 8 and not self._RE_CJK_CHAR.search(zv)
                                    and self._RE_EN_WORD.search(zv)):
                                stats["lang 英文殘留"] += 1
                                if len(samples) < 5:
                                    samples.append(f"英文值 {k} = {zv[:50]!r}")
                            elif (self._RE_CJK_CHAR.search(zv)
                                    and self._RE_EN_WORD.search(
                                        self._RE_FORMAT.sub('', zv))):
                                # 混英值（如 Mana魔力）：免費引擎保留自創詞的產物，
                                # 用 AI 引擎重跑會在階段二.八自動升級
                                stats["lang 混英值"] += 1
                    elif ('/book/' in fn_l and '/en_us/' in fn_l
                            and fn_l.endswith('.txt') and fn_l.startswith('assets/')):
                        zh_l = fn_l.replace('/en_us/', '/zh_tw/')
                        if zh_l not in lower:
                            stats["書本缺 zh_tw"] += 1
                        else:
                            try:
                                txt = self.safe_decode_bytes(z.read(lower[zh_l]))
                            except (OSError, KeyError):
                                continue
                            if (self._RE_EN_WORD.search(txt)
                                    and not self._RE_CJK_CHAR.search(txt)):
                                stats["書本英文殘留"] += 1
                                if len(samples) < 5:
                                    samples.append(f"書本英文 {os.path.basename(fn_l)}")
                    elif ('/patchouli_books/' in fn_l and '/en_us/' in fn_l
                            and fn_l.endswith('.json')):
                        zh_l = fn_l.replace('/en_us/', '/zh_tw/')
                        if zh_l not in lower:
                            stats["手冊缺 zh_tw"] += 1
        except (zipfile.BadZipFile, OSError):
            continue
        total = sum(stats.values())
        if total:
            rows.append((total, jar_name, stats, samples))

    # ── 全資料夾：config 文字系統（不只 mods） ──
    folder_lines = []
    quests_dir = os.path.join(mod_dir, 'config', 'ftbquests', 'quests')
    if os.path.isdir(quests_dir):
        total_q = zh_q = 0
        for r_dir, _d, fs in os.walk(quests_dir):
            for fname in fs:
                if not fname.lower().endswith('.snbt'):
                    continue
                try:
                    txt = self.safe_read_file(os.path.join(r_dir, fname))
                except OSError:
                    continue
                if not self._RE_QUOTED_STR.search(txt):
                    continue
                total_q += 1
                if self._RE_CJK_CHAR.search(txt):
                    zh_q += 1
        folder_lines.append(f"📖 FTB 任務書：{zh_q}/{total_q} 檔已含中文")
    skills_dir = os.path.join(mod_dir, 'config', 'puffish_skills')
    if os.path.isdir(skills_dir):
        need = done = 0
        for r_dir, _d, fs in os.walk(skills_dir):
            for fname in fs:
                if fname not in ('category.json', 'definitions.json'):
                    continue
                try:
                    txt = self.safe_read_file(os.path.join(r_dir, fname))
                except OSError:
                    continue
                if not self._RE_EN_WORD.search(txt) and not self._RE_CJK_CHAR.search(txt):
                    continue
                need += 1
                if self._RE_CJK_CHAR.search(txt):
                    done += 1
        folder_lines.append(f"🌳 Pufferfish 天賦樹：{done}/{need} 檔已含中文")
    names_path = os.path.join(mod_dir, 'config', 'apotheosis', 'names.cfg')
    if os.path.exists(names_path):
        try:
            content = self.safe_read_file(names_path)
            ents = [e for _l, e in self._iter_apoth_name_lines(content) if e]
            en_left = [e for e in ents
                       if not self._RE_CJK_CHAR.search(e) and self._RE_EN_WORD.search(e)]
            folder_lines.append(f"🏷️ Apotheosis 命名表：{len(ents) - len(en_left)}/{len(ents)} 條已翻")
        except OSError:
            pass
    for line in folder_lines:
        self.log(line)

    rows.sort(reverse=True)
    grand = sum(r[0] for r in rows)
    report_dir = os.path.join(os.getcwd(), "Failed Items")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(
        report_dir, time.strftime("%Y%m%d_%H%M%S") + "_coverage_report.txt")
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(f"翻譯覆蓋檢查報告  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"掃描目錄：{mods_dir}\n")
            f.write(f"掃描對象（JAR+資源包）：{len(targets)}，有缺漏的：{len(rows)}，未翻項目合計：{grand:,}\n")
            for line in folder_lines:
                f.write(line + "\n")
            f.write("\n")
            for total, jar_name, stats, samples in rows:
                detail = "、".join(f"{k} {v}" for k, v in stats.items() if v)
                f.write(f"[{total:>5}] {jar_name}\n        {detail}\n")
                for s in samples:
                    f.write(f"        · {s}\n")
    except OSError as e:
        self.log(f"⚠️ 覆蓋報告寫入失敗：{e}")
        report_path = None

    self.set_current_item("覆蓋檢查完成")
    if not rows:
        self.log("✅ 覆蓋檢查：所有 JAR 的語言檔/書本/手冊都已有翻譯，沒有發現缺漏。")
    else:
        self.log(f"📊 覆蓋檢查：{len(targets)} 個 JAR/資源包中 {len(rows)} 個有缺漏，合計 {grand:,} 項：")
        for total, jar_name, stats, _ in rows[:10]:
            detail = "、".join(f"{k} {v}" for k, v in stats.items() if v)
            self.log(f"   [{total:>5}] {jar_name}：{detail}")
        if len(rows) > 10:
            self.log(f"   ... 還有 {len(rows) - 10} 個，完整清單見報告")
        self.log("💡 重新「分析檔案 → 開始翻譯 → 套用」即可補上缺漏項目。")
    if report_path:
        self.log(f"🧾 完整報告：{report_path}")


def run_coverage_task_server(self, mod_dir):
    """伺服器專屬覆蓋檢查：只看「server 同步給全員」的內容。"""
    self.log("\n--- 🔍 翻譯覆蓋檢查【伺服器模式】 ---")
    self.log("ℹ️ 模組語言檔/書本/手冊是客戶端渲染的內容，伺服器端不檢查也不需要翻"
             "（翻了玩家也看不到，請玩家安裝客戶端翻譯包）。")
    findings = []

    # ① FTB 任務書：章節 snbt 是否含中文
    quests_dir = os.path.join(mod_dir, 'config', 'ftbquests', 'quests')
    if os.path.isdir(quests_dir):
        total = zh = 0
        english_files = []
        for root_dir, _dirs, files in os.walk(quests_dir):
            for fname in files:
                if not fname.lower().endswith('.snbt'):
                    continue
                p = os.path.join(root_dir, fname)
                try:
                    txt = self.safe_read_file(p)
                except OSError:
                    continue
                # 只統計含可翻文字的檔（title/description 等引號內容）
                if not self._RE_QUOTED_STR.search(txt):
                    continue
                total += 1
                if self._RE_CJK_CHAR.search(txt):
                    zh += 1
                else:
                    english_files.append(os.path.relpath(p, mod_dir))
        self.log(f"📖 FTB 任務書：{zh}/{total} 個檔案已含中文")
        if english_files:
            findings.append(("任務書整檔英文", english_files[:20]))

    # ② Apotheosis 命名表：剩多少純英文條目
    names_path = os.path.join(mod_dir, 'config', 'apotheosis', 'names.cfg')
    if os.path.exists(names_path):
        try:
            content = self.safe_read_file(names_path)
            entries_en = [e for _line, e in self._iter_apoth_name_lines(content)
                          if e and not self._RE_CJK_CHAR.search(e)
                          and self._RE_EN_WORD.search(e)]
            entries_all = [e for _line, e in self._iter_apoth_name_lines(content) if e]
            self.log(f"🏷️ Apotheosis 命名表：{len(entries_all) - len(entries_en)}/"
                     f"{len(entries_all)} 條已翻（其餘多為引擎拒翻的人名，重跑可補）")
            if entries_en:
                findings.append(("命名表英文條目", entries_en[:20]))
        except OSError:
            pass

    # ③ mod JAR 內 advancement 顯示文字：仍是英文字面值的數量
    mods_dir = os.path.join(mod_dir, 'mods')
    adv_en = 0
    adv_samples = []
    if os.path.isdir(mods_dir):
        jar_names = sorted(f for f in os.listdir(mods_dir) if f.lower().endswith('.jar'))
        for idx, jar_name in enumerate(jar_names):
            if self.stop_requested:
                break
            self.set_current_item(f"伺服器覆蓋檢查 {idx + 1}/{len(jar_names)}：{jar_name}")
            try:
                with zipfile.ZipFile(os.path.join(mods_dir, jar_name)) as z:
                    for info in z.infolist():
                        fn_l = info.filename.lower()
                        if not (fn_l.startswith('data/') and '/advancements/' in fn_l
                                and fn_l.endswith('.json')):
                            continue
                        try:
                            data = load_json_lenient(
                                self.safe_decode_bytes(z.read(info.filename)), self._clean_json_text)
                        except (json.JSONDecodeError, OSError):
                            continue
                        display = data.get('display') if isinstance(data, dict) else None
                        if not isinstance(display, dict):
                            continue
                        for key in ('title', 'description'):
                            val = display.get(key)
                            txt = val.get('text') if isinstance(val, dict) else (
                                val if isinstance(val, str) else None)
                            if (isinstance(txt, str) and self._RE_EN_WORD.search(txt)
                                    and not self._RE_CJK_CHAR.search(txt)
                                    and self.should_translate(txt)):
                                adv_en += 1
                                if len(adv_samples) < 10:
                                    adv_samples.append(f"{jar_name} :: {txt[:50]}")
            except (zipfile.BadZipFile, OSError):
                continue
        self.log(f"🏆 advancement 顯示文字：{adv_en} 筆仍是英文字面值"
                 + ("（重跑伺服器翻譯可補）" if adv_en else ""))
        if adv_samples:
            findings.append(("advancement 英文顯示文字", adv_samples))

    self.set_current_item("伺服器覆蓋檢查完成")
    if findings:
        report_dir = os.path.join(os.getcwd(), "Failed Items")
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(
            report_dir, time.strftime("%Y%m%d_%H%M%S") + "_server_coverage.txt")
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(f"伺服器覆蓋檢查  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("（lang/書本/手冊為客戶端內容，不在伺服器檢查範圍）\n\n")
                for title, items in findings:
                    f.write(f"== {title}（{len(items)} 例）==\n")
                    for it in items:
                        f.write(f"  · {it}\n")
                    f.write("\n")
            self.log(f"🧾 報告：{report_path}")
        except OSError as e:
            self.log(f"⚠️ 報告寫入失敗：{e}")
    else:
        self.log("✅ 伺服器端該翻的內容（任務書/命名表/advancement）覆蓋良好。")

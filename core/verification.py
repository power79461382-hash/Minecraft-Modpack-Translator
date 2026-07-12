import os
import time

from translation_cache import cache_snapshot


def verify_translations(self, unique_strings):
    """
    分析翻譯完整性與格式碼保留情況，記錄至日誌。
    - 未翻譯率過高 (>30% 長字串)：記錄警告後繼續輸出。
    - 格式碼異常：停止輸出，避免壞格式進入遊戲造成崩潰。
    - 問題在可接受範圍：僅記錄警告，自動繼續。
    回傳 True 代表繼續輸出，False 代表使用者取消。
    """
    self.log("\n--- 階段 2.5：翻譯品質驗證 ---")
    total = len(unique_strings)
    if total == 0:
        self.log("ℹ️  無可翻譯字串，跳過驗證。")
        return True

    # ── 統計未翻譯 ──
    # 快路徑：已在快取的（絕大多數）直接視為已翻譯，
    # 只對 miss 走完整 get_translation（字典/記憶池/變體查找）——
    # 上萬字串逐條 get_translation 會讓驗證階段卡數十秒
    cached_values = cache_snapshot(self.translation_cache, unique_strings)

    def cached_translation(source):
        translated = cached_values.get(source)
        if self._is_valid_trad_translation(source, translated):
            return translated
        return None

    def _count(lst):
        d = {}
        for x in lst: d[x] = d.get(x, 0) + 1
        return d

    # 單次掃描同時統計 miss 與格式碼，避免十幾萬字串重複驗證。
    untranslated = []
    fmt_issues = []
    for index, s in enumerate(unique_strings):
        if index % 1024 == 0:
            if (getattr(self, "stop_requested", False)
                    or getattr(self, "pause_requested", False)):
                self.log("ℹ️ 品質驗證已取消，保留目前翻譯進度。")
                return False
            if index:
                update_status = getattr(self, "set_current_item", None)
                if callable(update_status):
                    update_status(
                        f"階段 2.5：品質驗證 {index:,}/{total:,}...")

        t = cached_translation(s)
        if t is None:
            if (not self._RE_CJK_CHAR.search(s)
                    and self.should_translate(s)
                    and self.get_translation(s) == s):
                untranslated.append(s)
            continue
        # 只比對「會影響遊戲運作」的格式碼（排除羅馬數字等裝飾性 token），
        # 與快取載入層 _critical_formats 的口徑一致，避免永遠清不掉的假警報
        orig_fmts = self._critical_format_tokens(s)
        if not orig_fmts:
            continue
        if _count(orig_fmts) != _count(self._critical_format_tokens(t)):
            fmt_issues.append((s, t))

    # 長字串（≥15字）：句子/說明文，未翻譯較可能是 API 失敗
    nt_long = sorted(
        [s for s in untranslated if len(s) >= 15], key=len, reverse=True)
    # 短字串（<15字）：物品名/專有名詞，未翻譯較可能是刻意跳過
    nt_short = [s for s in untranslated if len(s) < 15]

    # ── 輸出統計 ──
    translated_n  = total - len(untranslated)
    rate          = (translated_n / total * 100) if total > 0 else 0.0
    failure_rate  = (len(nt_long)  / total * 100) if total > 0 else 0.0

    self.log(f"📊 總計：{total:,} 個可翻譯字串")
    self.log(f"✅ 成功翻譯：{translated_n:,} 個 ({rate:.1f}%)")

    if nt_long:
        self.log(f"⚠️  疑似翻譯失敗（長字串 ≥15字）：{len(nt_long):,} 個 ({failure_rate:.1f}%)")
        for s in nt_long[:5]:
            self.log(f"   · {s[:70].replace(chr(10), ' ')!r}")
        if len(nt_long) > 5:
            self.log(f"   ... 還有 {len(nt_long) - 5} 個未顯示")

    if nt_short:
        self.log(f"ℹ️  短字串未翻譯（可能為專有名詞）：{len(nt_short):,} 個")

    if fmt_issues:
        self.log(f"⚠️  格式碼異常（顏色碼/占位符數量不一致）：{len(fmt_issues):,} 個")
        for orig, trans in fmt_issues[:3]:
            self.log(f"   原文：{orig[:55].replace(chr(10), ' ')!r}")
            self.log(f"   譯文：{trans[:55].replace(chr(10), ' ')!r}")
        if len(fmt_issues) > 3:
            self.log(f"   ... 還有 {len(fmt_issues) - 3} 個未顯示")

    failed_report = self._write_failed_items_report(untranslated, fmt_issues)
    if failed_report:
        self.log(f"🧾 已輸出失敗項目報告：{failed_report}")

    # ── 判斷嚴重性 ──
    need_confirm = bool(fmt_issues) or failure_rate > 30

    if not nt_long and not fmt_issues:
        self.log("✅ 驗證通過！翻譯完整且格式碼無異常，即將輸出檔案...")
        return True

    if not need_confirm:
        # 少量失敗（≤30%長字串）且無格式碼問題 → 警告但自動繼續
        self.log(f"⚠️  驗證完成，少量字串未翻譯 ({failure_rate:.1f}%)，"
                 f"在可接受範圍內，繼續輸出...")
        return True

    if fmt_issues:
        self.log("🛑 驗證發現格式碼/占位符異常，已停止輸出以避免遊戲崩潰。")
        self.log("   詳細項目已寫入 Failed Items；修正或重新翻譯後再輸出。")
        return False

    self.log(f"⚠️  驗證發現大量長字串未翻譯 ({failure_rate:.1f}%)，"
             "但格式碼正常；為避免背景確認框卡住，已自動繼續輸出。")
    return True


def write_failed_items_report(self, untranslated, fmt_issues, context="translation_validation"):
    if not untranslated and not fmt_issues:
        return ""
    try:
        os.makedirs(self.failed_items_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.failed_items_dir, f"{stamp}_{context}.txt")
        lines = [
            "Minecraft 模組翻譯器 - Failed Items",
            f"時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"未翻譯：{len(untranslated)}",
            f"格式碼異常：{len(fmt_issues)}",
            "",
        ]
        if untranslated:
            lines.append("[未翻譯 / 保留原文]")
            for idx, src in enumerate(sorted(untranslated, key=lambda s: (-len(s), s)), 1):
                display = src.replace("\n", "\\n")
                lines.append(f"{idx}. {display}")
            lines.append("")
        if fmt_issues:
            lines.append("[格式碼 / 佔位符異常]")
            for idx, (orig, trans) in enumerate(fmt_issues, 1):
                lines.append(f"{idx}. 原文：{orig.replace(chr(10), chr(92) + 'n')}")
                lines.append(f"   譯文：{trans.replace(chr(10), chr(92) + 'n')}")
            lines.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except OSError as e:
        self.log(f"⚠️ 無法寫入 Failed Items 報告：{e}")
        return ""

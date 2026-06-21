# -*- coding: utf-8 -*-
"""一次性修復腳本：
1. 從 中文_模組語言包.zip 的 _backups/ 還原整合包 mods/ 內所有被翻譯弄壞的
   advancement JSON（requirements 被翻成中文 → 6,428 個進度載入失敗 → 死亡時崩潰）。
2. 將 alexsmobs 動物詞典的 zh_tw 書本檔重排為官方 zh_cn 格式
   （每行文字後跟 <NEWLINE> 標記），修復文字超框與重疊。
原始 JAR 不另外備份——備份已存在於 中文_模組語言包.zip 的 _backups/。
"""
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile

BACKUP_ZIP = r"C:\Users\vm060\Desktop\中文_模組語言包.zip"
# 從整合包原始 .rar 解出的乾淨 JAR（中文_模組語言包.zip 的 _backups 已被前一輪
# 翻譯污染，不能當原始來源）
PRISTINE_DIR = r"C:\Users\vm060\Desktop\翻譯\crash_analysis\pristine"
MODS_DIR = (r"C:\Users\vm060\Desktop\mc\.minecraft\versions"
            r"\Cisco's Fantasy Medieval RPG [Ultimate] (1)\mods")

RE_ADV = re.compile(r'^data/[^/]+/advancements/.+\.json$', re.IGNORECASE)
RE_BOOK_ZH = re.compile(r'^assets/.+/book/.+/zh_tw/.+\.txt$', re.IGNORECASE)
RE_CJK = re.compile(r'[㐀-鿿]')


def display_width(text):
    width = 0.0
    for ch in text:
        width += 0.55 if ord(ch) < 128 else 1.0
    return width


def wrap_cjk(text, max_width=16.0):
    break_after = set("，。！？；：、,.!?;:)]}）】」』")
    lines, buf, width = [], "", 0.0
    for ch in text:
        w = 0.55 if ord(ch) < 128 else 1.0
        if buf and width + w > max_width and ch not in break_after:
            lines.append(buf.rstrip())
            buf, width = "", 0.0
        buf += ch
        width += w
        if width >= max_width and ch in break_after:
            lines.append(buf.rstrip())
            buf, width = "", 0.0
    if buf:
        lines.append(buf.rstrip())
    return [l for l in lines if l]


def reflow_book_txt(content):
    """重排為官方 zh_cn 格式：開頭的 <NEWLINE> 保留（圖片佔位），
    其後所有文字串成連續段落重新以 16 全形字斷行，每行後跟一個 <NEWLINE>。
    此轉換具冪等性：對已重排的檔案再跑一次，輸出不變。"""
    lines = [l.strip() for l in content.replace('\r\n', '\n').split('\n') if l.strip()]
    leading = 0
    while leading < len(lines) and lines[leading] == "<NEWLINE>":
        leading += 1
    text_lines = [l for l in lines[leading:] if l != "<NEWLINE>"]
    if not text_lines:
        return "\r\n".join(lines)
    joined = "".join(text_lines) if RE_CJK.search("".join(text_lines)) \
        else " ".join(text_lines)
    out = ["<NEWLINE>"] * leading
    for line in wrap_cjk(joined):
        out.append(line)
        out.append("<NEWLINE>")
    if out and out[-1] == "<NEWLINE>" and len(out) > leading:
        out.pop()
    return "\r\n".join(out)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if not os.path.exists(BACKUP_ZIP):
        print(f"找不到備份包：{BACKUP_ZIP}")
        sys.exit(1)

    pristine_jars = {
        name: os.path.join(PRISTINE_DIR, name)
        for name in os.listdir(PRISTINE_DIR)
        if name.lower().endswith('.jar')
    }
    print(f"乾淨原始 JAR：{len(pristine_jars)} 個")

    repaired_jars = 0
    restored_files = 0
    reflowed_books = 0
    skipped = 0

    for jar_name in sorted(os.listdir(MODS_DIR)):
        if not jar_name.lower().endswith('.jar'):
            continue
        jar_path = os.path.join(MODS_DIR, jar_name)
        pristine_path = pristine_jars.get(jar_name)

        with zipfile.ZipFile(jar_path, 'r') as cur:
            cur_infos = cur.infolist()
            cur_crc = {i.filename: i.CRC for i in cur_infos}
            adv_names = [i.filename for i in cur_infos if RE_ADV.match(i.filename)]
            book_names = [i.filename for i in cur_infos if RE_BOOK_ZH.match(i.filename)]

        if not adv_names and not book_names:
            continue

        # 取得乾淨 jar 內各 advancement 的內容（CRC 不同才需要還原）
        orig_adv = {}
        if pristine_path and adv_names:
            with zipfile.ZipFile(pristine_path, 'r') as orig:
                orig_crc = {i.filename: i.CRC for i in orig.infolist()}
                for name in adv_names:
                    if name in orig_crc and orig_crc[name] != cur_crc.get(name):
                        orig_adv[name] = orig.read(name)

        # 需要重排的書本檔
        book_fix = {}
        with zipfile.ZipFile(jar_path, 'r') as cur:
            for name in book_names:
                text = cur.read(name).decode('utf-8', 'replace')
                if not RE_CJK.search(text):
                    continue
                fixed = reflow_book_txt(text)
                if fixed.strip() != text.strip():
                    book_fix[name] = fixed.encode('utf-8')

        replace = dict(orig_adv)
        replace.update(book_fix)
        if not replace:
            skipped += 1
            continue

        # 重建 JAR：替換損壞條目，其餘原樣
        fd, tmp_path = tempfile.mkstemp(suffix='.jar', dir=MODS_DIR)
        os.close(fd)
        try:
            with zipfile.ZipFile(jar_path, 'r') as src, \
                 zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    data = replace.get(item.filename)
                    if data is None:
                        data = src.read(item.filename)
                    dst.writestr(item, data)
            os.replace(tmp_path, jar_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            print(f"  ⚠️ {jar_name} 修復失敗：{e}")
            continue

        repaired_jars += 1
        restored_files += len(orig_adv)
        reflowed_books += len(book_fix)
        detail = []
        if orig_adv:
            detail.append(f"還原 advancement {len(orig_adv)} 個")
        if book_fix:
            detail.append(f"重排書本 {len(book_fix)} 個")
        print(f"  ✅ {jar_name}：{'、'.join(detail)}")

    print()
    print(f"完成：修復 {repaired_jars} 個 JAR，"
          f"還原 advancement {restored_files} 個，重排書本檔 {reflowed_books} 個")


if __name__ == "__main__":
    main()

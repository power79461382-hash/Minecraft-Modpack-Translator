# Minecraft Modpack Translator

Minecraft Modpack Translator 是一個面向 Minecraft 大型整合包的 Windows 翻譯工具。它會掃描 Forge、NeoForge、Fabric 整合包中的模組 JAR、語言檔、任務檔、手冊、書本與部分資料檔，將可安全覆蓋的玩家可見文字轉成繁體中文 `zh_tw`。

本專案的目標不是只翻譯單一 `en_us.json`，而是盡可能處理整合包常見的多來源文字：模組語言檔、Patchouli 手冊、FTB Quests / KubeJS 任務、advancement、OpenLoader / Paxi 覆蓋資料、Origins JSON、Alex's Mobs 類型書本、Markdown / SNBT / 部分設定文字，以及低風險硬編碼 tooltip。



## v1.2.13
- LibreTranslate **一鍵啟動／關閉本機服務**（介面按鈕）
- 優先 Docker 容器 `mc-libretranslate`；無 Docker 時可自動 `pip install libretranslate` 並以本機行程啟動
## v1.2.13
- 新增 **LibreTranslate（本機）** 免費供應商選項（預設 `http://127.0.0.1:5000`）
- 本機 LibreTranslate 支援批次 `/translate`、高併發（最多 16），並在僅有簡中時自動 OpenCC 轉繁
- 啟動方式範例：`docker run -d -p 5000:5000 libretranslate/libretranslate`
## 直接下載 Windows EXE

一般使用者不需要安裝 Python。請到 GitHub Releases 下載最新版：

- 目前最新版：**v1.2.13**（2026-09-10）
- 下載頁：[Releases / 最新版](https://github.com/power79461382-hash/Minecraft-Modpack-Translator/releases/latest)
- 直接下載：[MinecraftTranslatorGUI.exe（v1.2.6）](https://github.com/power79461382-hash/Minecraft-Modpack-Translator/releases/download/v1.2.13/MinecraftTranslatorGUI.exe)
- 檔案名稱：`MinecraftTranslatorGUI.exe`
- 系統需求：Windows 10/11，建議放在可寫入的資料夾中執行
- 使用方式：下載後直接雙擊啟動，選擇整合包資料夾，按「分析檔案」後再開始翻譯

第一次執行時，Windows Defender 或 SmartScreen 可能會提示未簽章程式。此 EXE 是由本專案源碼使用 PyInstaller 打包（含 OpenCC 簡轉繁詞典），不包含個人 API 設定檔、翻譯快取、輸出 ZIP 或 Minecraft 模組包。

## 目前能做到什麼

- 自動分析整合包根目錄、`mods/`、`config/`、`defaultconfigs/`、resource pack、OpenLoader 與 Paxi 覆蓋目錄。
- 從 JAR 內擷取 `assets/<modid>/lang/en_us.json`、`zh_cn.json`、legacy `.lang`，產生或補齊 `zh_tw`；僅有 `zh_cn`、沒有 `en_us` 的模組會走簡→繁，避免繁中包幾乎空白。
- 翻譯 Patchouli 與相似書本格式，並避免把頁面巨集、連結、圖片、recipe 標記翻壞。
- 翻譯 FTB Quests / KubeJS 任務文字，同時保護 task type、item id、NBT、image macro、pagebreak 與條件語法。
- 翻譯部分 advancement、Origins/OpenLoader JSON、Apotheosis 命名表、Markdown 與 SNBT 字串。
- 支援「補缺」、「跳過高命中項」、「強制重翻」與「只補缺漏 + 模組更新偵測」。
- 使用全域翻譯記憶池與 SQLite / shelve 快取，降低重複翻譯與重複掃描成本。
- 非 AI 翻譯鏈目前以 **GTX-only** 為主（Bing 免費 auth 已失效、Azure 易 429）；以大批次提高詞/秒，遇限流自動退避重試。
- 支援多種 API / OpenAI-compatible 模型，用於需要更好語意品質的付費或免費額度翻譯。
- 內建格式保護：Minecraft 顏色碼、placeholder、指令、URL、registry id、FTB/Patchouli token、數值單位與 Unicode surrogate 會被過濾或遮罩。
- 輸出前會驗證翻譯量，避免產生空 `zh_tw.json` 或看似完成但內容沒有翻譯的語言包。

## 輸出策略

GUI 固定使用單一「JAR 直接翻譯」模式。輸出 ZIP 會把 `zh_tw` 語言、書本與安全顯示資源直接注入重建後的 `mods/*.jar`、版本 JAR，以及整合包原有的 resource/data ZIP；不產生新的資源包或 Paxi 依賴。

套用時先關閉遊戲與啟動器，再把輸出 ZIP 整包解壓到實例根目錄並覆蓋。設定與任務檔原始版本保存在 `_backups/`；大型 JAR 備份可由 GUI 選項啟用。

為了降低遊戲崩潰風險，工具會跳過或降級處理高風險內容：

- 不修改 `.class` 啟動邏輯、coremod、AccessTransformer 或 ModLauncher service 類型風險檔。
- 已簽名 JAR 會移除舊簽名 metadata，避免 JVM 驗簽失敗。
- 不輸出空語言檔；若沒有可用翻譯內容，會略過該 JAR 或取消輸出。
- FTB Quests 的合法 ID、任務型別、物品 ID、NBT 與條件語法不會翻譯。
- 書本與手冊保留圖片、recipe、entity、advancement、link、pagebreak、換行與版面標記。

## 翻譯範圍

目前主要覆蓋這些來源：

- 模組語言檔：`assets/*/lang/en_us.json`、`zh_cn.json`、`*.lang`
- Patchouli 書本：`data/*/patchouli_books/**`
- Alex's Mobs / 自訂指南書：`assets/*/book/**`、JSON/TXT 書本頁
- FTB Quests：`config/ftbquests/quests/**/*.snbt`
- KubeJS / 任務 JSON：可解析的 `json`、`snbt`、部分 `md`
- Advancement：`data/*/advancements/**/*.json`
- OpenLoader / Paxi 資源與資料包
- Origins / datapack 類 JSON
- 部分硬編碼 tooltip：只針對低風險 item/block class 補丁

## 已知限制

- 有些文字由模組在執行期動態組合，不一定存在於語言檔或資料檔中。
- 高風險 JAR 只注入語言、書本等文字資源；Mixin、AccessTransformer、coremod 與其他啟動 class 不修改。
- 低風險 item/block tooltip class 會自動修補；高風險啟動 class 仍保留原文。
- 書本、任務、GUI 的格式差異很大，無法安全解析的檔案會被略過並記錄。
- Bing / Azure / GTX 免 Key 或免費額度翻譯可能被限流，速度會依網路與端點狀態波動。

## 使用方式

啟動 GUI：

```powershell
python .\MinecraftTranslatorGUI.py
```

基本流程：

1. 選擇 Minecraft 整合包根目錄。
2. 選擇輸出資料夾與輸出檔名。
3. 選擇翻譯引擎或使用非 AI 翻譯鏈。
4. 按「分析檔案」。
5. 分析完成後按「開始翻譯」。
6. 關閉遊戲與啟動器，把輸出 ZIP 整包解壓到實例根目錄並覆蓋。
7. 重啟遊戲並確認語言為繁體中文。

強制重翻可用環境變數啟動一次性流程：

```powershell
$env:MCT_FORCE_RETRANSLATE='1'
$env:MCT_AUTO_ANALYZE='1'
$env:MCT_AUTO_TRANSLATE='1'
python .\MinecraftTranslatorGUI.py
```

## CLI

CLI 入口可用於掃描、dry-run 或批次翻譯：

```powershell
python .\translate_cli.py --help
```

範例：

```powershell
python .\translate_cli.py --modpack "C:\Minecraft\Instances\ExamplePack" --output-dir "C:\Temp" --dry-run
```

## 開發環境

建議 Python 3.8+。

安裝依賴：

```powershell
pip install -r requirements.txt
```

執行測試：

```powershell
python -m pytest tests/ -q
```

語法檢查：

```powershell
python -m py_compile .\MinecraftTranslatorGUI.py .\translation_packager.py .\translation_cache.py .\translator_providers.py .\translate_cli.py .\gui\main_window.py .\core\translation_flow.py .\core\jar_patcher.py .\core\batch_translation.py .\core\class_patcher.py .\core\config_store.py .\core\format_mask.py .\core\json_utils.py
```

建置 Windows EXE：

```powershell
python -m PyInstaller --clean --noconfirm .\MinecraftTranslatorGUI.spec
```

建置後的正式執行檔會在：

```text
dist/MinecraftTranslatorGUI.exe
```

## 專案結構

```text
MinecraftTranslatorGUI.py      GUI 啟動入口
gui/main_window.py             主要桌面介面、設定、分析與流程協調
core/analysis_scan.py          整合包掃描與來源收集
core/string_extraction.py      可翻譯文字抽取
core/batch_translation.py      批次翻譯、引擎切換、限流處理、累計等待上限
core/translation_flow.py       翻譯流程、進度、輸出前驗證
core/jar_patcher.py            JAR/ZIP 安全注入與打包
core/file_parsers.py           JSON/SNBT/任務檔解析與修復
core/verification.py           輸出與翻譯覆蓋驗證
core/class_patcher.py          Class 檔案 UTF-8 字串擷取、MUTF8 編解碼、硬編碼 tooltip 修補
core/config_store.py           API Key 加密儲存（Windows DPAPI）、設定檔讀寫
core/format_mask.py            格式碼遮罩/還原系統（佔位符防碰撞）
core/json_utils.py             JSON 文字清理（BOM、註解、尾逗號、控制字元修復）
translator_providers.py        Bing、Azure、GTX、DeepL、AI provider 實作
translation_packager.py        資源包、JAR 補丁與資料檔打包工具
translation_cache.py           翻譯快取與記憶池（O(1) len 優化）
translate_cli.py               CLI 入口
tests/test_translation_core.py 核心行為測試
tests/test_should_translate.py should_translate 邊界條件測試
tests/test_format_mask.py      格式碼遮罩/還原往返一致性測試
tests/test_cli.py              CLI 參數解析與過濾邏輯測試
tests/test_zh_cn_locale_fixes.py 僅 zh_cn 覆蓋、Azure/MyMemory 語系、簡繁後備測試
tests/test_cache_polish.py       快取載入時格式碼／佔位符自動修復測試
```

## 安全性

- API Key 使用 Windows DPAPI 加密儲存，加密後的金鑰只能在同一台機器解密。
- 非 Windows 平台 fallback 到 Base64 編碼（跨平台相容）。
- 向後相容：讀取時自動偵測 `DPAPI:` 前綴使用 DPAPI 解密，否則用舊 Base64。
- 格式碼遮罩使用防碰撞佔位符，避免翻譯引擎產生的文字與佔位符衝突。
- 翻譯批次引擎有累計等待上限（5 分鐘），避免所有引擎限流時程式無限等待。

## 不應上傳的資料

本 repository 只應保存源碼與測試，不應保存個人或大型執行資料。

不要提交：

- `app_config.json` 或任何含 API Key 的設定檔
- `translation_cache*.json*`
- `translation_memory_pool.json*`
- `translation_update_index.json`
- `MinecraftTranslatorGUI.exe`、EXE 備份、`build/`、`dist/`
- 輸出的翻譯 ZIP、模組 JAR、整合包資料夾、崩潰報告
- `Failed Items/`、runtime backup、暫存檔

## 專案狀態

目前工具已能處理大型整合包的主要翻譯來源，並已針對常見崩潰原因加上防護：空 `zh_tw` 輸出、FTB Quests type 被翻譯、Patchouli 巨集破壞、Unicode surrogate、已簽名 JAR 與高風險啟動 JAR。

### 2026-09-10 v1.2.13 GTX adaptive rate + MyMemory failover

- GTX starts moderate and auto-slows gate on 429, recovers after success
- GTX cooldown 90s→~20s; MyMemory restored as free failover
- GTX worker cap 16→6 to reduce burst 429s

### 2026-09-10 v1.2.13 DeepSeek truncation auto-split

- DeepSeek/Kimi/Qwen batch 40→16; DeepSeek max_tokens 8192→16384
- On output truncation / incomplete JSON, auto-split batch in half and retry

### 2026-09-10 v1.2.13 Restore GTX high throughput

- Restore GTX fast profile (gate 0.05 / batch 128x9000 / singleton workers 12)
- Worker cap back to 16 (no longer locked to 1)
- Keep HTTP 429 cooldown/backoff

### 2026-09-10 v1.2.13 付費市面 AI 路由對齊
- Base URL 與供應商/api_type 自動對齊，避免 Anthropic 協議打到 DeepSeek 端點後 DISABLED 再落到 GTX。
- 切換供應商時不再無條件覆寫已自訂的 Base URL。
- DeepSeek / OpenAI-compatible 支援 `reasoning_content` 回填；UI 版號改為 v1.2.13。

### 2026-09-10 v1.2.5 GTX 安全速率 + 免費備援

- GTX 改為單線程、約 1 秒間隔、小批次（16／1200 字），降低 429。
- 非 AI 鏈備援：gtx → mymemory → libretranslate；限流等待延長至 180 秒。

### 2026-09-09 v1.2.3 GTX-only 高速路徑

- 非 AI 鏈停用 Bing／Azure，只走 GTX。
- 提高 GTX 批次（128／9000 字）、略降閘門間隔、提高並發；以「更大批次」衝詞/秒，比狂發請求更不易觸發限流。
- 遇 429 仍會自動退避後重試。

### 2026-09-09 v1.2.2 快取格式自動修復

- 載入翻譯快取時自動 polish：修 `% s`、`§ a`、`] (`、括號內被插空白的 `%s` 等機器／AI 常見損壞，並寫回快取。
- 補強 `fix_placeholders`（含 `&` 色碼、`%.2f` 類格式）。

### 2026-09-09 v1.2.1 語系覆蓋與打包修復

已發佈 [v1.2.1 Release](https://github.com/power79461382-hash/Minecraft-Modpack-Translator/releases/tag/v1.2.1)，Windows EXE 可直接下載。主要修復：

- **僅有 `zh_cn` 的模組**：已是中文的字串改走簡→繁；`drop_untranslated` 不再因簡繁同形字把 key 整包丟掉，避免 `zh_tw` 幾乎空白。
- **Azure / MyMemory**：Azure 拿掉硬編碼 `from=en`，改自動偵測來源；MyMemory 對 CJK 來源改用 `zh-CN|zh-TW`。
- **OpenCC 打包**：PyInstaller `.spec` 以 `collect_all('opencc')` 帶上詞典；無 OpenCC 時的簡繁後備表／詞組也已補強。
- **測試**：新增 `tests/test_zh_cn_locale_fixes.py`。

### 2026-07-08 程式碼優化與重構

已完成 10 項優化，全部完成 ✅，測試從 58 個增加到 104 個，全部通過。主要改進：

- **效能**：`TranslationCacheStore.__len__` 從 O(n) 逐筆查詢優化為 O(1) 計數器，大快取分析階段不再卡頓。
- **架構**：從 `main_window.py`（315KB/6247 行）提取 4 個獨立核心模組（`class_patcher.py`、`config_store.py`、`format_mask.py`、`json_utils.py`），15 個方法全部替換為薄包裝，檔案縮減至 294KB/5900 行。
- **安全**：API Key 改用 Windows DPAPI 加密，取代 Base64 編碼；向後相容舊格式。格式碼佔位符加入隨機 token 防碰撞。
- **穩定性**：`process_chunk_smart` 新增 5 分鐘累計等待上限，避免所有引擎限流時單一批次卡死數十分鐘。`save_cache` 不再每次重複載入記憶池。
- **測試**：新增 `test_should_translate.py`（20+ 邊界情況）、`test_format_mask.py`（遮罩往返一致性）、`test_cli.py`（CLI 參數解析）。
- **清理**：刪除 28 個舊 EXE 備份，釋放約 418MB 磁碟空間。

後續可持續改善的方向是提高動態 tooltip 覆蓋率、改善不同書本格式的版面推斷，以及針對各翻譯引擎做更細的自適應併發。

### 2026-07-08 Force 模式翻譯丟失修復

修復 force 模式下三個獨立 bug 導致大量內容未翻譯的問題：

- **Bug 1**：掃描階段 force 模式跳過 `zh_base_local` 設定，`zh_cn` fallback 被計算但從未保存
- **Bug 2**：輸出階段 force 模式直接清空 `zh_base = {}`，`zh_cn` 的 104 條翻譯完全被浪費
- **Bug 3**：force 模式忽略快取和記憶池 fallback，翻譯引擎未翻到的字串直接輸出原文

修復後 force 模式仍會重新翻譯所有字串，但：
- `zh_cn` fallback 在掃描和輸出階段都正確保留和合併
- 翻譯引擎未翻到的字串會從快取/記憶池取回，避免未翻譯輸出
- Origins datapack 中的 `name`/`description` 正確翻譯

### 2026-07-10 可靠性與封裝安全修復

- 批次排程會在部分請求失敗後繼續補充工作，並拒絕長度錯誤、空值或非字串的翻譯結果。
- AI JSON 回應改為嚴格檢查完整鍵值；付費引擎回應異常時會正確切換到備援引擎。
- 修復快取刪除後舊資料復活、force 模式誤把舊快取視為新翻譯，以及空白 `zh_tw` 未重新翻譯。
- ZIP/JAR 掃描拒絕路徑穿越項目，保留重複 ZIP 成員的正確內容，並移除修改後失效的簽章摘要。
- 修復 Patchouli 格式標記、結構化清單尾端遺失與 CLI 輸出名稱越界。
- 測試套件擴充至 170 項，涵蓋快取、供應商、批次翻譯、封裝安全、CLI 與 force 流程。

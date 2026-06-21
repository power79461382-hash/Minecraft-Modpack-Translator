# Minecraft Modpack Translator

Minecraft Modpack Translator 是一個面向 Minecraft 大型整合包的 Windows 翻譯工具。它會掃描 Forge、NeoForge、Fabric 整合包中的模組 JAR、語言檔、任務檔、手冊、書本與部分資料檔，將可安全覆蓋的玩家可見文字轉成繁體中文 `zh_tw`。

本專案的目標不是只翻譯單一 `en_us.json`，而是盡可能處理整合包常見的多來源文字：模組語言檔、Patchouli 手冊、FTB Quests / KubeJS 任務、advancement、OpenLoader / Paxi 覆蓋資料、Origins JSON、Alex's Mobs 類型書本、Markdown / SNBT / 部分設定文字，以及低風險硬編碼 tooltip。

## 目前能做到什麼

- 自動分析整合包根目錄、`mods/`、`config/`、`defaultconfigs/`、resource pack、OpenLoader 與 Paxi 覆蓋目錄。
- 從 JAR 內擷取 `assets/<modid>/lang/en_us.json`、`zh_cn.json`、legacy `.lang`，產生或補齊 `zh_tw`。
- 翻譯 Patchouli 與相似書本格式，並避免把頁面巨集、連結、圖片、recipe 標記翻壞。
- 翻譯 FTB Quests / KubeJS 任務文字，同時保護 task type、item id、NBT、image macro、pagebreak 與條件語法。
- 翻譯部分 advancement、Origins/OpenLoader JSON、Apotheosis 命名表、Markdown 與 SNBT 字串。
- 支援「補缺」、「跳過高命中項」、「強制重翻」與「只補缺漏 + 模組更新偵測」。
- 使用全域翻譯記憶池與 SQLite / shelve 快取，降低重複翻譯與重複掃描成本。
- 非 AI 翻譯鏈目前以 `Bing -> Azure -> GTX` 為主，遇到限流或錯誤會自動切換下一個可用引擎。
- 支援多種 API / OpenAI-compatible 模型，用於需要更好語意品質的付費或免費額度翻譯。
- 內建格式保護：Minecraft 顏色碼、placeholder、指令、URL、registry id、FTB/Patchouli token、數值單位與 Unicode surrogate 會被過濾或遮罩。
- 輸出前會驗證翻譯量，避免產生空 `zh_tw.json` 或看似完成但內容沒有翻譯的語言包。

## 輸出策略

工具提供 GUI 輸出模式，核心原則是「能用資源覆蓋就不破壞原始邏輯，必須寫入 JAR 時只寫入安全資源」。

- JAR 直接套用：產生可覆蓋的模組語言包 ZIP，用於把 `zh_tw` 語言/手冊/安全資源注入 JAR。
- 混合安全模式：資源包與必要補丁並用，盡量降低直接改 JAR 的風險。
- 安全資源包：輸出可放入 `resourcepacks/` 的資源包。
- OpenLoader / Paxi：若整合包支援，會優先把可覆蓋的 `assets/` 與 data 資源放到對應覆蓋目錄。

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
- 高風險 JAR 若無法用資源覆蓋，工具會保留原文以避免遊戲啟動失敗。
- class 文字修補只做低風險範圍，完整硬改 class 可能導致崩潰，因此不作為預設策略。
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
6. 依輸出模式把 ZIP 放到對應位置，或用輸出包中的安裝說明套用。
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
python -m unittest tests.test_translation_core
```

語法檢查：

```powershell
python -m py_compile .\MinecraftTranslatorGUI.py .\translation_packager.py .\translation_cache.py .\translator_providers.py .\translate_cli.py .\gui\main_window.py .\core\translation_flow.py .\core\jar_patcher.py
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
core/batch_translation.py      批次翻譯、引擎切換、限流處理
core/translation_flow.py       翻譯流程、進度、輸出前驗證
core/jar_patcher.py            JAR/ZIP 安全注入與打包
core/file_parsers.py           JSON/SNBT/任務檔解析與修復
core/verification.py           輸出與翻譯覆蓋驗證
translator_providers.py        Bing、Azure、GTX、DeepL、AI provider 實作
translation_packager.py        資源包、JAR 補丁與資料檔打包工具
translation_cache.py           翻譯快取與記憶池
translate_cli.py               CLI 入口
tests/test_translation_core.py 核心行為測試
```

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

目前工具已能處理大型整合包的主要翻譯來源，並已針對常見崩潰原因加上防護：空 `zh_tw` 輸出、FTB Quests type 被翻譯、Patchouli 巨集破壞、Unicode surrogate、已簽名 JAR 與高風險啟動 JAR。後續可持續改善的方向是提高動態 tooltip 覆蓋率、改善不同書本格式的版面推斷，以及針對各翻譯引擎做更細的自適應併發。

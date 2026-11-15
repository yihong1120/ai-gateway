# 評測工具

此目錄只供管理員執行合成任務驗證，不是正式聊天入口。
需要 PostgreSQL 的工具必須使用隔離測試 schema；不要以正式資料做範例。

| 模組 | 用途與成本 |
| --- | --- |
| comparison | 24題、4組的公司助理／單一Argon比較；預設只匯出離線題庫，需 --live、明確dev帳號與整批預算才付費實測 |
| data_replacement | 固定來源、工具與其他角色，只替換 data 模型的配對評測；預設離線匯出 |
| scenario_suite | 本人計費的多情境評測；沿用正式角色／輔助模型設定，需明確 --live、--owner 及預算 |
| hard_cases | 困難問題、合成來源及獨立驗證；由 scenario_suite 呼叫 |
| benchmark | 模型比較、成本計量與驗證共用函式；仍被情境測試引用 |
| capacity | 隨機 PostgreSQL schema 的多人排程與待處理容量測試；真實 PDF／Word 產製，模型與 OCR 延遲使用替身，不連 NAS／EIP 或付費模型，不調整正式上限 |
| smoke | 合成 Word／PPTX 示範；預設替身模型，--live 才使用模型 |
| smoke_vision | 明確指定使用者的一次付費圖片語意測試 |
| smoke_research | 使用已授權 Tavily 連線查詢公開資料；會消耗 Tavily 額度 |

以上均透過 python -m company_agent.evals.模組名稱 執行。
不要用「執行所有模組」作為一般測試命令；smoke_research 直接執行即會連外。
一般自動化回歸請使用公司助理 README 中的 unittest 命令。

`benchmark`、`comparison`、`data_replacement`、`scenario_suite`、`smoke`、`smoke_vision` 與 `capacity` 共用 `runner`：
每次執行持有一個 PostgreSQL pool scope，集中關閉模型及清理隔離 schema；設定、報告
寫入或取消失敗時也會執行清理。各工具仍保留既有案例、評分與 live 授權條件。
預算在發送前預留，無法確認成本的失敗請求保留預留額；已知實際成本再結算。
JSON 報告以同目錄暫存檔原子替換，追蹤中的主報告包含 `execution` 完成／錯誤／取消狀態。
清理失敗不會遮蔽原始執行錯誤。

`sandbox.py` 提供隔離 schema、狀態金鑰與 checkpoint；`fixtures.py` 提供共用合成工作簿。
評測不再匯入 `company_agent.tests`，正式 wheel 不包含測試程式；測試留在 repo 執行。

這些評測仍需工作區中的 examples/litellm/config.yaml 及正確服務環境；套件安裝不會自動配置金鑰或模型。
歷史測試結果不代表目前所有模型均已重新驗收。

## 模型比較

先匯出離線案例與 manifest，核對案例、來源與模型設定：

```bash
python -m company_agent.evals.comparison --output artifacts/evals/comparison-manifest.json
python -m company_agent.evals.data_replacement --output artifacts/evals/data-replacement-pack
```

`comparison` 支援公司助理與單一模型配對；檔案題不混入無工具聊天組的分母。
優化路由可選 `company_hybrid_lite`、`company_mixed`、`company_hybrid_data_argon`、
`company_chat_lite` 與 `company_chat_argon`；專責 Office 分工可選
`company_specialist_lite` 與 `company_specialist_data_argon`。
角色政策與交付契約見[工作流程設計](../../docs/company-agent-optimization-design-2026-10-17.md)。

真正模型評測需要 `--live --owner 使用者 --billing-key-file /private/dev-key.json --budget-usd 額度`。
金鑰須為正常計費、具模型白名單的一般 dev 金鑰；`--budget-usd` 不會提高 gateway 額度，
測試結束需核對帳務並撤銷臨時金鑰。不同版本、來源與批次分開記錄。

## data 角色的模型替換評測

`data_replacement` 包含 22 題直接 data 工作與 4 題完整工作流程。
缺資料與工具限制對照和正向工作分開計算，預設每題兩輪，共 104 件配對工作。
兩組只替換 data 模型，其餘七角色、來源位元組、工具、推理等級、輸出額度與時限固定。

可使用 `--cases D02 D14 D17 W01 --repeats 1` 先試跑。
role 配對最多兩組並行，workflow 序列執行，費用分工作追蹤。
`--source-report` 可明確指定私人目錄中的前批報告，沿用原來源並驗證 SHA；
不會從 repo 自動尋找舊實驗資料。`--model-lock-dir` 提供跨程序的同模型執行限制，
各程序仍須使用不同的 PostgreSQL schema。

數值、工具成功憑據、來源 SHA、原簿封裝及公式重算由模型看不到的獨立 oracle 驗收。
不同資料型別、格式或標籤不符仍算失敗；失敗後的補測另存，不能覆蓋原始結果。
模型替換須先通過品質與操作邊界驗收，再比較每成功件的費用與交付時間。
候選結果不會自動更換正式設定。

## 多情境與容量驗證

`scenario_suite` 涵蓋文件產製與局部修訂、Excel 統計、來源衝突、跨角色交付、
權限拒絕及文件偽造指令。另有合成 CAD 結構讀取；
`public_research` 會使用本人 Tavily 權限及額度，其餘使用合成檔案，不連 NAS／EIP。

每題呼叫額度、時限與輔助模型費用共同受整批預算限制。費率優先使用
`examples/litellm/config.yaml`，其次為已安裝 LiteLLM 費率表；缺費率不發送請求。
報告保留問題、獨立檢查、角色、耗時、呼叫、來源及成果，未知成本保留預留額。

容量驗證需設定 `COMPANY_AGENT_TEST_DATABASE_URL` 與 `EIP_TEST_DATABASE_URL`：

```bash
python -m company_agent.evals.capacity --users 50 --output artifacts/evals/capacity.json
```

此工具量測本機排程、等待、取消釋放與待處理容量；模型及 OCR 延遲使用替身。
本機吞吐量不代表雲端供應商限流，也不會修改正式並行設定。

## 輸出保存

逐輪 JSON／CSV、盲評包、來源包與預覽放在已忽略的 `artifacts/` 或 repo 外的私人目錄，
不放入 `docs/`。舊模型比較及容量實驗已移出 repo；重跑時以目前程式、來源及設定重新驗收。
`docs/` 保留現行設計、操作與 API 契約。

# Company agent graph31：單一文字run修訂的輸入驗證

沿用[graph30的來源、型別與版本鏈設計](company-agent-reliability-v30-design-2026-10-19.md)。原簿修改、角色分工、模型與計算工具不變；本版處理實測中一個可修正參數錯誤造成流程中止的缺口。

## 實測原因

W04／Argon／第一輪的文件已通過獨立內容檢查，但模型為滿足內部固定文字規格，嘗試用edit_artifact_text_run把一個文字run換成兩行文字。原工具只接受單一run，解析子程序正確拒絕換行，CalledProcessError卻使整件工作中止。離線重現已確認原因，不是來源損壞；原批失敗不補分。

## 行為

來源Word／PPT的Edit與私人草稿的EditArtifact共用SingleRunReplacement。replacement含真正CR、LF或tab時，在工具參數驗證階段就退回，不建立工具回條、不修改檔案。agent可以依回覆改成單段文字；需要多段內容則另製新版。明確要求的字面反斜線n仍為普通文字，不自動解碼或改寫。

擁有權、SHA、實讀run憑據與原有解析器限制均保留。欄位說明也同步修正為「沿用原交付檔名可建立新ID並取代舊稿」，避免舊說明誤導模型一定要換檔名。不同核准檔名仍代表保留兩份交付。

## 驗證

完整回歸642項：628通過、14略過；兩項新增測試涵蓋來源及草稿兩種schema的CR／LF／tab，以及真正LangGraph內「輸入錯誤 → 修正 → 原子新版 → 獨立審核 → 完成」。錯誤修訂不留下未完成回條。相關23項回歸通過；套回工作區後23項亦通過。

付費補測僅W04版本報告，兩輪、兩data模型共4件，使用相同真實合成來源與獨立判分器，不混算為最新版104件或16件驗收。兩組只有data模型不同：gemini-3.8-flash／gemini-4-argon，其餘七角色固定gemini-3.5-flash-lite。條件仍為300秒請求、600秒工作、40次模型呼叫、medium與4096 tokens。

新舊失敗與費用分批保留。角色模型在工作建立時固定，沒有自動升級、fallback或重送失敗工作；正式服務未部署、重啟或切換模型。graph31不直接恢復舊checkpoint，既有成果仍依原擁有權匯出。

候選設定另存為[Flash版](../company_agent/configs/config.office-v31-flash.example.json)與[Argon版](../company_agent/configs/config.office-v31-argon.example.json)，只有data模型不同。請依真實來源配置roots及私人state_dir；加入範例不會自動套用正式服務。

## 本輪候選架構與模型

使用LangChain工具式agent迴圈、LangGraph持久化及主協調委派。一般對答直接回答；Office工作採hybrid＋specialist，依真正需要啟動角色。有依賴的文件等待統計憑據，獨立探索可並行，每份成果只有一個產製負責子任務。

| 角色 | Office／工作責任 | 本輪模型 |
| --- | --- | --- |
| coordinator | 唯讀了解來源、完整計畫、委派與最終驗收 | gemini-3.5-flash-lite |
| data | Excel讀取、統計、公式圖表與原簿修改 | gemini-3.8-flash候選；gemini-4-argon對照 |
| document | 使用真實憑據製作／修改Word、PPT及文字 | gemini-3.5-flash-lite |
| reviewer | 唯讀來源與有效最新版，獨立校核 | gemini-3.5-flash-lite |
| knowledge／research／engineering／eip | 依需求使用各自授權來源與領域工具 | gemini-3.5-flash-lite |

coordinator在此Office模式不產製文件，reviewer也不重製成果。work_key、outputs、版本關係與程式驗收限制重複派工及漏交；每件工作固定模型與政策，沒有中途切換模型補成功。

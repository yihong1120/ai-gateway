# 重構後 company_agent 情境功能回歸測試

日期：2026-09-10。模式：不付費的情境功能回歸，不是真實 LLM 的自然語言理解評測。

本輪重新執行 20 類情境、51 項檢查，全部通過，沒有跳過案例。正式程式碼與部署設定未修改，服務未重啟。

## 證據範圍

- 下表問法是對應的使用者驗收情境，不是本輪已送入真實 LLM 的提示詞。
- 涉及模型的圖流程使用可重現的替身回應；實際執行 LangGraph、隔離 PostgreSQL、檔案解析、產製、修改與檢查。
- CAD／Excel 計算／頁面轉換使用實際 Docker 容器；OCR 使用本地實際服务。
- NAS／EIP／Tavily 使用 API 替身；未存取正式 NAS，未新增正式行事曆或簽核公文。
- 圖片語意測試只驗證介接、空白回應處理、權限與計費契約，不證明真實視覺辨識準確率。
- 真人模型測試待指定 Open WebUI 扣款帳號及總預算；本輪付費模型呼叫為 0。

## 問題與結果

| 編號 | 對應問法 | 實際驗證範圍 | 檢查數 | 結果 |
| --- | --- | --- | ---: | --- |
| 01 | 請製作工程週報，缺少資料先問我。 | 缺資料回傳 needs_input；不能空白或假裝完成 | 2 | 通過 |
| 02 | 產生 Word 草稿，再由另一位 agent 檢查內容。 | 實際建立、開啟、獨立審核產出；不能口頭宣稱已審核 | 2 | 通過 |
| 03 | 先算 Excel 數值合計，再把結果做成 Word。 | 統計實際資料，再跨角色交接產製及審核 | 2 | 通過 |
| 04 | 只改 Word／PPTX 指定文字，其他頁面和格式都保留。 | 局部修改並比對封裝內容，不重建或覆寫原檔 | 2 | 通過 |
| 05 | 修改 Excel 兩個工作表，保留公式、圖表、合併儲存格。 | 真實 XLSX 修改與版面結構核對 | 2 | 通過 |
| 06 | 做含公式和圖表的報表；遇到除以零或循環引用不能交付錯誤結果。 | 實際 LibreOffice 容器計算與錯誤拒絕 | 2 | 通過 |
| 07 | 讀掃描 PDF，告訴我文字；不要把 OCR 當作圖面語意判讀。 | 實際頁面轉換、本地 OCR；錯誤不重複執行 | 3 | 通過 |
| 08 | 這張圖片有哪些可見資訊？不要擅自推算施工尺寸。 | 語意分析介接使用本人計費；空白結果拒絕，不重複請求；非真實視覺模型準確率測試 | 2 | 通過 |
| 09 | 建立 DXF、分頁讀圖，再修改指定文字；保留原始檔。 | 實際 CAD 容器建立、讀取、修改及檔案雜湊驗證 | 3 | 通過 |
| 10 | 直接修改這份 DWG。 | 目前 DWG 僅讀取；修改應明確拒絕而不是假成功 | 1 | 通過 |
| 11 | 延續剛才的週報，這次附上新資料；接著查 EIP 最近任務。 | 聊天接續仍保有原來源；新任務切換 EIP，不沿用上一個角色 | 3 | 通過 |
| 12 | 對話已經很長，請整理前文後繼續。 | 真實 LangGraph 摘要流程及 checkpoint；模型回應替身；保留原文和當前任務 | 3 | 通過 |
| 13 | 幫我讀 NAS 本人目錄與有權限的共用檔。 | NAS API 替身；實際圖流程、權限隔離及帳密不進模型 | 3 | 通過 |
| 14 | NAS 帳號剛換過，請接續讀取舊工作的檔案。 | 綁定版本已換應拒絕；不能把新帳號套到舊來源 | 3 | 通過 |
| 15 | 查 EIP 行事曆，然後準備私人行程草稿。 | EIP API 替身；查詢使用 Calendar，不誤查公文；產生真實 CSV 但不提交 | 3 | 通過 |
| 16 | 將這份 CSV 匯入行事曆；已匯入的不要重複新增。 | EIP 模擬匯入確認、去重、結果不明停止重送；不操作正式行事曆 | 2 | 通過 |
| 17 | 查網路資料並附來源；搜尋不到也要說明。 | Tavily HTTP 替身；實際 research graph、引用、空結果、不重送及權限 | 3 | 通過 |
| 18 | 讀更多檔案內容；模型給了超過上限的 limit。 | 有限度修正分頁參數，不變更來源或擴大權限 | 3 | 通過 |
| 19 | 請繼續中斷的工作，已做好的檔案不要重做。 | checkpoint 恢復、已完成工具去重、步數不歸零 | 3 | 通過 |
| 20 | 請看看別人的結果／另一個聊天分支，或由文件 agent 直接執行其他角色操作。 | 拒絕越權；使用者、分支及角色隔離 | 4 | 通過 |

## 已確認的能力限制與尚未驗收項目

- DWG 目前僅提供讀取，不能視為已具備 DWG 修改能力；可修改的是支援範圍內的 DXF。
- company_agent 產生行事曆草稿後仍需工作台預覽與本人確認；這不同於 EIP 每小時自動處理 pipeline。本輪未驗收正式 EIP 寫入／附件上傳／專案歸類。
- 未驗收真實模型對不同措辭、錯字、含糊指令、跨三份文件日期與事件配對的判斷準確率。
- 未測試真實 Tavily 搜尋品質、正式 NAS／EIP 當前連線狀態或壓力測試。
- 本輪未發現上述功能回歸測試中的錯誤；不能據此保證所有真人對話均正確。

## 重現與紀錄

[測試執行器](/tmp/company-agent-task-eval-vDF0BH/run_cases.py)、[JSON 完整結果](/tmp/company-agent-task-eval-vDF0BH/report.json)、[執行紀錄](/tmp/company-agent-task-eval-vDF0BH/run.log)。每個情境的逐項紀錄在同目錄的 01.log 至 20.log。

以下為各情境實際執行的測試方法：

### 01

- `company_agent.tests.test_enterprise_completion.EnterpriseTests.test_explicit_missing_input`
- `company_agent.tests.test_enterprise_completion.EnterpriseTests.test_no_free_text_or_empty_completion_no_retry`

### 02

- `company_agent.tests.test_enterprise_completion.EnterpriseTests.test_valid_create_review_and_typed_completion`
- `company_agent.tests.test_enterprise_completion.EnterpriseTests.test_false_review_claim_cannot_pass`

### 03

- `company_agent.tests.test_artifact_revision.RevisionGraphTests.test_statistics_handoff_to_word_uses_real_result_and_review`
- `company_agent.tests.test_context_isolation.IsolationTests.test_real_xlsx_analysis_creation_and_independent_review`

### 04

- `company_agent.tests.test_artifact_revision.PrivateRevisionTests.test_word_and_slides_local_revision_preserves_other_parts_and_replay`
- `company_agent.tests.test_artifact_revision.RevisionGraphTests.test_create_inspect_revise_review_both_and_complete`

### 05

- `company_agent.tests.test_bridges.SpreadsheetEdits.test_multi_sheet_styles_formula_and_preservation`
- `company_agent.tests.test_bridges.SpreadsheetTool.test_actual_worker_sha_acl_and_private_output`

### 06

- `company_agent.tests.test_feature_expansion.OfficeContainerTests.test_real_calc_formulas_charts_and_literal_formula_text`
- `company_agent.tests.test_feature_expansion.OfficeContainerTests.test_divide_zero_and_circular_reference_rejected`

### 07

- `company_agent.tests.test_feature_expansion.OfficeContainerTests.test_pdf_page_bounds_and_image_preparation`
- `company_agent.tests.test_feature_expansion.OfficeContainerTests.test_local_ocr_real_scanned_page`
- `company_agent.tests.test_feature_expansion.OCRToolTests.test_failed_ocr_is_not_automatically_replayed`

### 08

- `company_agent.tests.test_bridges.SemanticTests.test_vision_cache_one_billed_request_and_no_url_fetch`
- `company_agent.tests.test_bridges.SemanticTests.test_empty_vision_fails_once_and_binding_acl_rechecked`

### 09

- `company_agent.tests.test_cad.CadContainerTests.test_real_create_paginate_read_edit_and_source_unchanged`
- `company_agent.tests.test_cad.CadContainerTests.test_malformed_and_mismatched_text_do_not_publish`
- `company_agent.tests.test_cad.CadContainerTests.test_complex_entity_rejected`

### 10

- `company_agent.tests.test_cad.CadContracts.test_dwg_write_and_bad_signature_never_launch_parser`

### 11

- `company_agent.tests.test_chat_context.ConversationTests.test_pipe_uses_owned_branch_even_with_new_attachments`
- `company_agent.tests.test_chat_context.ConversationTests.test_actual_read_of_original_and_output_no_followup_copies`
- `company_agent.tests.test_web_service.QueueTests.test_weekly_word_then_eip_same_chat_uses_new_task_and_safe_batch`

### 12

- `company_agent.tests.test_compaction.CompactionTests.test_full_graph_continues_and_checkpoint_keeps_raw_history`
- `company_agent.tests.test_compaction.CompactionTests.test_checkpoint_resume_reuses_summary_without_rebilling_it`
- `company_agent.tests.test_compaction.CompactionTests.test_blank_truncated_or_nonshrinking_summary_is_rejected`

### 13

- `company_agent.tests.test_nas.NASGraphTests.test_official_graph_reads_own_nas_without_credentials_in_model`
- `company_agent.tests.test_nas.NASTests.test_separate_users_home_and_shares`
- `company_agent.tests.test_nas.NASTests.test_remote_read_uses_existing_document_parser_and_source_hash`

### 14

- `company_agent.tests.test_nas.NASTests.test_jobs_capture_nas_revision_and_reject_rebound_followup`
- `company_agent.tests.test_nas.NASTests.test_unlink_invalidates_old_revision`
- `company_agent.tests.test_chat_context.ConversationTests.test_history_roots_are_readonly_and_owner_scoped`

### 15

- `company_agent.tests.test_calendar_tools.CalendarToolTests.test_graph_query_uses_calendar_without_preparing_writes`
- `company_agent.tests.test_calendar_tools.CalendarToolTests.test_draft_is_private_owned_eligible_cached_and_not_written`
- `company_agent.tests.test_calendar_tools.CalendarToolTests.test_complete_draft_review_handoff_never_commits`

### 16

- `company_agent.tests.test_bridges.EIPProcessingTests.test_calendar_preview_confirmation_dedup_and_unknown_stop`
- `company_agent.tests.test_bridges.EIPProcessingTests.test_calendar_only_pipeline_csv_and_owner`

### 17

- `company_agent.tests.test_research.ResearchTests.test_real_graph_handoff_citations_and_typed_completion`
- `company_agent.tests.test_research.ResearchTests.test_empty_search_is_honest_and_extract_upgrade_preserves_kind`
- `company_agent.tests.test_research.ResearchTests.test_disabled_or_revoked_permission_sends_nothing`

### 18

- `company_agent.tests.test_read_repair.ReadRepairTests.test_limit_corrected_once_then_done_with_complete_tool_receipts`
- `company_agent.tests.test_read_repair.ReadRepairTests.test_correction_cannot_change_path_tool_range_or_add_operation`
- `company_agent.tests.test_eip_batches.EIPBatchTests.test_attachment_limit_corrects_once_before_read`

### 19

- `company_agent.tests.test_langgraph.NativeGraphTests.test_model_error_can_resume_without_recreating_draft`
- `company_agent.tests.test_langgraph.NativeGraphTests.test_resume_does_not_reset_model_attempt_budget`
- `company_agent.tests.test_web_service.QueueTests.test_duplicate_is_idempotent_and_changed_payload_denied`

### 20

- `company_agent.tests.test_context_isolation.IsolationTests.test_reviewer_cannot_read_other_job_artifact`
- `company_agent.tests.test_chat_context.BranchTests.test_official_branch_helper_excludes_siblings_and_tool_authority`
- `company_agent.tests.test_multi_agent.MultiAgentTests.test_document_agent_cannot_search_outside_its_scope`
- `company_agent.tests.test_multi_agent.MultiAgentTests.test_coordinator_cannot_execute_document_tools_directly`

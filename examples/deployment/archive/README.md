# 本機歷史封存

版本控制保留本說明與 `integrity.json`。`dev/`、`releases/` 存放歷史快照、原發布包、
簽章及校驗檔，已加入 `.gitignore`；忽略規則不刪除目前本機檔案。

封存內容不是目前部署或一般測試的輸入。需要還原時，從原發布附件或既有離線備份取得
對應位元組，按 `integrity.json` 路徑去除 `examples/deployment/archive/` 前綴後放置：

```text
/private/ai-gateway-archive/
  dev/<snapshot-name>/...
  releases/<original-release-file>
```

本 repo 未設定統一下載來源，也不會從歷史紀錄中的機器路徑自動下載。
原發布檔與簽章不應解壓後重新壓縮或改寫；以受版本控制的 SHA-256 清單確認取得正確內容。
封存內的舊 `.env.example` 也屬不可變證據，不轉成 TOML、不移除或當成目前範本。
現行公開範本為根目錄 `config.example.toml`，手工私人設定來源為 `config.local.toml`。

```bash
python -m examples.deployment.tools.verify_archive --archive-dir /private/ai-gateway-archive
```

這是嚴格檢查，缺少任何列出的檔案都會失敗。一般 checkout 使用
`python -m examples.deployment.tools.verify_archive --manifest-only` 即可檢查清單；
一般完整驗證也只檢查清單，報告會明確標記封存內容檢查為 skipped。

# A / B 內網 Nginx 傳輸

2026-10-26 已完成正式切換。OCR 與 Ollama 的 B→A 連線改走 LAN HTTPS，Tailscale 保留作遠端維護及其他既有服務連線。

## 路由器 DHCP 保留

Zyxel USG FLEX 50H，LAN ge3，192.168.1.0/24。

| 主機 | 保留 IP | MAC |
| --- | --- | --- |
| A，br0 | 192.168.1.93 | 36:6f:1f:15:33:27 |
| B，wlp0s20f3 | 192.168.1.88 | c8:5e:a9:d2:33:1d |
| B 的 Windows VM，橋接網卡 | 192.168.1.98 | 52:54:00:6d:c4:ee |

B 原有保留未改動；新增 A、Windows VM 的兩筆保留。路由器設定與儲存操作均收到成功回覆，讀回三筆保留正確。地址池 .20–.199、DNS、閘道、租期未變更。用戶端繼續使用 DHCP。

VM 另有 NAT 位址 192.168.122.50，與上述橋接位址不同。A 上另一台 VM 的 .97 不在此次修改範圍。

A 已新增 `/etc/netplan/99-ai-gateway-br0-mac.yaml`，將橋接 MAC 固定為當前值；已執行 `netplan generate`，未中斷現有網路。B 的 MAC 與硬體永久位址一致；Windows VM 的橋接 MAC 已存在 libvirt 持久設定中。更換網卡或重建 VM 時需更新相應 DHCP 保留。

## 正式路徑與驗證

```text
B 127.0.0.1:8113 → HTTPS 192.168.1.93:14888/ocr/ → A 127.0.0.1:8113
B 127.0.0.1:11434 → HTTPS 192.168.1.93:14888/ollama/ → A 127.0.0.1:11434
```

A 僅監聽 LAN 的 14888，原 Tailscale 100.96.17.117:14888 已移除。Nginx ACL 及 UFW 僅允許 B 的 192.168.1.88 連入，UFW 規則限制 br0、TCP、目的位址及連接埠。沒有新增 WAN NAT。

使用獨立的內網傳輸 CA，與安裝程式程式碼簽署憑證分開。A 要求 B 的用戶端憑證；B 開啟 SNI，驗證 `ai-backend-a.internal` 的名稱及 CA 信任鏈。未停用 TLS 驗證。

正式 TLS 檔案位於兩台主機的 `/etc/nginx/ai-gateway-lan/`，私鑰權限 0600、目錄 0700。CA 私鑰只留在 B 的 root 私有目錄 `/home/changdar02/.local/state/ai-gateway/lan-transport-20261026/`，不提供網頁下載。

伺服器與用戶端憑證於 **2029-01-28 09:28:41 UTC** 到期，需在到期前換發；CA 於 2036-10-23 到期。換發後先執行兩端 `nginx -t`，依序重載並測試雙向驗證，保留舊版本以便回復。

## 開機與故障恢復

A、B Nginx 均為 enabled、active。A 新增 `/etc/systemd/system/nginx.service.d/30-ai-gateway-network.conf`，等待 network-online.target，失敗每 10 秒重試，不限制重試次數。原 Nginx 對 tailscaled.service 的啟動依賴已移除。

Docker 與 A 的 OCR API 使用者服務均已啟用；B Open WebUI 也已啟用。此次以設定與執行狀態驗證，未實際重開機。

## 部署與回復

當時使用 A／B 專用的一次性 LAN 遷移腳本與含雜湊的 staging 資料。遷移已完成，這兩個固定日期的舊腳本已從 repo 移除；此文件保留變更、驗證與備份位置，不再提供重跑入口。

部署先保留舊入口、建立 LAN 入口；B 驗證 LAN 雙向 TLS 後切換兩個 proxy，檢查 OCR 健康與模型清單，成功後才移除舊入口。Nginx 驗證或健康檢查失敗會回復 B 與 A。另測試了 B 套用／回復、Nginx 拒絕新設定、切換後健康檢查失敗，以及偵測他人修改而拒絕覆寫，四項均通過。

A 原設定備份：`/var/backups/ai-gateway/nginx-lan-20261026T174739-1021034`。

B 原設定備份：`/home/changdar02/.local/state/ai-gateway/lan-transport-20261026/b-before.conf`。

完整回復需先在 A 恢復舊入口並重載，再回復 B，驗證舊路徑正常後才移除 LAN 入口及本次專用規則；不要只回復 B，因舊入口已於成功切換後移除。備份不含後續管理員修改，回復前必須重新核對現況。

## 正式驗收

- 直連 HTTPS：HTTP 200，TCP 來源 192.168.1.88、目的 192.168.1.93，TLS verify result 0。
- 缺少用戶端憑證：HTTP 400，拒絕服務；錯誤伺服器名稱的憑證驗證測試亦被拒絕。
- B 的正式 loopback OCR 健康端點與 Ollama 模型清單均通過。
- 原問題檔 `20261022141617379.pdf`，SHA256 `132dcede4d8b9250d84bc499d38430ba7545ab189426b491e0604879623b55cf`，透過正式 B Pipe 設定重新辨識，未使用 OCR 快取。
- 兩頁版面辨識成功，10.01 秒；兩頁文字座標辨識成功，12.60 秒。來源檔未改動，EIP 請求及行事曆寫入皆為 0。

更換路由器時須移轉 DHCP 保留；更換主機 IP 時須同步調整 Nginx、UFW 與憑證 SAN。其他原有 Tailscale 連線仍依其既有設定運作。

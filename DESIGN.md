# iPhone_Linux_talker

iPhone ⇄ Ubuntu (GNOME Wayland) 文字剪貼簿中繼站。目標：接近 macOS Universal Clipboard 的體驗。

## 需求（2026-10-23 討論結論）
- 內容：文字（檔案暫不做）
- 方向：雙向（iPhone→Ubuntu、Ubuntu→iPhone）
- 網路：Tailscale（Ubuntu tailscale IP: <TAILSCALE_IP>），外出也可用
- 桌面：GNOME Wayland → 用 `wl-copy` / `wl-paste`

## 限制
iOS 不允許 App 背景讀取剪貼簿，iPhone 端必須有一個觸發動作
（Back Tap 點背面 / Action Button / 分享選單），無法完全無感。

## 架構
```
iPhone 捷徑 (Back Tap / Action Button / 分享選單)
   → 取得剪貼簿文字
   → POST http://<tailscale-ip>:PORT/push   (Bearer token)
        → Ubuntu 小服務：wl-copy 寫入剪貼簿，Ctrl+V 即可貼上
        → 桌面通知

Ubuntu → iPhone：
   Ubuntu 端快捷鍵/指令 → 服務存最新一筆到記憶體
   iPhone 捷徑 GET /pull → 「拷貝到剪貼簿」
   （或選配：ntfy 推播通知，點開即複製）
```

## 元件
1. `server/` — Python 標準庫 HTTP 服務
   - `POST /push`：body 為文字 → `wl-copy`
   - `GET /pull`：回傳最新要送給 iPhone 的文字
   - Ubuntu→iPhone：`talker send` 寫入 outbox 檔，`/pull` 讀取（無額外本機端點）
   - Bearer token 驗證；只綁定 Tailscale 介面 IP
2. `systemd/iphone-linux-talker.service` — user service 常駐
3. `cli/talker` — Ubuntu 端指令：`talker send`（送剪貼簿到 iPhone）
4. `shortcuts/README.md` — iPhone 捷徑建立步驟（Push / Pull 各一支）+ Back Tap 綁定

## 待辦 / 前置
- [x] 安裝 `wl-clipboard`
- [x] port 8787；token 在 ~/.config/iphone_linux_talker/token (600)
- [x] 實作 server + systemd unit + `talker` CLI + install.sh（2026-10-24，已用 curl 經 Tailscale IP 測通）
- [x] 建立 iPhone 捷徑（雙向測通 2026-10-24）；Back Tap 綁定由使用者自行設定
- [ ] 測試：Tailscale 外網、螢幕鎖定時 GNOME 剪貼簿行為

## 安全
- 只監聽 Tailscale IP，不對區網/公網開放
- 固定 token，存放於 600 權限檔案
- 不記錄剪貼簿內容到 log

## Roadmap
- [x] 網頁聊天介面（訊息紀錄 50 筆、SSE 即時更新、設定、可選提示方式）— 2026-10-24
- [ ] GNOME Shell 擴充套件：頂端列圖示 + 滑出提示（真正的 always-on-top）+ 聊天面板，沿用同一組 API/SSE 與設定
- [ ] 選配：ntfy 推播，讓 Ubuntu→iPhone 有手機通知
- [ ] 螢幕鎖定時的剪貼簿行為測試

# codex-whipper

[English](README.en.md) · 繁體中文

監控本機 Codex session 的模型容量錯誤，自動送出 `keep doing`，讓中斷的工作繼續。

只處理實際的 `Selected model is at capacity. Please try a different model` turn failure。它會再次確認 session 狀態、避免重送，並保留最多 1 MB 的應用 log。沒有自動換模型或替你回答 approval 的功能。

## 系統需求

- Linux、Python 3.10 以上。
- 已登入並正常工作的 Codex CLI/shared app-server。
- app-server 必須支援 Unix WebSocket 與 `thread/read`、`thread/turns/list`、`thread/queue/list`、`turn/start`。其中 turn pagination 為實驗性介面，相容性可能隨 Codex 版本變動。
- 選用：Herdr，用於明確指定的本機 `--no-daemon` CLI session。必須支援此專案使用的 `agent get/read/prompt` 與 `pane process-info` 指令。

Python 依賴只有 `websockets`。不需要另外提供 API key；script 使用已存在且已登入的 Codex app-server。

## 快速開始

```bash
git clone https://github.com/littlehsun/codex-whipper.git
cd codex-whipper
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

編輯 `.env`。範例預設 `WHIPPER_DRY_RUN=true`，因此先確認設定時不會送出訊息。

```bash
.venv/bin/python whipper.py --doctor
.venv/bin/python whipper.py --once
```

`--doctor` 檢查資料庫、socket 與載入中的 threads；不會另啟 Codex daemon，也不會 resume 工作。第二個指令會依 `.env` 掃描一次，到期的候選事件記為 `dry_run would_send` 或 `skip`。

確認 `.env` 的路徑與監控範圍後，將 `WHIPPER_DRY_RUN=false`，再執行：

```bash
.venv/bin/python whipper.py
```

`Ctrl+C` 可停止。自動重試會繼續原本 session 的工作；建議先用 `WHIPPER_THREADS` 限制到準備交給自動監控的 sessions。

## `.env` 設定

個人路徑、session UUID、監控訊息與參數都放在 `.env`。此檔已被 `.gitignore` 排除；公開倉庫只提供不含個人資料的 `.env.example`。

優先順序：**命令列參數 > process environment > `.env` > 程式預設值**。

- 預設讀取 script 所在資料夾的 `.env`，不受目前 shell 資料夾影響。
- 可用 `--env-file /path/to/private.env` 指定其他檔案。
- 相對路徑以專案資料夾為基準，支援 `~`。
- 含空白的值需加引號。只接受範例列出的 keys；不執行 shell、不展開 `$VARIABLE`、不支援多行值。
- threads 清單以逗號分隔，請填 session UUID，不是標題或 pane ID。

| 變數 | 用途／程式預設值 |
| --- | --- |
| `WHIPPER_CODEX_HOME` | Codex home；未設定則用 `CODEX_HOME` 或 `~/.codex`。 |
| `WHIPPER_DATABASE` | 留白使用 Codex home 下的 `logs_2.sqlite`。 |
| `WHIPPER_SOCKET` | 留白使用 Codex home 下的 `app-server-control/app-server-control.sock`。 |
| `WHIPPER_STATE` | 留白使用 `~/.local/state/codex-whipper/state.json`；dry-run 使用獨立狀態檔。 |
| `WHIPPER_THREADS` | 留白監控 shared server 的所有合格 sessions（子 session 需另啟用）；填入 UUID 可縮小範圍，此限制也套用到 Herdr targets。 |
| `WHIPPER_SUBAGENTS` | 預設 `false`；啟用 shared-server 子 session 續跑及 review 重送。限制 UUID 時，review 的子／主 UUID 都必須列入。 |
| `WHIPPER_HERDR_THREADS` | 明確允許透過 Herdr 操作的 standalone CLI UUID；留白關閉此功能。 |
| `WHIPPER_HERDR_BIN` | Herdr 執行檔；預設 `~/.local/bin/herdr`，也可填 PATH 中的 `herdr`。 |
| `WHIPPER_MESSAGE` | 重試訊息；預設 `keep doing`。 |
| `WHIPPER_DELAY` | 第一次重試等待秒數；預設 `60`。 |
| `WHIPPER_MAX_DELAY` | 最大等待秒數；預設 `300`，不得小於初始 delay。 |
| `WHIPPER_MAX_RETRIES` | 同一條連續 capacity failure chain 的最大重試數；預設 `5`。 |
| `WHIPPER_MAX_AGE` | 錯誤保留秒數；預設 `1800`。 |
| `WHIPPER_POLL` | 掃描間隔秒數；預設 `5`。 |
| `WHIPPER_LOOKBACK` | 新 state 要補掃的歷史秒數；程式預設 `0`，範例為 `1800`。 |
| `WHIPPER_RECONCILE_ON_START` | 每次啟動也補掃 lookback 範圍；程式預設 false，範例 true。需正的 lookback。 |
| `WHIPPER_DRY_RUN` | 只檢查不送訊息；程式預設 false，範例 true。 |
| `WHIPPER_ONCE` | 掃描一次，到期事件處理後退出；預設 false。 |
| `WHIPPER_LOG_FILE` | 留白輸出到 stderr；範例為 `logs/whipper.log`。 |
| `WHIPPER_INSTALL_DIR` | Service 安裝工具使用的專案位置；留白使用目前 checkout。 |
| `WHIPPER_PYTHON` | Service 的 Python；留白使用專案 `.venv/bin/python`。 |

Boolean 接受 `true/false`，亦接受 `1/0`、`yes/no`、`on/off`。CLI 可用 `--no-dry-run`、`--no-once`、`--no-reconcile-on-start` 覆蓋檔案設定。完整選項：

```bash
.venv/bin/python whipper.py --help
```

## Session 範圍與 Herdr

Shared app-server 預設處理目前載入、接受直接輸入的主 session。設定 `WHIPPER_SUBAGENTS=true` 可納入同一 server 的一般子 session，仍要求已載入、接受直接輸入、最新 turn 為 capacity failure 且沒有排隊輸入。內部／ephemeral session、不同 app-server process、已有新 turn 或其他錯誤會跳過；active session 不會故意被 steer。

Review 子 session 可在卸載後恢復：從本機 rollout 核對子／主 session 與原始主 turn，讀取失敗 reviewer 的原始文字輸入，以 `review/start` 的 `custom` target 在主 session 重新審查。這會重跑審查，不會從中斷點接續；未保留原始 target 類型，因此只在能取回完整文字要求時重送。主 session 必須閒置、原 review 仍為最新失敗／中斷 turn，子／主 session 都不得有排隊輸入；已重新 review、已有新 turn 或找不到完整來源時會跳過。Review 重試計數依主 session 保存，沿用相同退避、次數上限與不確定送出防護。Herdr 仍只處理明確允許的主 session。

本機 standalone CLI 沒有 shared socket 時，可以在 `.env` 的 `WHIPPER_HERDR_THREADS` 加入要授權的 UUID。這表示允許 script 在該 Herdr terminal 送出重試訊息，包含從背景服務操作。

Herdr 可透過明確的 pane ID 查看 session（在你自己的 Herdr 環境使用）：

```bash
herdr agent get <pane-id>
```

使用回傳的 `agent.agent_session.value` 作為 UUID。Script 從原 process 讀取 pane/socket，核對 foreground PID、session UUID、idle/done、主 session 範圍、最新 capacity failure、沒有 queued input 及空白輸入框。blocked、unknown、有未送出文字或 session 已替換時不送出；不切換焦點、不重啟 CLI、不回答 approval。

Herdr 的 `agent prompt` 回報送出後，還必須讀到新 turn ID 才算 accepted。其他獨立 app-server 及遠端主機不會自動套用本機 fallback。

## 重試與狀態

1. 唯讀掃描 Codex SQLite log 的新增紀錄。只接受 `codex_core::session::turn` 的實際 `Turn error`，不把聊天中的錯誤字串當成 failure。
2. 擷取 process/thread/turn ID，確認控制介面及最新 turn 仍相符。
3. 確認最新 turn 是 capacity failure、session 為 idle 或該 failure 留下的 systemError、沒有 queued user input。
4. 保存該事件已消耗，再送出訊息，讀回新 turn ID。

預設間隔為 **60、120、240、300、300 秒**，最多 5 次。只有自己啟動的下一輪又出現 capacity failure 才延續重試計數；新的使用者 turn 另算。成功送出不代表模型一定可用，容量仍滿時會再進入此流程。

State 用 atomic rename、fsync 和檔案鎖保存 cursor、待處理事件與重試鏈。請勿用不同 state 同時啟動多個正式 watcher 監控同一 session。

重啟後既有待處理事件也會套用目前的 thread 範圍與重試上限；縮小範圍或降低上限，不會沿用舊授權送出。若自行指定 state 路徑，請放在倉庫外或被 Git 忽略的資料夾。

Dry-run 不使用 live state：預設是 `dry-run.json`；如果在 `.env` 或 CLI 指定 `live.json`，dry-run 自動使用旁邊的 `live.dry-run.json`。`--once` 不等待尚未到期的事件；事件保留在其 state，後續執行會接續。

如果 dispatch timeout、斷線或 RPC 拒絕，thread 會被標為 `uncertain_or_rejected`，停止自動送訊息。`dispatching` 則代表可能在送出期間中止。**不要直接重送**：先確認 session 狀態，停止 watcher，備份 state，確認送出結果後才移除該 thread 的 `retries` 項目，讓之後的新事件可重新處理。

## Log

```bash
tail -F logs/whipper.log
```

`capacity` 是偵測到錯誤、`sent` 是已送出且有新 turn、`skip` 帶跳過原因。以下為格式示例：

```text
INFO capacity thread=<session-uuid> cwd="/work/project" turn=<turn-uuid> retry_in=60s
INFO sent thread=<session-uuid> cwd="/work/project" turn=<new-turn-uuid> attempt=1
```

`cwd` 來自 session metadata，不是 shared daemon 的 process folder；無法取得時顯示 `<unknown>`，不阻止重試。

主 log 與一份 `.1` 備份各最多 **500,000 bytes**，合計最多 **1,000,000 bytes（1 MB）**。按 UTF-8 bytes 輪替；單筆過長訊息會截斷。啟動時若既有 log 超過限制，會先移除最舊紀錄。Logs/state 可能含資料夾、訊息或 session IDs，都不應上傳或 commit。

## Systemd 背景服務

先建立 `.venv`、設定 `.env`（服務必須指定 `WHIPPER_LOG_FILE`），再產生個人 service：

```bash
.venv/bin/python install_service.py
systemctl --user daemon-reload
systemctl --user start codex-whipper.service
systemctl --user status codex-whipper.service
```

安裝工具只產生設定，不會啟動或 enable 服務。倉庫的 `codex-whipper.service` 是 template，**不要直接複製使用**；實際安裝路徑從 `.env` 取得，不會寫回倉庫。

啟用登入後自動啟動：

```bash
systemctl --user enable codex-whipper.service
```

停止並關閉自動啟動：

```bash
systemctl --user disable --now codex-whipper.service
```

如果已有 `.service.d/*.conf` drop-in，安裝工具會要求先檢查並移除／移到其他位置，避免舊參數覆蓋 `.env`。修改 `.env` 後 restart 即可；改安裝位置或 Python 後需重新產生 service 並 daemon-reload。服務把應用 log 寫到 `WHIPPER_LOG_FILE`，journal 僅用於查看 systemd 生命週期與啟動失敗。

## Troubleshooting 與限制

- **沒有觸發**：看 `skip` 原因；確認 thread allowlist、最新 turn、process/socket、錯誤 age 和 retry limit。啟動前的錯誤需要 lookback/reconciliation。Standalone CLI 需要 Herdr 明確授權。
- **連線／method 錯誤**：先 `--doctor`；CLI 和 daemon 版本可能不同。Script 不會為你重啟或升級 Codex daemon。
- **State mismatch／ID 倒退**：先停止 watcher，確認 database/socket 是否變動。備份原 state 後再建立適合新目標的 state。
- **改設定但沒生效**：確認 CLI／process environment 沒有覆蓋 `.env`；service 舊 drop-in 也可能覆蓋 ExecStart。
- **容量一直滿**：達到 retry limit 後停止；此工具不能保證或增加模型容量。
- 狀態檢查與訊息送出不是同一個原子操作，同時手動操作仍有競態。只將準備允許繼續工作的 sessions 交給 watcher。
- `/proc` 和 `fcntl` 目前使此專案限定 Linux；尚未提供 macOS/Windows 或 remote host。

## 開發與驗證

```bash
.venv/bin/python -m unittest -v
```

測試使用合成 UUID 與暫存資料庫／socket，不需要真實帳號。涵蓋錯誤過濾、重啟去重、退避與上限、active/new turn/queued input、Herdr identity/approval/input guards、真正 Unix WebSocket 模擬、設定優先順序與 dry-run 隔離、service quoting，以及 UTF-8／超長 log 的大小限制。

## 參考

- [OpenAI：Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Herdr](https://herdr.dev/)

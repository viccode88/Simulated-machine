# 架構說明

## 1. 設計原則

1. **每台設備都是獨立個體**：獨立容器、獨立行程、獨立狀態機、獨立 Modbus TCP Server。
   即使沒有內建 DCS、即使 plant-bus 失聯，設備仍維持基本狀態與安全邏輯。
2. **Modbus 與物理模型隔離**：request handler 不直接改物理變數。
3. **物理公式留在設備內**：plant-bus 只負責時間同步、路由、品質與逾時判定。
4. **安全邏輯永遠優先於手動命令**：跳機、快關、min fire、允許條件都在設備內執行。
5. **可重現**：所有時間相關邏輯（watchdog、延遲、遲滯）都以模擬時間計時，
   因此改變模擬速度或還原快照後行為一致。

## 2. 設備容器內部

```
┌──────────────────────────────────────────────┐
│ Device Container                             │
│  ┌───────────────┐   ┌────────────────┐      │
│  │ Modbus Server │──►│ Command Queue  │      │
│  └───────────────┘   └───────┬────────┘      │
│                              ▼               │
│  ┌────────────────────────────────────────┐  │
│  │ Device State Machine                   │  │
│  │ OFF / STARTING / RUNNING / TRIPPED …   │  │
│  └───────────────┬────────────────────────┘  │
│                  ▼                           │
│  ┌────────────────────────────────────────┐  │
│  │ Local Physics Model（100 ms）           │  │
│  └───────────────┬────────────────────────┘  │
│                  ▼                           │
│  ┌────────────────────────────────────────┐  │
│  │ Protection / Alarm / Interlock（100 ms）│  │
│  └───────────────┬────────────────────────┘  │
│                  ▼                           │
│  ┌────────────────────────────────────────┐  │
│  │ Atomic Register Image（整份替換）        │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

寫入流程：

```
Modbus Write
  → 檢查地址、型別、範圍與控制權（否則回傳對應 Exception）
  → 進入 command queue
  → 下一個 scan cycle 套用
  → 狀態機與安全邏輯判斷
  → 更新實際輸出
  → 產生新的暫存器映像（原子替換）
```

這樣可避免 Modbus 執行緒與物理執行緒競爭、讀到半更新的資料、
PLC 繞過跳機邏輯，以及 32 位元資料高低 word 不一致。

## 3. 為什麼 server 是自己實作的

`pymodbus==3.14.0` 固定作為 **client**（DCS、測試、互通性驗證）使用。
Server 端則自行以 asyncio 實作 MBAP/PDU，原因是驗收標準要求：

* 對唯讀區寫入、超範圍設定值、設備忙碌、未啟用功能碼，
  必須分別回傳 **02 / 03 / 06 / 01**，且不可關閉連線。
* 需要注入協定層故障（延遲、丟棄 request/response、錯誤例外碼、連線重置、rate limit）。
* 需要逐筆稽核（client IP、transaction id、unit id、function code、起始地址、
  數量、寫入值、exception code、latency、接受或拒絕）。

`tests/modbus/` 以 pymodbus client 驗證互通性，確保標準工具可以正常連線。

## 4. 模擬時間與 lockstep

plant-bus 每個 tick：

```json
{"type": "TICK", "tick": 10230, "sim_time": 1023.0, "dt": 0.1,
 "inputs": {"boiler.pressure_bar_abs": {"value": 100.12, "quality": "GOOD", "tick": 10229}}}
```

設備計算完成後回覆：

```json
{"type": "TICK_DONE", "device": "boiler", "tick": 10230,
 "outputs": {"boiler.pressure_bar_abs": 100.12, "boiler.level_pct": 66.54,
             "boiler.steam_generation_kg_s": 82.41},
 "quality": "GOOD"}
```

設備使用第 n 次已確認的鄰接輸出計算第 n+1 次狀態。

**設備失聯**：某設備沒有在 `tick_timeout` 內完成 →
其他設備繼續使用上一筆程序量，品質先降為 `STALE`，超過 `bad_seconds` 後為 `BAD`，
設備依自身 `comm.failure_policy` 進入保持或安全狀態，模擬器不會整體崩潰。

## 5. 週期

| 功能 | 週期 |
| --- | ---: |
| 物理模型 / 安全邏輯 | 100 ms（每個 tick） |
| 暫存器映像更新 | 100 ms |
| DCS PID | 500 ms |
| DCS 快速 Modbus 掃描 | 250 ms |
| 歷史資料取樣 | 1 s |
| 狀態持久化 | 1 s |
| 控制器 watchdog | 1 s（逾時 3 s） |

## 6. 通訊監控與控制權

* 控制器每秒遞增 `40003 Watchdog Counter`，設備在 `30030 Watchdog Echo` 回傳。
* 超過 3 秒未更新 → `CONTROL_WATCHDOG_OK = false`，並依 `comm.failure_policy` 動作：

| 設備 | 預設策略 | 行為 |
| --- | --- | --- |
| 鍋爐 | `FAIL_LOW` | 燃燒器立即降至 0% |
| 主蒸汽閥 | `FAIL_CLOSE` | 閥門關閉 |
| 汽輪機 | `FAIL_LOW` | 要求關閥、安全減速 |
| 給水泵 | `HOLD_LAST` | 保持 2 秒後降速 |
| 凝結水泵 | `LOCAL_FALLBACK` | 切換至本地水位控制 |
| 發電機 | `HOLD_LAST` | 保持負載，嚴重逾時後打開斷路器 |
| 冷凝器 | `LOCAL_FALLBACK` | 維持本地冷卻控制 |
| 給水槽 | `HOLD_LAST` | 無主動輸出，僅標記通訊品質 |

* 多控制器衝突：`single_writer` 啟用時，只有取得 lease 的來源 IP 可寫入，
  其他 client 仍可讀取，寫入回傳 `Server Device Busy`；
  緊急停止可另外由 `safety_allowlist` 指定的 Safety PLC 來源寫入
  （僅限整批都是安全線圈的寫入，混合批次不適用此特權）。

## 7. 持久化與快照的差別

| | 持久化（SQLite volume） | 快照（plant-bus） |
| --- | --- | --- |
| 目的 | 容器重啟後不遺失鎖存與累積量 | 測試環境秒級回到指定狀態 |
| 內容 | 跳機鎖存、第一故障、累積量、計數器 | 全廠完整狀態（含物理量、暫存器、PID） |
| 觸發 | 每秒自動 | 手動／API／自動排程 |
| 還原時機 | 容器啟動 | 任何時候，不重啟容器 |

容器重新啟動後：跳機不會自動清除、設備回到安全輸出、需要操作員執行 reset 與 start。

### 7.1 快照完整性

還原前 plant-bus 一定會檢查快照的格式版本、結構與 SHA-256 checksum，任一項不符就
拒絕還原並回傳 HTTP 409，不會把損毀的內容套到機組上。

儲存當下若有設備離線，該快照會被標記為 `complete: false` 並記下 `missing` 清單：

* 不會成為 `last_snapshot`（`plantctl rollback` 不會挑到它）。
* 還原時預設拒絕，避免產生「部分設備新狀態、部分設備舊狀態」的混合機組。
* 確定要繼續時才用 `--allow-incomplete`（API 為 `allow_incomplete: true`）。

# 快速恢復狀態（Plant Snapshot）

## 1. 解決什麼問題

測試工業控制邏輯時，最花時間的往往不是測試本身，而是「把機組再開一次」：
建立真空、補水、升壓、升速、併聯、加載——在模擬器裡也要好幾分鐘。
`docker compose down && up` 更糟：容器重建、Modbus 連線中斷、累積量歸零、
外部 PLC 也得重連。

本功能讓整套環境在**毫秒級**回到指定狀態，而且**不重啟任何容器**。

## 2. 運作方式

```
plantctl / HTTP API
        │
        ▼
   plant-bus（快照協調器）
   1. 在 tick 邊界暫停模擬（等待進行中的 tick 完成）
   2. 廣播 SNAPSHOT_SAVE / SNAPSHOT_RESTORE
   3. 收集或下發每個參與者自己的狀態
   4. 等待全部 ACK
   5. 恢復執行
        │
        ├── boiler / turbine / …（8 台設備，各自序列化自己的完整狀態）
        └── dcs-plc（PID 積分項、啟動順序步驟、跳機矩陣）
```

關鍵設計：

* **在 tick 邊界操作**：暫停時所有設備都剛好完成同一個 tick，
  因此快照是一致的時間切片，不會出現「鍋爐在 t，汽輪機在 t+1」。
* **設備自己序列化**：plant-bus 不需要知道任何物理變數的意義，
  新增設備或新增狀態變數不必改動 plant-bus。
* **原子套用**：設備在暫停狀態下套用，套用後立即重建暫存器映像並持久化；
  同時清空 command queue，避免還原前殘留的命令污染新環境。
* **Modbus 不中斷**：server、TCP 連線、controller lease 都不受影響，
  外部 PLC 只會看到程序值瞬間跳回快照當時的數值。

## 3. 快照內容

| 類別 | 內容 |
| --- | --- |
| 匯流排 | tick、模擬時間、全部程序量與其來源／品質／forced 狀態 |
| 物理 | 各設備 `STATE_VARS` 宣告的所有物理量（水量、壓力、轉速、閥位、慣性、溫度…） |
| 狀態機 | 目前狀態、前一狀態、停留時間 |
| 保護 | 每個跳機的 active / latched / first_out / resettable、計數、第一故障原因與前後趨勢 |
| 警報 | active / latched / acked / 數值 / 門檻 / 次數 |
| 暫存器 | Holding 與 Coil 的完整內容（設定值、模式、限值、PID 參數） |
| 累積量 | 運轉秒數、啟動次數、跳機次數、質量與能量 totalizer、命令計數 |
| 故障注入 | 感測器／執行器／程序故障，以及協定層通訊故障設定 |
| 控制器 | 每個 PID 的積分項與 bumpless 狀態、三元素設定、啟動順序索引、跳機矩陣 |

檔案為單一 JSON，含 `meta`（名稱、建立時間、模擬時間、設備清單、描述、標籤、
SHA-256 checksum），以「暫存檔 + rename」原子寫入 `plant-bus-state` volume。

## 4. 使用方式

### CLI

```bash
plantctl snapshot save steady-60mw -d "60 MW 穩態基準" -t baseline
plantctl snapshot list
plantctl snapshot show steady-60mw
plantctl snapshot restore steady-60mw
plantctl snapshot restore steady-60mw --clean
plantctl snapshot delete old-run
plantctl rollback
```

### HTTP

```bash
curl -X POST localhost:15080/snapshot/save \
     -H 'Content-Type: application/json' \
     -d '{"name":"steady-60mw","description":"60 MW 穩態基準","tags":["baseline"]}'

curl -X POST localhost:15080/snapshot/restore \
     -H 'Content-Type: application/json' \
     -d '{"name":"steady-60mw","clear_latches":true,"resume":true}'
```

### 啟動時自動還原

```bash
RESTORE_ON_BOOT=steady-60mw docker compose --profile standalone up
```

### HMI

頁面右上角可直接輸入名稱後按「存快照 / 還原 / 還原(清鎖存)」。

## 5. 還原選項與鎖存語意

| 選項 | 預設 | 行為 |
| --- | --- | --- |
| `clear_latches` | false | true 時清除跳機鎖存、第一故障與警報，並把 TRIPPED 設備放回 OFF |
| `keep_faults` | false | true 時保留目前注入的故障，不套用快照內的故障設定 |
| `preserve_totalizers` | false | true 時保留目前累積量（適合長期壓力測試） |
| `resume` | true | false 時還原後維持暫停，方便單步除錯 |

**預設是忠實還原**：跳機鎖存與第一故障原因會一起回來，
符合「跳機不因數值恢復或容器重啟自動清除」的要求；
需要乾淨起點時再明確加上 `--clean`。

建議的工作流：

```bash
plantctl snapshot save clean-baseline -t baseline   # 存一份未跳機的乾淨狀態
# … 執行破壞性測試 …
plantctl snapshot save incident-2026-07-26          # 保留現場供事後分析
plantctl snapshot restore clean-baseline            # 回到乾淨起點繼續下一個測試
```

## 6. 驗證環境已重置

每次還原都會讓每台設備的 `30039 SNAPSHOT_GENERATION` 加一，
plant-bus 的 `/state` 也會回報 `snapshot_generation`。
測試程式可以：

```python
before = api("/state")["snapshot_generation"]
api("/snapshot/restore", "POST", {"name": "steady-60mw"})
assert api("/state")["snapshot_generation"] == before + 1
```

`POST /snapshot/restore` 的回應包含 `restored`、`failed`、`missing`、`elapsed_ms`，
其中 `missing` 是「快照裡有、但目前沒連線」的設備，
可用來判斷環境是否與快照當時一致。

## 7. 與模糊測試的搭配

`tools/fuzz/harness.py` 每一輪都：

1. `POST /snapshot/restore`（`clear_latches: true`）回到基準
2. 送出一批畸形封包
3. 檢查設備存活、回應格式與例外碼合法
4. 檢查物理安全不變量（水量守恆、轉速上限、鎖存不得自行解除）
5. 失敗時輸出 crash artifact（最後 50 筆封包、匯流排狀態、事件）

因為每輪起點都一模一樣，crash 才有機會被穩定重現；
搭配 `tools/modbus_recorder.py` 的 replay 可以把序列重播回去。

## 8. 限制

* 快照不包含 Modbus 連線狀態（TCP 連線本身不受影響，但 client 端的
  transaction 計數與 controller lease 不會回捲）。
* 快照不包含 historian 的歷史資料庫（那是刻意的：事件記錄要保留全部歷史）。
* 還原時若某設備離線，該設備會被列入 `missing`，其餘設備仍會還原；
  設備重新上線後需要再還原一次或自行由持久化狀態啟動。

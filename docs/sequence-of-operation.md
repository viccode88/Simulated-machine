# 操作順序

本頁是操作順序的摘要。逐步操作方法、錯誤判讀與排除，見
[操作手冊](operations-manual.md)。

## 1. 冷啟動順序（內建 DCS 自動執行）

`controller/startup_sequence.py` 的 `build_sequence()` 共 **16 個 Step**，
下表的編號與程式中的索引一致。

| 步驟 | Step 名稱 | 說明 | 完成條件 |
| ---: | --- | --- | --- |
| 1 | `COOLING_WATER` | 啟動冷凝器冷卻系統 | 冷卻水可用率 > 90% |
| 2 | `PULL_VACUUM` | 建立冷凝器真空 | 冷凝器壓力 ≤ 0.12 bar(a) |
| 3 | `CHECK_HOTWELL` | 確認熱井水位 | ≥ 20% |
| 4 | `START_CONDENSATE_PUMP` | 啟動凝結水泵 | RUNNING（熱井低低水位則禁止） |
| 5 | `TANK_LEVEL` | 給水槽水位控制至 60% | 誤差 < 5% |
| 6 | `START_FEEDWATER_PUMP` | 啟動給水泵 | RUNNING（給水槽低低水位則禁止） |
| 7 | `BOILER_LEVEL` | 鍋爐水位控制至 66.7% | 誤差 < 5% |
| 8 | `BOILER_PURGE` | 鍋爐吹掃 | 進入 IGNITING／PRESSURIZING／RUNNING（水位須在 30～80%） |
| 9 | `IGNITE` | 點火 | `FLAME_STATUS`(30020) ≥ 2（火焰穩定） |
| 10 | `RAISE_PRESSURE` | 緩慢升壓 | 壓力 ≥ 30 bar(a)（升壓期間燃燒器上限 20%） |
| 11 | `OPEN_MSV` | 緩慢開啟主蒸汽閥 | 轉速 > 300 RPM（真空不良則禁止） |
| 12 | `RUN_UP` | 汽輪機升速至 3000 RPM | 誤差 ≤ 30 RPM（升速期間閥門上限 15%） |
| 13 | `SYNC_CHECK` | 同步檢查 | `SYNC_PERMISSIVE`(30017) = 0x3F（六項全成立） |
| 14 | `CLOSE_BREAKER` | 閉合斷路器 | `BREAKER_STATUS`(30016) ≥ 1 |
| 15 | `RAMP_LOAD` | 逐步增加負載 | 達目標負載 95% |
| 16 | `NORMAL` | 進入正常自動控制 | — |

任一步驟的 `guard` 不成立時，順序停在該步驟並記錄 `SEQUENCE_BLOCKED`，不會硬闖；
超過該步驟的 `timeout` 則記錄 `SEQUENCE_STEP_TIMEOUT` 並停止順序。

## 2. 順序禁止（會被拒絕的操作）

分兩層，判讀事件時要分清楚是哪一層擋下的。

**DCS 順序 guard**（5 條，事件 `SEQUENCE_BLOCKED`）：

| 步驟 | 禁止條件 |
| --- | --- |
| 4 `START_CONDENSATE_PUMP` | 熱井水位 < 20% 時啟動凝結水泵 |
| 6 `START_FEEDWATER_PUMP` | 給水槽水位 < 25% 時啟動給水泵 |
| 8 `BOILER_PURGE` | 鍋爐水位不在 30～80% 時點火 |
| 11 `OPEN_MSV` | 冷凝器壓力 > 0.15 bar(a)（真空不良）時啟動汽輪機 |
| 14 `CLOSE_BREAKER` | 汽輪機轉速偏差 > 30 RPM 時閉合斷路器 |

**設備層拒絕**（事件 `COMMAND_REJECTED`，代碼 `5x90`）：

* 跳機未重置時重新啟動設備
* 緊急停止啟動中
* 維修模式（`CONTROL_MODE`=4）
* 啟動允許條件不成立（`INTERLOCKS_OK`=0，例如鍋爐壓力不足時汽輪機無法啟動）
* 發電機同步六項條件未全部成立時閉合斷路器

被拒絕的命令會：保持設備原狀、設定 `COMMAND_REJECTED` 警報、
記錄拒絕原因與被擋下的允許條件、增加 `30033 REJECTED_COMMAND_COUNT`。

（`30032` 是 `MODBUS_REQUEST_COUNT`，不要混用。）

## 3. 控制迴路

| 迴路 | PV | SP | MV | 備註 |
| --- | --- | --- | --- | --- |
| 鍋爐壓力 | 鍋爐壓力 | 100 bar(a) | 燃燒器輸出 | 5 %/s 上升、10 %/s 下降、anti-windup |
| 鍋爐水位 | 顯示水位 | 66.7% | 給水泵速度 | 三元素：水位修正 + 蒸汽流量前饋 − 給水流量回授 |
| 給水槽水位 | 槽水位 | 60% | 凝結水泵速度 | 輸出變化限制較慢 |
| 汽輪機轉速 | 轉速 | 3000 RPM | 主蒸汽閥開度 | 死區 2 RPM、負載前饋、超速無條件關閥 |
| 有功功率 | 電氣功率 | 負載設定 | 主蒸汽閥開度 | 強電網模式使用 |

自動／手動切換採 bumpless transfer（切換瞬間以目前輸出反推積分項）。

## 4. 跳機後的處理

1. 設備自身立即執行安全動作（燃燒器歸零、快關、斷路器跳脫）。
2. 相鄰設備透過 sim_net 訊號連鎖（`turbine.tripped` → 主蒸汽閥快關）。
3. DCS 跳機矩陣作為第二層防護（開斷路器、燃燒器歸零、降載）。
4. 第一故障原因寫入事件記錄與持久化，不被後續連鎖跳機覆蓋。
5. 操作員以 Reset Key `0xA55A` + 新命令序號 + Reset Coil 脈衝重置，
   且所有安全條件（含遲滯與 reset_delay）必須成立。

## 5. 測試前的環境重置

```bash
plantctl snapshot restore steady-60mw --clean   # 毫秒級，容器不重啟
```

比起 `docker compose down && up`（分鐘級、Modbus 連線中斷、累積量歸零），
快照還原能讓每個測試都從位元級相同的起點開始。

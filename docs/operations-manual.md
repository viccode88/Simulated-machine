# 火力發電廠模擬器 — 操作手冊

本手冊說明「怎麼把這座模擬電廠正確開起來並發電」、「運轉中要看什麼」、
「出錯時如何判讀與排除」。所有門檻與行為皆取自程式與 `configs/*.yaml`，
文件中每個數字都可在對應檔案中找到出處。

相關文件：[架構](architecture.md)｜[物理模型](physics-model.md)｜
[操作順序摘要](sequence-of-operation.md)｜[暫存器表](register-map.csv)｜
[警報代碼表](alarm-codes.csv)｜[外部 PLC 串接](plc-integration.md)

---

## 目錄

1. [前置準備與啟動環境](#一前置準備與啟動環境)
2. [三種操作介面的分工](#二三種操作介面的分工)
3. [正常冷啟動並發電](#三正常冷啟動並發電16-步)
4. [正常運轉：控制迴路與監看](#四正常運轉控制迴路與監看)
5. [正常停機](#五正常停機)
6. [狀態判讀：狀態字、警報字、跳機字](#六狀態判讀狀態字警報字跳機字)
7. [錯誤分類與處理](#七錯誤分類與處理)
8. [跳機重置標準程序](#八跳機重置標準程序)
9. [INTERLOCKS_OK 變成 NOT OK 的完整條件](#九interlocks_ok-變成-not-ok-的完整條件)
10. [案例：冷凝器永遠停在 ALARM ACTIVE](#十案例冷凝器永遠停在-alarm-active)
11. [附錄：代碼與位址速查](#十一附錄代碼與位址速查)

---

## 一、前置準備與啟動環境

```bash
cp .env.example .env
docker compose up --build          # 設備自持：開起來就會自己發電
```

八台設備、plant-bus、HMI 與 historian 一律啟動；profile 只決定要哪一種 PLC：

| Profile | PLC | 適用 |
| --- | --- | --- |
| `openplc-v4`（**預設**） | OpenPLC Runtime v4（官方映像） | 一般使用；Editor 由 15443 部署專案 |
| `openplc-v3` | OpenPLC Runtime v3（自原始碼建置） | 需要 v3 網頁介面或舊版相容測試 |
| `no-plc` | 無 | 驗證設備真的自持、或接自己的 PLC |
| `secure` | 額外以 stunnel 在 802 埠提供 Modbus Security | 協定安全測試 |

profile 由 `.env` 的 `COMPOSE_PROFILES` 決定。切換寫法：

```bash
COMPOSE_PROFILES=openplc-v3 docker compose up
```

> 命令列的 `--profile` 是**疊加**在 `COMPOSE_PROFILES` 之上而非取代。
> v3 與 v4 會搶同一個主機埠（`PLC_MODBUS_PORT`，預設 15020），不要同時啟動。

啟動後可用位址：

| 服務 | 位址 | 用途 |
| --- | --- | --- |
| HMI | <http://127.0.0.1:15082> | 監看 + 模擬控制 + 快照 |
| plant-bus API | <http://127.0.0.1:15080/state> | 全廠狀態 JSON |
| Historian | <http://127.0.0.1:15081/events> | 事件查詢 |
| PLC 北向 Modbus | 15020 | SCADA／HMI 連這裡 |
| OpenPLC 介面 | 15443（v4 Editor API）／15083（v3 網頁） | 部署 PLC 程式 |
| 設備 Modbus TCP | 15021～15028 | 直連單一設備（工程用） |

準備 CLI：

```bash
alias plantctl='python -m tools.plantctl'
plantctl status          # 確認 8 台設備都 online
```

**開工前檢查清單**

1. `plantctl status` 的 `offline_devices` 必須為空。
2. plant-bus 的 `paused` 為 `false`（HMI 右上角顯示 `RUN`）。
3. 每台設備 `SIM_BUS_OK`(10012) 為 1。
   （`CONTROL_WATCHDOG_OK`(10011) 只在有 PLC 時才會是 1；沒有 PLC 也能運轉。）
4. 沒有殘留的跳機鎖存：`TRIP_WORD`(30004) 全為 0。
   容器重啟後鎖存會保留（`WARM_START` 事件），必須先重置才能啟動。

> **注意**：冷啟動時冷凝器壓力是 1.0 bar(a)（大氣壓），此時 `ALARM_ACTIVE` 為 1
> 是**正常現象**，不是故障。詳見[第十章](#十案例冷凝器永遠停在-alarm-active)。

### 全廠沒有動作？先看自持狀態

設備是自持的，「全廠靜止不動」表示**允許條件沒成立**或**被操作員停機鎖住**，
不是沒有控制器：

```bash
plantctl events --event SELF_START --limit 20      # 誰自己啟動了
plantctl read --device condenser --register SELF_HOLD_STATE
plantctl read --device condenser --register PERMISSIVE_WORD
```

| `SELF_HOLD_STATE`(30027) | 意義 | 處理 |
| ---: | --- | --- |
| 0 | 自持被關閉 | `.env` 的 `SELF_HOLD=true`，重啟該容器 |
| 1 | 待機，等允許條件 | 讀 `PERMISSIVE_WORD`(30028)，哪個位元是 0 就是在等哪一項 |
| 2 | 自持運轉中 | 正常 |
| 3 | 操作員停機鎖 | 下一次 `START` 脈衝即可恢復 |
| 4 | 跳機鎖定 | 走[第八章](#八跳機重置標準程序)的重置程序 |
| 5 | 維修模式 | `CONTROL_MODE`(40001) 從 4 改回 1 |

| 其他現象 | 原因 | 處理 |
| --- | --- | --- |
| 設備都在但 `plantctl status` 顯示 offline | Modbus 埠沒開或容器沒健康 | `docker compose ps`、`docker logs <service>` |
| 有 PLC 但 `CONTROL_WATCHDOG_OK` 為 0 | PLC 程式沒部署或沒在跑 | 檢查 OpenPLC（15443／15083）是否已載入專案並 Start PLC |

---

## 二、三種操作介面的分工

這三種介面**能做的事並不一樣**，選錯介面是最常見的「按了沒反應」原因。

| 介面 | 能做 | 不能做 |
| --- | --- | --- |
| **HMI 網頁**（15082） | 看程序量與品質、看事件與第一故障、暫停／繼續／單步、存取快照 | **不能啟停設備、不能寫設定值**（是唯讀監看 + 模擬控制） |
| **plantctl CLI** | 上述全部，外加 Modbus 讀寫、故障注入、訊號強制、情境執行 | — |
| **SCADA／Modbus TCP**（經 PLC，或直連設備） | 啟動、停止、跳機重置、確認警報、觀察 | 不能改模擬時間或快照（那是 management_net 的事） |

所以：

* **想「看」** → HMI 或 ScadaBR。
* **想「臨時操作一下」** → `plantctl write` / `plantctl read`。
* **想「自動運轉」** → 什麼都不用做，設備自己會跑。想要 PLC 的資料交換與跳機矩陣，
  就用預設的 `openplc-v4` profile（或接自己的 PLC，骨架見 `examples/external_plc.py`）。

### 等價操作對照

| 動作 | 誰負責 | plantctl | Modbus（位址／PDU offset） |
| --- | --- | --- | --- |
| 啟動設備 | 設備自持；SCADA 可手動 | `plantctl write --device boiler --register START --value 1 --coil` | Coil `00001` / offset 0，脈衝 |
| 停止設備 | SCADA（會鎖住自持啟動） | `--register STOP --coil` | Coil `00002` / offset 1，脈衝 |
| 跳機重置 | SCADA | 見[第八章](#八跳機重置標準程序) | Coil `00003` / offset 2，脈衝 |
| 確認警報 | SCADA | `--register ACK_ALARM --coil` | Coil `00004` / offset 3，脈衝 |
| 緊急停止 | SCADA | `--register EMERGENCY_STOP --coil` | Coil `00005` / offset 4，**保持型** |
| 調整目標值 | 工程操作（非日常） | `--register PRIMARY_SETPOINT --value 100` | Holding `40010` / offset 9 |
| 人工接管輸出 | 工程操作 | 先 `CONTROL_MODE=0`，再 `--register MANUAL_OUTPUT --value 50` | Holding `40001` → `40012` |

`CONTROL_MODE`：`0=LOCAL_MANUAL 1=LOCAL_AUTO（預設） 2=REMOTE_MANUAL 3=REMOTE_AUTO 4=MAINTENANCE`。

* **AUTO（1／3）時 `MANUAL_OUTPUT` 會被忽略**：執行器由設備本地調節器決定。
  這是「寫了 MANUAL_OUTPUT 卻沒反應」最常見的原因——要人工接管請先寫 `CONTROL_MODE=0`。
* **`4=MAINTENANCE` 會讓所有 START 命令被拒絕**，這是另一個常見的「按了沒反應」。
* **STOP 之後設備不會自己重啟**（操作員停機鎖），要再按一次 START。

> 寫入不會立即生效：所有 Modbus 寫入先進 command queue，
> 下一個 scan cycle 才由狀態機與安全邏輯決定是否套用（`base_device.py: _apply_commands`）。

---

## 三、正常冷啟動並發電（設備自持，不需下命令）

開機之後什麼都不用做。每台設備只看自己的允許條件，順序是這些條件互相扣住後
自然浮現的（完整說明見 [sequence-of-operation.md](sequence-of-operation.md)）。

| 約略時間 | 發生的事 | 觸發它的允許條件 |
| ---: | --- | --- |
| 0 s | 冷凝器啟動、抽真空 | 熱井水位 > 5% |
| 0 s | 凝結水泵、給水泵啟動 | 熱井 ≥ 20%、給水槽 ≥ 25% |
| 0 s | 鍋爐吹掃 30 秒 | 水位 30～80%、主蒸汽閥接近關閉 |
| 35 s | 點火，升壓（燃燒器限幅 20%） | `FLAME_STATUS`(30020) ≥ 2 |
| 60 s | 冷凝器真空 ≤ 0.15 bar(a) | — |
| 145 s | 主蒸汽閥開啟（限幅 15%） | 壓力 ≥ 30 bar + 真空良好 |
| 150 s | 汽輪機升速 | 真空良好 + 主蒸汽壓力 ≥ 10 bar |
| 220 s | 發電機勵磁 | 轉速 ≥ 額定 90% |
| 235 s | **自動併聯** | `SYNC_PERMISSIVE`(30017) = 0x3F |
| 235 s～ | 負載以 0.5 MW/s 爬升，壓力不足時自動暫停 | 鍋爐壓力 ≥ 設定值 90% |
| 930 s | 60 MW 穩態（99 bar / 3002 RPM） | — |

### 監看啟動進度

```bash
plantctl watch                                  # 即時追蹤重點程序量
plantctl events --event SELF_START              # 哪些設備自己啟動了
plantctl events --event SELF_HOLD_STANDBY       # 哪些設備自己進了待機
plantctl read --device boiler --register PERMISSIVE_WORD   # 還在等哪個條件
```

卡住時的判讀：讀該設備的 `PERMISSIVE_WORD`(30028)，位元順序與
`start_permissives()` 一致（見 [自持設計](self-holding.md) 或該設備原始碼）。

### 免等冷啟動：存一份滿載基準快照

第一次跑完整啟動、機組到達目標負載後：

```bash
plantctl baseline                    # 等到 ≥57 MW 後存成 steady-60mw
```

把 `.env` 改成 `RESTORE_ON_BOOT=steady-60mw`，之後每次 `docker compose up`
plant-bus 都會在所有設備連上後直接還原（毫秒級），機組開機即在 60 MW 滿載。

`plantctl baseline` 的行為：每 2 秒輪詢一次，發現任何設備跳機就中止並回非零
（避免把故障狀態存成基準）；逾時預設 1800 秒。

覺得太慢就加速模擬：`plantctl speed 20`。本地調節器與物理在同一個 scan cycle，
控制週期永遠等於 dt，**加速不會改變控制行為**。

### 想手動走一次（把自持關掉）

```bash
SELF_HOLD=false docker compose up -d        # 設備只待機，等外部命令
plantctl write --device condenser --register START --value 1 --coil
plantctl read  --device condenser --register CONDENSER_PRESSURE
plantctl write --device condensate_pump --register START --value 1 --coil
```

每一步先讀 `READY`(10001) 與 `INTERLOCKS_OK`(10013) 確認允許條件成立再下 START。
注意此時仍不需要（也不該）寫 `MANUAL_OUTPUT`：調節器還是設備自己的，
除非再把 `CONTROL_MODE` 改成 0。

---

## 四、正常運轉：控制迴路與監看

### 控制迴路

全部的調節都在**設備內**（`configs/<device>.yaml` 的 `control:` 區塊）：

| 迴路 | 執行者 | PV | SP | MV | 特性 |
| --- | --- | --- | --- | --- | --- |
| 鍋爐壓力 | 鍋爐 | `BOILER_PRESSURE` | 40010（100 bar） | 燃燒器輸出 | kp 1.5 / ki 0.01 / kd 3.0，上升 5 %/s、下降 10 %/s、死區 0.2 |
| 鍋爐水位 | 給水泵 | `boiler.level_pct` | 40010（66.7%） | 泵浦轉速 | **三元素**：水位修正 + 蒸汽流量前饋(×1.0) − 給水流量回授 |
| 給水槽水位 | 凝結水泵 | `feedwater_tank.level_pct` | 40010（60%） | 泵浦轉速 | 給水泵流量前饋；水位過高自行待機 |
| 熱井水位 | 冷凝器 | `HOTWELL_LEVEL` | 40011（55%） | 補水閥 | — |
| 汽輪機轉速 | 主蒸汽閥 | `turbine.speed_rpm` | 40010（3000 RPM） | 閥門開度 | 死區 2 RPM、負載前饋、**超速無條件關閥** |
| 有功功率 | 主蒸汽閥 | `generator.electrical_power_mw` | 發電機負載需求 | 閥門開度 | 強電網模式（`OPERATING_MODE`=1）併聯後接手 |
| 負載爬升 | 發電機 | — | 40010（60 MW） | 負載需求 | 0.5 MW/s；鍋爐壓力不足自動暫停 |

本地調節器的輸出可直接讀 `LOCAL_OUTPUT`(30026)。自動／手動切換與轉速↔負載接手都採
**bumpless transfer**：切換瞬間以目前輸出反推積分項，所以切過去不會跳動。

啟動期間的兩個暫時限幅（`boiler.control.startup_burner_max_pct` 20%、
`steam_valve.control.startup_valve_max_pct` 15%）是直接套在調節器的 `out_max` 上，
不是把 PID 輸出砍掉。這件事很重要：若在 PID 外面 `min()`，
積分項會對著看不見的限幅一路累積到上限，限幅解除的瞬間執行器從 15% 跳到 100%，
汽輪機必定超速跳機、鍋爐必定超壓跳機。

**高壓 runback**：壓力達 108 bar（高壓警報門檻）時，鍋爐立即切燃料且不受
正常降載速率限制（10 %/s → 100 %/s），壓力回到門檻以下才恢復調節。
甩載時蒸汽需求瞬間消失，沒有這個動作壓力會直接衝過安全閥到 115 bar 超壓跳機。
事件：`PRESSURE_RUNBACK`。

| 限幅 | 值 | 解除條件 |
| --- | ---: | --- |
| 燃燒器（升壓期間） | 20% | 壓力 ≥ 95 bar(a) **或斷路器已閉合** |
| 主蒸汽閥（升速期間） | 15% | 轉速 ≥ 2910 RPM 或斷路器已閉合 |

> 燃燒器限幅的「或斷路器已閉合」不可省略：併聯後汽輪機開始抽汽，
> 壓力就不會再自己爬到 95 bar，燃燒器會被鎖在 20%（約 20 kg/s）
> 而負載需要 60 kg/s，壓力一路掉到 `LOW_PRESSURE`，轉速跟著垮掉並欠頻跳機。

### 改變負載

```bash
plantctl write --device generator --register PRIMARY_SETPOINT --value 90
```

加載速率受 `LOAD_RATE_LIMIT`(40031) 限制。加載太快的典型後果是
鍋爐壓力掉 → `LOW_PRESSURE` 警報 → 轉速掉 → `UNDERFREQUENCY`。

### 每班應該看的東西

```bash
plantctl status              # 設備狀態、跳機、離線
plantctl watch               # 重點程序量滾動顯示
plantctl events --limit 50   # 最近事件
```

必看的四個位址（每台設備都有）：

| 位址 | 名稱 | 判讀 |
| --- | --- | --- |
| 30001 | `STATUS_WORD` | 逐位元狀態，見第六章 |
| 30002 / 30003 | `ALARM_WORD_1/2` | 哪些警報 **active 或未確認的 latched** |
| 30004 | `TRIP_WORD` | 哪些跳機**鎖存中** |
| 30006 | `FIRST_OUT_CODE` | 第一故障原因代碼，**不會被後續連鎖跳機覆蓋** |

---

## 五、正常停機

倒著關，順序和啟動相反：

1. **降載到 0**：`generator` `PRIMARY_SETPOINT` = 0，等 `ELECTRICAL_POWER` ≈ 0。
2. **打開斷路器**：`generator` Coil `00011` `BREAKER_OPEN` 脈衝。
   （功率沒降就開斷路器 → 汽輪機失去電磁轉矩 → 瞬間加速 → **超速跳機**。）
3. **關主蒸汽閥**：`steam_valve` `MANUAL_OUTPUT` = 0，或 STOP。
4. **停鍋爐**：`boiler` `MANUAL_OUTPUT` = 0，再 STOP。
5. **等汽輪機惰走停止**，再 `turbine` STOP。
6. **保持給水**直到鍋爐水位穩定，再停給水泵、凝結水泵。
7. **最後停冷凝器**：STOP 後真空系統輸出降到 5% 以下才會進 OFF；
   壓力會回到大氣壓，此時 `HIGH_PRESSURE` 警報重新出現是正常的。

> 停機不需要重置：`STOP` 走的是 `STOPPING → OFF`，不會產生跳機鎖存。
> 只有跳機（TRIPPED）才需要重置。

---

## 六、狀態判讀：狀態字、警報字、跳機字

### 6.1 `STATUS_WORD` (30001) 與 Discrete Inputs (10001～10016)

| bit | Discrete | 名稱 | 意義 |
| ---: | ---: | --- | --- |
| 0 | 10001 | `READY` | 無跳機鎖存 + 無 E-Stop + **所有啟動允許條件成立** |
| 1 | 10002 | `RUNNING` | 狀態機為 RUNNING |
| 2 | 10003 | `STARTING` | — |
| 3 | 10004 | `STOPPING` | — |
| 4 | 10005 | `TRIPPED` | 狀態機跳機 **或** 有任何保護鎖存 |
| 5 | 10006 | `ALARM_ACTIVE` | **有警報條件正在成立**（不含已消失但未確認者） |
| 6 | 10007 | `REMOTE` | CONTROL_MODE 為 2 或 3 |
| 7 | 10008 | `AUTO` | CONTROL_MODE 為 1 或 3 |
| 8 | 10011 | `WATCHDOG_OK` | 控制器 watchdog 正常 |
| 9 | 10012 | `SIM_BUS_OK` | 模擬匯流排連線正常 |
| 10 | 10013 | `INTERLOCKS_OK` | **所有啟動允許條件成立**（見第九章） |
| 11 | 10014 | `SENSOR_FAULT` | 有感測器故障注入 |
| 12 | 10015 | `ACTUATOR_FAULT` | 有執行器故障注入 |
| 13 | 10016 | `MAINTENANCE` | 維修模式（START 一律被拒） |
| 14 | — | `LAB_MODE` | 實驗模式開啟（可注入故障） |
| 15 | — | `SIM_PAUSED` | 模擬暫停中 |

`READY` 與 `INTERLOCKS_OK` 的差別：`READY` = `INTERLOCKS_OK` **且**沒有跳機鎖存、
沒有 E-Stop。所以 `INTERLOCKS_OK=1` 但 `READY=0` → 一定是跳機沒重置或 E-Stop 還壓著。

### 6.2 `ALARM_WORD` 與「ALARM_ACTIVE」的關鍵差異

這是最容易誤判的地方：

* **`ALARM_ACTIVE`（狀態字 bit 5 / 10006）** 只看 `active` — 條件**此刻正在成立**。
* **`ALARM_WORD_1/2`（30002/30003）** 看 `active` **或** `latched` —
  只要曾經發生且**尚未 ACK**，位元就會一直亮。

所以：

| 現象 | 意義 | 處置 |
| --- | --- | --- |
| ALARM_WORD 有位元、ALARM_ACTIVE = 0 | 警報已解除，只是沒確認 | 送 `ACK_ALARM`(00004) 清掉 |
| ALARM_ACTIVE = 1 | **條件仍在成立** | ACK 沒用，要排除根因 |

`ACK_ALARM` 對「還在成立中」的警報只會標記為已確認，**不會讓它消失**
（`alarm.py: ack_all` 只在 `latched and not active` 時才解除 latched）。

### 6.3 `TRIP_WORD` (30004) 與第一故障

每個跳機有四個屬性：

| 屬性 | 意義 |
| --- | --- |
| `active` | 跳機條件此刻仍成立 |
| `latched` | 已鎖存，**必須用重置命令解除** |
| `first_out` | 是否為第一故障原因 |
| `resettable` | 值已回到 `reset` 門檻另一側，且維持滿 `reset_delay` |

第一故障原因（`FIRST_OUT_CODE` 30006）會記錄設備、代碼、模擬時間、真實時間、
程序值、門檻、當時控制輸出與**前後各 10 秒的主要變數趨勢**，且不會被後續連鎖跳機覆蓋。

```bash
plantctl events --event FIRST_OUT
plantctl events --event TRIP_LATCHED
```

### 6.4 `OVERALL_QUALITY` (30007)

`0=GOOD 1=UNCERTAIN 2=STALE 3=BAD_SENSOR 4=BAD_COMM 5=FORCED 6=OUT_OF_RANGE 7=SIMULATED_FAULT`

判定順序（`base_device.py: overall_quality`）：匯流排斷 → `BAD_COMM`；
有感測器故障注入 → `SIMULATED_FAULT`；訂閱訊號有 BAD → `BAD_COMM`；
有 STALE → `STALE`；否則 `GOOD`。

---

## 七、錯誤分類與處理

錯誤分成六類，**排查順序建議由上而下**（越上面越容易誤判成設備故障）。

### 7.1 命令被拒絕（`COMMAND_REJECTED`，代碼 5x90）

**症狀**：下了 START/RESET，設備完全沒反應；`REJECTED_COMMAND_COUNT`(30033) 增加；
警報 5x90 亮 2 秒。

**查法**：

```bash
plantctl events --event COMMAND_REJECTED
```

事件內含 `reason` 與 `blocked`（被擋下的允許條件清單）。

| reason | 原因 | 處理 |
| --- | --- | --- |
| `跳機未重置` | `TRIPPED` 或有保護鎖存 | 依[第八章](#八跳機重置標準程序)重置 |
| `緊急停止啟動中` | Coil 00005 為 1 | 先把 `EMERGENCY_STOP` 寫回 0 |
| `維修模式` | `CONTROL_MODE`=4 | 改回 2 或 3 |
| `啟動允許條件不成立` | `INTERLOCKS_OK`=0 | 看 `blocked` 清單，對照[第九章](#九interlocks_ok-變成-not-ok-的完整條件) |
| `Reset Key 錯誤` | `RESET_KEY`(40004) ≠ 0xA55A(42330) | 先寫 42330 |
| `命令序號未更新` | `COMMAND_SEQUENCE`(40002) 與上次相同 | 換一個新值 |
| `安全條件不成立` | 有跳機仍 active，或未滿 `reset_delay` | 等條件回復並滿延遲 |
| `同步條件不成立` | 發電機六項同步允許未全成立 | 見 7.5 |
| `TRIP_TEST 僅在 LAB_MODE 開放` | `LAB_MODE=false` | 改 `.env` 後重啟 |

**允許條件不成立**（設備層，`INTERLOCKS_OK`=0、`PERMISSIVE_WORD` 有位元為 0）
常見的七項：

* 冷凝器真空不良時啟動汽輪機／主蒸汽閥
* 鍋爐水位不在 30～80% 時點火
* 鍋爐壓力不足時開啟主蒸汽閥
* 汽輪機轉速不符時閉合發電機斷路器
* 跳機未重置時重新啟動設備
* 給水槽低低水位時啟動給水泵
* 熱井低低水位時啟動凝結水泵

這些條件同時也是自持啟動的條件——設備不會硬闖，會停在待機
（`SELF_HOLD_STATE`=1）直到條件成立。

### 7.2 Modbus 協定層錯誤（Exception Code）

| Exception | 意義 | 常見原因 | 處理 |
| ---: | --- | --- | --- |
| 01 | ILLEGAL FUNCTION | 用了未啟用的功能碼 | 只用 01/02/03/04/05/06/15/16/22/23/43-14；**連線不會被關閉** |
| 02 | ILLEGAL DATA ADDRESS | 地址不存在，或寫入唯讀區 | 對照 `register-map.csv`，注意**文件地址 40010 = PDU offset 9** |
| 03 | ILLEGAL DATA VALUE | 數值超出工程範圍 | 看 `register-map.csv` 的 min/max 欄 |
| 04 | SERVER DEVICE FAILURE | 內部錯誤 | 看 `MODBUS_REQUEST` 事件 |
| 06 | SERVER DEVICE BUSY | 設備正在切換狀態、資料鎖定，或 command queue 滿（512） | 稍後重試；持續發生代表寫入頻率過高 |

`EXCEPTION_COUNT`(30034) 會累計。另外有兩個容易踩的規則：

* **單一寫入者**（`modbus.single_writer`）：租約 `COMMAND_LEASE_TIME`(40007) 內
  只有一個 controller ID 能寫。搶寫會被拒。
* **寫入 allowlist**：`configs/*.yaml` 的 `modbus.write_allowlist` 限制哪些暫存器可寫。

### 7.3 通訊錯誤

#### (a) 控制器 watchdog 逾時（`CONTROL_WATCHDOG_LOST`，5x91）

**條件**：`WATCHDOG_COUNTER`(40003) 超過 `comm.watchdog_timeout`（預設 3 秒）沒變化，
且曾經不為 0。

**後果**：超過 `comm.hold_seconds`（預設 2 秒）後套用通訊失效策略：

| 策略 | 行為 | 使用的設備 |
| --- | --- | --- |
| `HOLD_LAST` | 保持最後輸出 | `feedwater_pump`、`feedwater_tank`、`generator` |
| `FAIL_LOW` | 輸出降到最低（燃燒器歸零、汽輪機安全減速） | `boiler`、`turbine` |
| `FAIL_CLOSE` | 閥門關閉 | `steam_valve` |
| `LOCAL_FALLBACK` | 切回本地控制 | `condenser`（冷卻能力維持 100%）、`condensate_pump` |
| `TRIP` | 直接跳機（代碼 5x97） | 預設無設備使用，需要時才設定 |

發電機另有 `breaker_open_after_s: 10.0`：通訊中斷持續 10 秒後打開斷路器。

**處理**：恢復 watchdog 遞增即可，警報自動解除。

#### (b) 模擬匯流排資料品質不良（`SIM_BUS_BAD`，5x92）

**條件**（`base_device.py: _update_common_alarms`）：

```
alarm 5x92 = (有任何訂閱訊號的品質不是 GOOD) 或 (bus_ok = false)
```

訊號品質由 plant-bus 依「資料新舊」判定（`bus.py: _quality_for`）：

| 資料年齡 | 品質 |
| --- | --- |
| ≤ 1.5 × dt | `GOOD` |
| ≤ `bad_seconds`（預設 3 s） | `STALE` |
| 更久 / 從未發佈 | `BAD` |

**開機時大量出現 5x92 是正常的**：設備陸續連上 plant-bus，先連上的設備會有幾百毫秒
讀不到還沒連上的鄰居，於是 ALARM_SET，等對方上線後 ALARM_CLEARED。
典型 log 長這樣（同一批 `PARTICIPANT_JOINED` 之後 0.1～0.2 秒內全部清除）：

```
6.6s condensate_pump ALARM_SET     code=5592 模擬匯流排資料品質不良
6.7s steam_valve     ALARM_SET     code=5892 模擬匯流排資料品質不良
...
6.9s generator       ALARM_CLEARED code=5392
7.0s condensate_pump ALARM_CLEARED code=5592
```

**只有在「持續不清除」時才是問題**，原因有三：

1. 某台設備真的離線 → `plantctl status` 的 `offline_devices` 會列出來。
2. 某台設備一直趕不上 tick（`tick_timeout` 0.35 s）→ 事件 `DEVICE_TICK_TIMEOUT`，
   `plant_device_missed_ticks` metric 上升。處理：降低 `simulation.speed`，或加大 `dt`。
3. plant-bus 本身斷線 → 設備會 free-run 並發出 `SIM_BUS_TIMEOUT`，
   `SIM_BUS_OK`(10012) 變 0。

`SIM_QUALITY_WORD`(30035) 逐位元顯示**每一個訂閱訊號**是否 GOOD，可以直接看出是哪一路壞。

#### (c) 通訊故障注入（僅 LAB_MODE）

```bash
plantctl fault set --target turbine --category comm --name modbus \
        --spec '{"response_delay_ms": 300, "drop_response_prob": 0.05}'
```

可用參數：`response_delay_ms`、`drop_request_prob`、`drop_response_prob`、
`force_busy_prob`、`wrong_exception_prob`、`connection_reset_prob`、`freeze`、`rate_limit_per_s`。

### 7.4 程序警報（逐台）

警報**不會停機**，但通常是跳機的前兆。門檻全部來自 `configs/*.yaml`。

#### 鍋爐 `boiler`（5100）

| 代碼 | 名稱 | 條件 | 意義與處理 |
| ---: | --- | --- | --- |
| 5111 | `LOW_LEVEL` | 水位 < 30% | 給水不足；檢查給水泵速度、給水槽水位 |
| 5112 | `HIGH_LEVEL` | 水位 > 85% | 給水過量；三元素迴路可能積分飽和 |
| 5113 | `HIGH_PRESSURE` | 壓力 > 108 bar(a) | 燃燒率過高或蒸汽閥開度不足 |
| 5114 | `FLAME_UNSTABLE` | 火焰不穩 | 燃燒器輸出過低或波動 |
| 5115 | `FEEDWATER_MISMATCH` | 給水與蒸汽流量偏差過大 | 三元素迴路調諧問題，升載時常見 |
| 5116 | `LOW_PRESSURE` | 壓力低 | 加載太快或燃燒率不足 |
| 5117 | `RELIEF_VALVE_OPEN` | 壓力 > **113 bar(a)**（`relief_setpoint_bar`） | 安全閥是超壓跳機（115 bar）前的最後一道防線，立刻降燃燒率 |

#### 汽輪機 `turbine`（5200）

| 代碼 | 名稱 | 條件 |
| ---: | --- | --- |
| 5211 | `HIGH_SPEED` | 轉速 > 3150 RPM |
| 5212 | `HIGH_VIBRATION` | 振動 > 7 mm/s |
| 5213 | `LOW_VACUUM` | 排汽壓力 > 0.15 bar(a) |
| 5214 | `HIGH_BEARING_TEMP` | 軸承溫度 > 95 °C |
| 5215 | `LOW_SPEED` | 轉速低 |

#### 發電機 `generator`（5300）

| 代碼 | 名稱 | 條件 |
| ---: | --- | --- |
| 5311 | `OVERCURRENT` | 電流 > 1.05 pu |
| 5312 | `OVERFREQUENCY` | 頻率 > 51.0 Hz |
| 5313 | `REVERSE_POWER` | 逆功率 |
| 5314 | `SYNC_BLOCKED` | 同步條件不成立（BREAKER_CLOSE 被拒時設定） |
| 5315 | `BREAKER_FAIL` | 斷路器拒動 |
| 5316 | `UNDERFREQUENCY` | 頻率 < 48.5 Hz |

#### 冷凝器 `condenser`（5400）

| 代碼 | 名稱 | 條件 | 備註 |
| ---: | --- | --- | --- |
| 5411 | `HIGH_PRESSURE` | 壓力 > **0.15 bar(a)** | **停機時必然成立**，見第十章 |
| 5412 | `LOW_HOTWELL_LEVEL` | 熱井 < 20% | 補水閥 `MAKEUP_VALVE_CMD`(40030) |
| 5413 | `HIGH_HOTWELL_LEVEL` | 熱井 > 90% | |
| 5414 | `COOLING_WATER_LOW` | 運轉中且冷卻能力 < 80% | `MANUAL_OUTPUT` 太低或冷卻水故障 |
| 5415 | `VACUUM_SYSTEM_FAULT` | 運轉中且真空系統輸出 < 50% | 真空建立期間會短暫成立 |

#### 泵浦 `condensate_pump`（5500）／`feedwater_pump`（5700）

| 代碼 | 名稱 | 條件（凝結水泵 / 給水泵） |
| ---: | --- | --- |
| 5x11 | `LOW_SUCTION_LEVEL` | 來源水位 < 20% / < 25% |
| 5x12 | `CAVITATION` | 汽蝕因子 < 95% 且轉速 > 1% |
| 5x13 | `MOTOR_OVERCURRENT` | 電流 > 1.1 pu |
| 5514 | `LOW_FLOW` | 流量低（凝結水泵） |
| 5714 | `NO_FLOW_HIGH_PRESSURE` | 排出壓力不足以進水（給水泵） |

#### 給水槽 `feedwater_tank`（5600）

`5611 LOW_LEVEL`(<25%)、`5612 LOW_LOW_LEVEL`、`5613 HIGH_LEVEL`(>85%)、`5614 OVERFLOW`。
給水槽是**被動容器**，沒有啟停，`INTERLOCKS_OK` 恆為 1。

#### 主蒸汽閥 `steam_valve`（5800）

`5811 POSITION_DEVIATION`（偏差 > 5%）、`5812 FAST_CLOSE_ACTIVE`、
`5813 ACTUATOR_FAULT`、`5814 FAIL_TO_CLOSE`。

### 7.5 跳機（TRIP）

跳機會鎖存並強制設備進入 TRIPPED。門檻與延遲：

| 設備 | 代碼 | 名稱 | 警報 | 跳機 | 延遲 | 重置門檻 | 重置延遲 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| boiler | 5101 | `LOW_LOW_LEVEL` | 30% | **< 20%** | 2 s | 30% | 5 s |
| boiler | 5102 | `HIGH_HIGH_LEVEL` | 85% | **> 90%** | 2 s | 80% | 5 s |
| boiler | 5103 | `HIGH_PRESSURE` | 108 | **> 115 bar(a)** | 1 s | 105 | 5 s |
| boiler | 5104 | `FLAME_FAILURE` | — | 火焰失敗 | 1 s | — | 3 s |
| turbine | 5201 | `OVERSPEED` | 3150 | **> 3300 RPM** | 0.2 s | 3050 | 5 s |
| turbine | 5202 | `HIGH_VIBRATION` | 7 | **> 11 mm/s** | 1 s | 6 | 5 s |
| turbine | 5203 | `LOW_VACUUM` | 0.15 | **> 0.25 bar(a)** | 2 s | 0.12 | 10 s |
| turbine | 5204 | `HIGH_BEARING_TEMP` | 95 | **> 110 °C** | 5 s | 90 | 10 s |
| generator | 5301 | `OVERCURRENT` | 1.05 | **> 1.2 pu** | 2 s | 1.0 | 5 s |
| generator | 5302 | `OVERFREQUENCY` | 51.0 | **> 52.0 Hz** | 1 s | 50.5 | 5 s |（斷路器打開時抑制：超速由汽輪機 3300 RPM 保護負責）
| generator | 5303 | `UNDERFREQUENCY` | 48.5 | **< 47.5 Hz** | 1 s | 49.5 | 5 s |
| generator | 5304 | `REVERSE_POWER` | — | 逆功率 | 3 s | — | 5 s |
| condensate_pump | 5501 | `LOW_LOW_SUCTION` | 20% | **< 10%** | 2 s | 25% | 5 s |
| condensate_pump | 5502 | `CAVITATION_TRIP` | 10 s | **> 20 s** | 0.5 s | 1 | 5 s |
| condensate_pump | 5503 | `MOTOR_OVERCURRENT` | 1.1 | **> 1.3 pu** | 2 s | 1.0 | 5 s |
| feedwater_pump | 5701 | `LOW_LOW_SUCTION` | 25% | **< 15%** | 3 s | 25% | 5 s |
| feedwater_pump | 5702 | `CAVITATION_TRIP` | 10 s | **> 20 s** | 0.5 s | 1 | 5 s |
| feedwater_pump | 5703 | `MOTOR_OVERCURRENT` | 1.1 | **> 1.3 pu** | 2 s | 1.0 | 5 s |
| steam_valve | 5802 | `FAIL_TO_CLOSE` | — | 無法關閉 | 0.5 s | — | 5 s |
| 全設備 | 5x97 | `COMM_TIMEOUT_TRIP` | — | 策略為 TRIP 時 | — | — | — |
| 全設備 | 5x98 | `TRIP_TEST` | — | LAB_MODE 測試 | — | — | — |
| 全設備 | 5x99 | `EMERGENCY_STOP` | — | Coil 00005 | — | — | — |

**冷凝器與給水槽的保護設定為「只警報、不跳機」**（`trip: null`），
冷凝器真空喪失是由汽輪機的 `LOW_VACUUM`(5203) 執行跳機。

### 跳機後的四層動作

1. **設備自身**立即安全動作：燃燒器歸零、閥門快關、斷路器跳脫。
2. **鄰接設備**透過 sim_net 訊號連鎖，例如 `turbine.tripped` → 主蒸汽閥快關、
   `boiler.feedwater_permitted`=0 → 給水泵實際停轉。
3. **PLC 跳機矩陣**作為第二層防護（OpenPLC 專案的 `main.st`）。PLC 只下命令，
   不寫控制值：

   | 來源跳機 | PLC 下達的命令 |
   | --- | --- |
   | `turbine` | 發電機 `BREAKER_OPEN` + 主蒸汽閥 `STOP` |
   | `boiler` | 主蒸汽閥 `STOP` |
   | `condenser` | 發電機 `BREAKER_OPEN` |
   | `feedwater_pump` | 鍋爐 `STOP`（避免低水位） |
   | `condensate_pump` | 無（給水泵自己保護給水槽） |
   | `steam_valve` | 鍋爐 `STOP` |

   另有獨立的超速保護：轉速 > 3150 RPM 時對主蒸汽閥下 `STOP`。
   被 `STOP` 停下的設備會維持停機，直到操作員按 `START`。

4. **第一故障原因**寫入事件記錄與持久化，容器重啟也保留。

### 7.6 感測器／執行器／程序故障注入（僅 `LAB_MODE=true`）

```bash
# 程序：冷卻水能力降到 30%
plantctl fault set --target condenser --category process \
        --name cooling_water_availability --value 0.3

# 執行器：主蒸汽閥卡開
plantctl fault set --target steam_valve --category actuator \
        --name valve_mode --spec STUCK_OPEN

# 感測器：鍋爐水位 +10% 偏差
plantctl fault set --target boiler --category sensor --name level \
        --mode bias --bias 10

plantctl fault clear --target '*'
```

判別方式：`SENSOR_FAULT`(10014)、`ACTUATOR_FAULT`(10015)、
`FAULT_INJECT_WORD`(30038) 以及警報 `5x93/5x94/5x95`。

> **注意**：故障注入目標打錯字**不會**退化成全廠廣播，會回報
> `FAULT_TARGET_UNKNOWN` 並列出合法目標。
> 快照還原時，若本機 `LAB_MODE=false`，快照內的故障設定會被丟棄
> （事件 `SNAPSHOT_FAULTS_DISCARDED`）。

---

## 八、跳機重置標準程序

重置需要**同時**滿足五個條件（`base_device.py: _handle_reset`）：

1. `RESET_KEY`(40004) = `0xA55A` = 十進位 **42330**
2. 緊急停止已解除（Coil 00005 = 0）
3. `COMMAND_SEQUENCE`(40002) 為**新值**（與上次重置不同）
4. 所有鎖存的跳機都 **不再 active**
5. 所有鎖存的跳機都 **resettable**：程序值回到 `reset` 門檻另一側，並維持滿 `reset_delay`

標準步驟：

```bash
# 0) 先確認為什麼跳
plantctl read --device boiler --register FIRST_OUT_CODE
plantctl events --event FIRST_OUT

# 1) 排除根因（例如把水位補回 30% 以上）

# 2) 等 reset_delay（多數為 5 秒，LOW_VACUUM / HIGH_BEARING_TEMP 為 10 秒）

# 3) 送重置
plantctl write --device boiler --register RESET_KEY --value 42330
plantctl write --device boiler --register COMMAND_SEQUENCE --value 12
plantctl write --device boiler --register RESET_TRIP --value 1 --coil

# 4) 確認
plantctl read --device boiler --register TRIP_WORD      # 應為 0
plantctl read --device boiler --register STATUS_WORD    # READY 應為 1
```

成功會產生 `TRIP_RESET` 事件，設備從 TRIPPED 回到 OFF，
`FIRST_OUT` 清除（事件 `FIRST_OUT_RESET`），警報一併 ACK。

**失敗時看 `COMMAND_REJECTED` 事件的 reason**：

| reason | 意義 |
| --- | --- |
| `Reset Key 錯誤` | 沒先寫 42330，或寫入順序顛倒 |
| `緊急停止尚未解除` | Coil 00005 還是 1 |
| `命令序號未更新` | `COMMAND_SEQUENCE` 和上次一樣 |
| `XXX 仍在動作中` | 該跳機的 `active` 還是 true，根因沒排除 |
| `XXX 尚未滿足重置條件` | 還沒滿 `reset_delay`，再等一下 |

> **緊急停止的重置順序**：先把 `EMERGENCY_STOP` 寫回 0 → 等 `reset_delay` →
> 才送 RESET_TRIP。順序顛倒一定被拒。

### 測試用捷徑

不想手動重置時（僅測試環境）：

```bash
plantctl snapshot restore steady-60mw --clean   # 還原並清除所有鎖存與警報
```

---

## 九、`INTERLOCKS_OK` 變成 NOT OK 的完整條件

`INTERLOCKS_OK`（Discrete `10013`，STATUS_WORD bit 10）的定義只有一行
（`base_device.py: _interlocks_ok`）：

```python
def _interlocks_ok(self) -> bool:
    return all(ok for _, ok in self.start_permissives())
```

也就是**該設備的啟動允許條件只要有一項不成立，就是 NOT OK**。
每台設備的允許條件不同，完整清單如下。

### 9.1 冷凝器 `condenser`（3 項）

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 熱井水位可用 | `hotwell_level` > 5% | 熱井幾乎抽乾（洩漏故障注入、凝結水泵抽太快而無補水） |
| 無跳機鎖存 | 無任何保護鎖存 | E-Stop / TRIP_TEST / COMM_TIMEOUT_TRIP 觸發過且未重置 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop 壓著 |

> 冷凝器三個程序保護都設為 `trip: null`，所以**壓力高、熱井水位異常不會**讓
> `INTERLOCKS_OK` 變 NOT OK，只會發警報。

### 9.2 凝結水泵 `condensate_pump` / 給水泵 `feedwater_pump`（各 6 項）

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 來源水位高於最低值 | 凝結水泵：熱井 ≥ **20%**<br>給水泵：給水槽 ≥ **25%** | 上游水位不足；**冷啟動最初一個 scan 也會短暫 NOT OK**（訊號尚未收到，讀到 0） |
| 吸入口壓力有效 | 凝結水泵：冷凝器壓力 > 0<br>給水泵：給水槽壓力 > 0 | 上游設備離線或訊號 BAD |
| 馬達可用 | 無 `motor_unavailable` 執行器故障 | 故障注入 |
| 排出路徑可用 | 出口閥 > 1%（給水泵有出口閥）<br>凝結水泵無此閥，恆成立 | 給水泵 `OUTLET_VALVE_CMD`(40030) 忘了開 |
| 無跳機鎖存 | 無保護鎖存 | 低低吸入水位 / 汽蝕 / 過流跳機未重置 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop |

> **給水泵最常見的 NOT OK 是「排出路徑可用」**：只下了 START 卻沒先寫
> `OUTLET_VALVE_CMD=100`（預設值就是 100，通常不需要動）。

### 9.3 鍋爐 `boiler`（6 項）— 條件最多，最常 NOT OK

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 水位在 30%～80% | `30 ≤ level_indicated ≤ 80` | 冷啟動預設水位 **66.7%**（`initial_mass_kg: 40010`）本來就成立；水位被抽乾或灌太滿才會 NOT OK |
| 給水泵可用 | 給水流量訊號 ≥ 0 **且 `bus_ok`** | 匯流排斷線 |
| 主蒸汽閥接近關閉 | `steam_valve.steam_flow_kg_s` ≤ **5 kg/s** | **運轉中必然 NOT OK**（正常帶載時蒸汽流量遠大於 5） |
| 無鍋爐跳機鎖存 | 無保護鎖存 | 低低水位 / 高高水位 / 超壓 / 熄火跳機 |
| 模擬匯流排正常 | `bus_ok` = true | plant-bus 斷線 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop |

> **重要**：鍋爐在正常帶載運轉時 `INTERLOCKS_OK` 本來就是 0，
> 因為「主蒸汽閥接近關閉」不成立。這是**啟動允許條件**，不是運轉中的健康指標。
> 判斷鍋爐是否正常，請看 `RUNNING`(10002) 與 `TRIPPED`(10005)，不要看 `INTERLOCKS_OK`。

### 9.4 主蒸汽閥 `steam_valve`（3 項）

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 無閥門跳機鎖存 | 無保護鎖存 | `FAIL_TO_CLOSE`(5802) 跳機 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop |
| 執行器電源正常 | 無 `ACTUATOR_POWER_LOSS` 故障 | 執行器故障注入 |

### 9.5 汽輪機 `turbine`（4 項）

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 冷凝器真空良好 | 冷凝器壓力 ≤ **0.15 bar(a)**（`turbine.start_max_exhaust_bar`） | **冷啟動時必然不成立**（1.0 bar(a)），要等真空建立約 57 秒 |
| 主蒸汽壓力足夠 | 鍋爐壓力 ≥ **10 bar(a)**（`turbine.min_admission_pressure_bar`） | **冷啟動時必然不成立**（1.0 bar(a)），要等鍋爐升壓 |
| 無汽輪機跳機鎖存 | 無保護鎖存 | 超速 / 高振動 / 低真空 / 軸溫高跳機 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop |

> **冷啟動時汽輪機 `INTERLOCKS_OK` 一定是 NOT OK**，這是設計上的順序保護，
> 不是故障。它會在冷凝器真空建立 + 鍋爐升壓完成後自動變 OK
> （實測約在啟動後 135 秒）。

### 9.6 發電機 `generator`（3 項）

| 允許條件 | 成立條件 | NOT OK 的情形 |
| --- | --- | --- |
| 汽輪機未跳機 | `turbine.tripped` < 0.5 | 汽輪機跳機 |
| 無跳機鎖存 | 無保護鎖存 | 過流 / 過頻 / 欠頻 / 逆功率跳機 |
| 緊急停止未啟動 | Coil 00005 = 0 | E-Stop |

**併聯另有六項同步允許**（`SYNC_PERMISSIVE` 30017 = 0x3F 才可閉合斷路器）：

| bit | 條件 |
| ---: | --- |
| 0 | 轉速接近額定（3000 ± 允許帶） |
| 1 | 頻率在範圍內 |
| 2 | 電壓在範圍內 |
| 3 | 相角差小於限值 |
| 4 | 汽輪機未跳機 |
| 5 | 無電氣保護動作 |

任一不成立時送 `BREAKER_CLOSE` → 警報 `5314 SYNC_BLOCKED` + `COMMAND_REJECTED`
（reason=`同步條件不成立`，事件內含 `blocked` 清單）。

### 9.7 給水槽 `feedwater_tank`

被動容器，唯一的允許條件是常數 `True`，**`INTERLOCKS_OK` 永遠為 1**。

### 9.8 速查：NOT OK 的四大類原因

| 類別 | 適用設備 | 判別 |
| --- | --- | --- |
| **順序未到**（上游條件尚未建立） | turbine、boiler、pumps | 正常現象，等順序推進 |
| **跳機未重置** | 全部（除 feedwater_tank） | `TRIP_WORD`(30004) ≠ 0 |
| **緊急停止** | 全部（除 feedwater_tank） | Coil 00005 = 1 |
| **故障注入 / 匯流排異常** | boiler、steam_valve、pumps | `FAULT_INJECT_WORD`(30038)、`SIM_BUS_OK`(10012) |

實測：把 `bus_ok` 強制設為 false 時，8 台設備中**只有鍋爐與汽輪機**的
`INTERLOCKS_OK` 會變 NOT OK（鍋爐因為「模擬匯流排正常」與「給水泵可用」兩項，
汽輪機因為讀不到冷凝器與鍋爐壓力而退回預設值）。其餘六台維持 OK。

---

## 十、案例：冷凝器永遠停在 `ALARM ACTIVE`

### 10.1 先說結論

**這通常不是故障，而是「冷凝器沒有被啟動、真空沒有建立」。**

冷凝器的 `HIGH_PRESSURE` 警報門檻是 **0.15 bar(a)**（`configs/condenser.yaml`），
而冷凝器的**初始壓力是 1.0 bar(a)**（大氣壓，`initial_pressure_bar: 1.0`）。
這個保護**沒有設定 inhibit**，所以設備即使在 OFF 狀態也照樣評估，
於是開機後第一個 scan cycle 就會 `ALARM_SET code=5411`，然後一直亮著，
直到真空真的被抽下來為止。

實測（in-process 機組，`dt=0.1s`）：

| 情境 | 300 秒後的結果 |
| --- | --- |
| **從未啟動冷凝器** | `state=OFF`、`P=1.000 bar(a)`、`ALARM_ACTIVE=1`、作用中警報 = `5411 HIGH_PRESSURE`、`ALARM_WORD_1=0x0001` |
| 已 START，`MANUAL_OUTPUT=100` | 約 **57 秒**後 `P ≤ 0.15` → `5411/5414/5415` 全部 `ALARM_CLEARED`、`ALARM_ACTIVE=0`、狀態 `STARTING → RUNNING` |
| 已 START，但 `MANUAL_OUTPUT=0` | 真空系統仍會抽（真空只看 running 狀態），`P` 降到 0.04，`5411` 清除，但 **`5414 COOLING_WATER_LOW` 永遠亮**（冷卻能力 0% < 80%） |
| 已 START，冷卻水故障注入 30% | `5414 COOLING_WATER_LOW` + `5495 FAULT_INJECTED` 永遠亮 |

### 10.2 您貼的 log 說明了什麼

那段 log **完全是正常的開機瞬態**，看不出任何故障：

```
6.6s condensate_pump SIM_BUS_CONNECTED / ALARM_SET code=5592
6.7s steam_valve, turbine  ALARM_SET code=5892 / 5292
6.8s feedwater_pump, boiler, condenser, feedwater_tank  PARTICIPANT_JOINED
6.9s generator  ALARM_CLEARED code=5392
7.0s feedwater_tank / turbine / feedwater_pump / steam_valve / condensate_pump  ALARM_CLEARED
```

判讀重點：

1. **所有 `5x92` 都是 `SIM_BUS_BAD`**（各設備代碼基底 + 92）。
   `5592`=凝結水泵、`5892`=主蒸汽閥、`5292`=汽輪機、`5692`=給水槽、
   `5792`=給水泵、`5392`=發電機。
2. 這些警報在**設備連上 plant-bus 的瞬間必然發生**：先連上的設備讀不到
   還沒連上的鄰居，訂閱訊號品質是 `BAD` → `5x92` 亮。等大家都上線、
   開始互相發佈程序量後，0.1～0.4 秒內就 `ALARM_CLEARED`。log 裡看到的正是這個過程。
3. **log 裡沒有冷凝器的 `5492`，也沒有鍋爐的 `5192`** —— 兩種可能：
   一是它們的清除事件落在這 25 筆事件視窗之外（HMI 只顯示最近 25 筆），
   二是它們的訂閱訊號還沒 GOOD。用下面的方法可以確定是哪一種。
4. **`SIM_BUS_CONNECTED` 的時間戳都是 `0.0s`，這不是錯誤**。
   設備事件用的是**設備自己的模擬時間**，而該事件發生在收到 WELCOME 的當下，
   設備還沒收到第一個 TICK，所以 `sim_time` 仍是 0。plant-bus 的事件
   （`PARTICIPANT_JOINED`）用的則是**匯流排的模擬時間** 6.6～6.8 秒。兩個時鐘不同。

**所以：您看到的「冷凝器永遠 ALARM」幾乎可以確定不是 `5492`，而是 `5411 HIGH_PRESSURE`。**
`5x92` 那一批在 7.0 秒就清掉了，不會「永遠」。

### 10.3 三步診斷法

```bash
# ① 到底是哪一個警報還在成立？（這是關鍵，別只看 ALARM_ACTIVE）
plantctl read --device condenser --register ALARM_WORD_1
plantctl read --device condenser --register ALARM_WORD_2
```

對照位元：

| 值 | 位元 | 代碼 | 名稱 | 意義 |
| --- | ---: | ---: | --- | --- |
| `ALARM_WORD_1 = 0x0001` | 0 | 5411 | `HIGH_PRESSURE` | **真空沒建立** ← 最常見 |
| `0x0002` | 1 | 5412 | `LOW_HOTWELL_LEVEL` | 熱井水位 < 20% |
| `0x0004` | 2 | 5413 | `HIGH_HOTWELL_LEVEL` | 熱井 > 90% |
| `0x0008` | 3 | 5414 | `COOLING_WATER_LOW` | 冷卻能力 < 80% |
| `0x0010` | 4 | 5415 | `VACUUM_SYSTEM_FAULT` | 真空系統輸出 < 50% |
| `ALARM_WORD_2 = 0x1000` | 12 | 5492 | `SIM_BUS_BAD` | 訂閱訊號品質不良 |
| `ALARM_WORD_2 = 0x8000` | 15 | 5495 | `FAULT_INJECTED` | 有故障注入 |

```bash
# ② 確認是「還在成立」還是「只是沒確認」
plantctl read --device condenser --register STATUS_WORD   # bit 5 = ALARM_ACTIVE
```

`ALARM_WORD` 有位元但 `ALARM_ACTIVE=0` → 只是未確認的歷史，送 `ACK_ALARM` 即可。
`ALARM_ACTIVE=1` → 條件真的還在，ACK 沒用。

```bash
# ③ 看實際程序值
plantctl read --device condenser --register CONDENSER_PRESSURE            # 1.0 = 沒真空
plantctl read --device condenser --register COOLING_WATER_AVAILABILITY    # 0 = 沒下冷卻命令
plantctl read --device condenser --register VACUUM_SYSTEM_OUTPUT
plantctl read --device condenser --register DEVICE_STATE                  # 0=OFF 1=STARTING 2=RUNNING
```

### 10.4 對症處理

| 診斷 | 根因 | 處理 |
| --- | --- | --- |
| `5411` 亮、`DEVICE_STATE=0`、`P=1.0` | **冷凝器根本沒啟動** | 自持設備應該自己啟動，依序檢查 `SELF_HOLD_STATE`(30027)：3=被操作員 STOP 過（下 START）、1=允許條件不成立（讀 `PERMISSIVE_WORD`，通常是熱井水位 ≤ 5%）、0=`SELF_HOLD` 被關掉 |
| `5411` 亮、`DEVICE_STATE=1`(STARTING)、P 正在下降 | **真空建立中** | 正常，等約 57 秒即可（`vacuum_pull_time_s: 60`） |
| `5414` 亮、`COOLING_WATER_AVAILABILITY≈0` | START 了但沒寫 `MANUAL_OUTPUT` | `plantctl write --device condenser --register MANUAL_OUTPUT --value 100` |
| `5414`+`5495` 亮 | 冷卻水故障注入還在 | `plantctl fault clear --target condenser` |
| `5415` 亮且不清 | 真空系統故障注入 `vacuum_system_availability` | 同上清除故障 |
| `5412` 亮 | 熱井水位低 | 開補水閥 `MAKEUP_VALVE_CMD`(40030)，或檢查凝結水泵抽太快 |
| `5492` 亮且**持續超過數秒** | 訂閱訊號（`turbine.exhaust_flow_kg_s`、`condensate_pump.flow_kg_s`）品質不是 GOOD | 見 7.3(b)：查 `offline_devices`、`DEVICE_TICK_TIMEOUT`、`SIM_QUALITY_WORD`(30035) |

### 10.5 為什麼冷凝器不會因此擋住啟動

值得注意的是：即使 `5411` 一直亮，冷凝器的 **`INTERLOCKS_OK` 仍然是 1**
（實測值），因為冷凝器的允許條件只有「熱井水位 > 5%」「無跳機鎖存」「無 E-Stop」，
壓力高不在其中。

真正被擋住的是**汽輪機**：它的允許條件「冷凝器真空良好（≤ 0.15 bar(a)）」不成立，
所以在真空建立之前，汽輪機的 `INTERLOCKS_OK` 一直是 NOT OK，
主蒸汽閥與汽輪機的允許條件「冷凝器真空良好」也會擋下啟動（`PERMISSIVE_WORD` 對應位元為 0）。

**這條連鎖就是這座電廠最典型的「一個警報 + 一個 interlock」組合**：
冷凝器發警報，汽輪機擋啟動。

---

## 十一、附錄：代碼與位址速查

### 11.1 設備代碼基底

| 設備 | 基底 | 跳機 | 警報 | 共通警報 |
| --- | ---: | --- | --- | --- |
| boiler | 5100 | 5101–5104 | 5111–5117 | 5190–5195 |
| turbine | 5200 | 5201–5204 | 5211–5215 | 5290–5295 |
| generator | 5300 | 5301–5304 | 5311–5316 | 5390–5395 |
| condenser | 5400 | 5401–5403（皆不跳機） | 5411–5415 | 5490–5495 |
| condensate_pump | 5500 | 5501–5503 | 5511–5514 | 5590–5595 |
| feedwater_tank | 5600 | 5601–5602（皆不跳機） | 5611–5614 | 5690–5695 |
| feedwater_pump | 5700 | 5701–5703 | 5711–5714 | 5790–5795 |
| steam_valve | 5800 | 5801–5802 | 5811–5814 | 5890–5895 |

共通代碼（`5x90`～`5x99`）：

| 尾碼 | 類型 | 名稱 | Alarm Word |
| ---: | --- | --- | --- |
| 90 | 警報 | `COMMAND_REJECTED` | Word 2, bit 10 |
| 91 | 警報 | `CONTROL_WATCHDOG_LOST` | Word 2, bit 11 |
| 92 | 警報 | `SIM_BUS_BAD` | Word 2, bit 12 |
| 93 | 警報 | `SENSOR_FAULT` | Word 2, bit 13 |
| 94 | 警報 | `ACTUATOR_FAULT` | Word 2, bit 14 |
| 95 | 警報 | `FAULT_INJECTED` | Word 2, bit 15 |
| 97 | 跳機 | `COMM_TIMEOUT_TRIP` | — |
| 98 | 跳機 | `TRIP_TEST` | — |
| 99 | 跳機 | `EMERGENCY_STOP` | — |

### 11.2 每台設備都有的位址

**Coils（可寫）**

| 文件位址 | offset | 名稱 | 型態 |
| ---: | ---: | --- | --- |
| 00001 | 0 | `START` | 脈衝 |
| 00002 | 1 | `STOP` | 脈衝 |
| 00003 | 2 | `RESET_TRIP` | 脈衝（需 Reset Key） |
| 00004 | 3 | `ACK_ALARM` | 脈衝 |
| 00005 | 4 | `EMERGENCY_STOP` | **保持型** |
| 00006 | 5 | `FORCE_SAFE` | 保持型 |
| 00007 | 6 | `TRIP_TEST` | 脈衝（需 LAB_MODE） |
| 00008 | 7 | `CLEAR_TOTALIZER` | 脈衝 |

**Holding Registers（可寫）**

| 文件位址 | offset | 名稱 | 備註 |
| ---: | ---: | --- | --- |
| 40001 | 0 | `CONTROL_MODE` | 0–4 |
| 40002 | 1 | `COMMAND_SEQUENCE` | 重置需為新值 |
| 40003 | 2 | `WATCHDOG_COUNTER` | 控制器每秒 +1 |
| 40004 | 3 | `RESET_KEY` | 42330 (0xA55A) |
| 40007 | 6 | `COMMAND_LEASE_TIME` | 秒 |
| 40010 | 9 | `PRIMARY_SETPOINT` | 單位依設備 |
| 40011 | 10 | `SECONDARY_SETPOINT` | 單位依設備 |
| 40012 | 11 | `MANUAL_OUTPUT` | %（發電機為 MW） |
| 40013 / 40014 | 12 / 13 | `OUTPUT_HIGH/LOW_LIMIT` | % |
| 40020–40025 | 19–24 | PID 參數、死區、積分限幅、掃描時間 | |

**Input Registers（唯讀，重點）**

| 文件位址 | offset | 名稱 |
| ---: | ---: | --- |
| 30001 | 0 | `STATUS_WORD` |
| 30002 / 30003 | 1 / 2 | `ALARM_WORD_1` / `ALARM_WORD_2` |
| 30004 | 3 | `TRIP_WORD` |
| 30005 | 4 | `DEVICE_STATE` |
| 30006 | 5 | `FIRST_OUT_CODE` |
| 30007 | 6 | `OVERALL_QUALITY` |
| 30030 | 29 | `WATCHDOG_ECHO` |
| 30031 | 30 | `SCAN_TIME_MS` |
| 30033 | 32 | `REJECTED_COMMAND_COUNT` |
| 30034 | 33 | `EXCEPTION_COUNT` |
| 30035 | 34 | `SIM_QUALITY_WORD` |
| 30037 | 36 | `COMM_LOSS_SECONDS` |
| 30038 | 37 | `FAULT_INJECT_WORD` |
| 30039 | 38 | `SNAPSHOT_GENERATION` |
| 30043 | 42 | `TRIP_COUNT` |

各設備專屬的程序量從 **30010** 開始，完整清單見 [`register-map.csv`](register-map.csv)。

> **位址換算**：文件位址 `40010` 對應 PDU offset **9**，
> `30010` 對應 offset **9**，`10013` 對應 offset **12**，`00003` 對應 offset **2**。
> 位址算錯是 Exception 02 的頭號原因。
>
> 本手冊的 Coil 採 `0xxxx` 五位補零寫法（`00003`）；
> [`register-map.csv`](register-map.csv) 的 `doc_address` 欄則直接寫 `3`，兩者是同一個位址。
> `plantctl` 與 `pymodbus` 一律使用 **PDU offset**，不要把 `40010` 直接送進去。

### 11.3 `DEVICE_STATE` (30005) 列舉

`0=OFF 1=STARTING 2=RUNNING 3=STOPPING 4=TRIPPED 5=PURGING 6=IGNITING 7=PRESSURIZING 8=MAINTENANCE 9=SAFE_HOLD`

### 11.4 常用診斷指令彙整

```bash
plantctl status                                  # 全廠狀態、離線設備
plantctl watch                                   # 重點程序量即時追蹤
plantctl events --limit 50                       # 最近事件
plantctl events --event TRIP_LATCHED             # 只看跳機
plantctl events --event COMMAND_REJECTED         # 只看被拒命令
plantctl events --event SEQUENCE_BLOCKED         # 順序卡在哪
plantctl events --event FIRST_OUT                # 第一故障原因
plantctl read  --device X --register ALARM_WORD_1
plantctl pause | resume | step 10 | speed 5      # 暫停、單步、加速
plantctl snapshot save baseline -d "基準"
plantctl snapshot restore baseline --clean       # 毫秒級重置，容器不重啟
```

### 11.5 情境驗收

```bash
plantctl scenario run scenarios/normal_startup.yaml      # 正常冷啟動
plantctl scenario run scenarios/load_step.yaml           # 負載變動
plantctl scenario run scenarios/cooling_loss.yaml        # 冷卻水喪失
plantctl scenario run scenarios/feedwater_pump_trip.yaml # 給水泵跳機
plantctl scenario run scenarios/load_rejection.yaml      # 甩負載
plantctl scenario run scenarios/valve_stuck_open.yaml    # 閥門卡開
plantctl scenario run scenarios/sensor_bias.yaml         # 感測器偏差
plantctl scenario run scenarios/snapshot_roundtrip.yaml  # 快照往返
```

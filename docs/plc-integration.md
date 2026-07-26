# 外部 PLC／DCS 串接指南

本文說明「控制器」要如何透過 Modbus TCP 接管這 8 台設備。
內建 DCS（`controller/dcs/main.py`）就是這份規格的參考實作，可直接對照原始碼閱讀。

---

## 0. 先釐清方向：PLC 有兩個面

```
   SCADA / HMI ──(北向：PLC 當 Modbus Server)──▶ 你的 PLC ──(南向：PLC 當 Modbus Client)──▶ 8 台設備
```

* **南向（必要）**：PLC 是 **Client／Master**，主動輪詢 8 台設備、下命令。本文主要在講這一段。
* **北向（選配）**：PLC 自己再開一個 Modbus Server 給上位系統讀。

> 注意：內建 DCS **只做南向**，它沒有開 Modbus Server。
> `compose.yaml` 為 `dcs-plc` 保留了 `15020:502` 的埠對映，但預設沒有行程在聽。
> 要做北向就自己用 `common/modbus/server.py` 的 `ModbusTcpServer`（第 9 節）。

設備端不是「被動記憶體」：每台設備自己跑物理模型、狀態機、保護邏輯，
**PLC 寫下去的命令只是請求**，設備會依安全邏輯決定接不接受。

---

## 1. 連線資訊

| 項目 | 值 |
| --- | --- |
| 協定 | Modbus TCP |
| 容器內埠 | `502`（全部設備一致） |
| Unit ID | `1`（`configs/plant.yaml` 的 `modbus.unit_id`）；`0` 與 `0xFF` 也接受，其他回 Exception 0B |
| 網路 | `control_net`（Modbus 專用），設備間物理量走 `sim_net`，PLC 拿不到也不該拿 |
| 最大連線 | 32（`modbus.max_clients`） |

**從容器內連**（推薦，PLC 也做成 compose service）：直接用服務名，
`condenser:502`、`condensate-pump:502`、`feedwater-tank:502`、`feedwater-pump:502`、
`boiler:502`、`steam-valve:502`、`turbine:502`、`generator:502`。

**從主機連**：用 `127.0.0.1` + 對映埠 15021~15028（見 README 埠表）。

啟動不含內建 DCS 的環境，把控制權留給你的 PLC：

```bash
docker compose --profile external-plc up --build -d
```

---

## 2. 介面契約在哪裡

`docs/register-map.csv` 是唯一的介面契約來源（688 筆），欄位：

```
device,table,doc_address,pdu_offset,name,dtype,unit,scale,min,max,writable,pulse,description
```

* `doc_address` 是文件地址（3xxxx/4xxxx/1xxxx/0xxxx），`pdu_offset` 才是**線上真正要送的位址**。
  例：鍋爐壓力文件寫 30010，PDU offset 是 **9**，用 FC 04 讀位址 9。
* `scale`：工程值 = 原始值 ÷ scale。壓力 scale=100 → 讀到 10000 表示 100.00 bar(a)。
* `dtype`：`u16` / `i16`（≥0x8000 要減 0x10000）/ `u32`（**高 word 在前**，佔 2 個暫存器）/
  `bitfield` / `enum`。
* `min`/`max`：只有 Holding 有，寫入超出範圍 → Exception 03。
* `writable=no` 的位址寫下去 → Exception 02（不會靜默接受）。

**不要把 CSV 的內容硬編碼進 PLC**，載入它、用名稱查位址，日後 map 改版才不會全錯。
CSV 可用 `python -m tools.export_docs` 重新產生。

四張表的分工：

| 表 | FC | 用途 |
| --- | --- | --- |
| Discrete Input `1xxxx` | 02 | 16 個狀態位元：READY / RUNNING / TRIPPED / ALARM_ACTIVE / CONTROL_WATCHDOG_OK / INTERLOCKS_OK … |
| Input Register `3xxxx` | 04 | 全部程序量與診斷值（唯讀） |
| Coil `0xxxx` | 01 / 05 / 15 | 命令：START / STOP / RESET_TRIP / ACK_ALARM / EMERGENCY_STOP / FORCE_SAFE … |
| Holding Register `4xxxx` | 03 / 06 / 16 / 22 / 23 | 設定值、模式、watchdog、控制權欄位 |

前 9 個 Input Register 與前 9 個 Holding Register 每台設備完全一致（通用區），
30010 起、40010 起才是各設備專屬。這讓 PLC 可以寫一套通用邏輯處理 8 台。

---

## 3. 取得控制權：四道關卡

寫入被接受之前要同時滿足這四件事，任何一項不成立就會被拒絕：

### 3.1 CONTROL_MODE（40001）必須是 REMOTE

| 值 | 模式 | 遠端可寫？ |
| ---: | --- | --- |
| 0 | LOCAL_MANUAL | ✗ |
| 1 | LOCAL_AUTO | ✗ |
| 2 | REMOTE_MANUAL | ✓ 用 MANUAL_OUTPUT |
| 3 | REMOTE_AUTO | ✓ 用 PRIMARY_SETPOINT（預設值） |
| 4 | MAINTENANCE | ✗ 拒絕 START |

開機預設是 **3 = REMOTE_AUTO**（`configs/plant.yaml` 的 `control.default_mode`）。

### 3.2 單一寫入者租約（Single Writer Lease）

設備用**來源 IP** 認定控制權：

* 沒有有效租約時，第一個寫入的 IP 取得控制權，租約 **5 秒**（`modbus.lease_seconds`）。
* 同一 IP 每次寫入都會自動續租。
* 租約有效期間，**別的 IP 寫入一律回 Exception 06（Server Device Busy）**。
* 租約過期（5 秒沒人寫）後才會釋出。

實務意義：PLC 要**持續寫**（至少每秒 kick 一次 watchdog）才不會被別人搶走控制權；
反過來說，測試工具想插手就得等 PLC 停寫超過 5 秒。
`EMERGENCY_STOP`／`FORCE_SAFE` coil 可設定成安全來源白名單（`modbus.safety_allowlist`）
而不受租約限制。此特權只涵蓋**整批位址都是安全線圈**的寫入：若用 FC15 一次寫入的範圍
混進 `START`、`RESET_TRIP` 等非安全線圈，整批會退回一般寫入規則檢查（`write_allowlist`
與單一寫入者租約），避免安全來源藉由批次寫入取得完整控制權。

### 3.3 控制器 watchdog（40003 → 30030）

* PLC **每秒**把 `WATCHDOG_COUNTER`（40003）寫成新值（遞增、跳過 0）。
* 設備把收到的值原樣回填 `WATCHDOG_ECHO`（30030），PLC 可比對確認鏈路真的通。
* 超過 **3 秒**（`comm.watchdog_timeout`）沒變 → `CONTROL_WATCHDOG_OK`（10011）轉 false、
  發 `CONTROL_WATCHDOG_LOST` 事件、警報碼 `+91`。
* 再過 `hold_seconds`（2 秒）就執行該設備的**通訊失效策略**（第 8 節）。

### 3.4 命令序號（40002）

`RESET_TRIP` 這類「一次性」命令要求 `COMMAND_SEQUENCE` 是**沒用過的新值**，
防止重放同一個封包造成重複重置。PLC 每下一次重置就 +1。

---

## 4. 標準掃描週期

內建 DCS 的節奏（`configs/dcs.yaml`）：輪詢 `poll_s: 0.25` 秒、PID `pid_scan_s: 0.5` 秒、
watchdog 1 秒。建議照抄這個結構：

```
每 250 ms：讀 → 更新快取
  FC 04  位址 0, 數量 50   Input Registers
  FC 02  位址 0, 數量 16   Discrete Inputs
  FC 03  位址 0, 數量 32   Holding Registers（讀回自己寫的值，確認被接受）

每 500 ms：算 → 寫
  跳機矩陣 → PID → 寫 Holding / 脈衝 Coil

每 1 s：WATCHDOG_COUNTER +1（同時續租寫入控制權）
```

**用整段批次讀，不要一個一個讀。** 三次請求就能拿到一台設備的完整狀態，
而且暫存器映像是「一次 scan 產生一份、整份替換」的不可變快照，
所以一次讀 50 個暫存器**不會出現撕裂讀取**（讀到一半被更新）。

寫入的時序要特別注意：

> **寫入一律先進 command queue，下一個 scan cycle（0.1 秒）才由狀態機與安全邏輯決定是否套用。**

所以寫完立刻讀回去可能還是舊值，PLC 不能用「寫完馬上讀」來判斷成功；
要看的是**動作有沒有發生**（DEVICE_STATE、RUNNING、ACCEPTED_COMMAND_COUNT／REJECTED_COMMAND_COUNT）。

---

## 5. 下命令的規則

### 5.1 Holding Register：設定值

```
FC 06（單筆）或 FC 16（多筆）
raw = round(工程值 × scale)，超出 min/max → Exception 03
```

各設備的可寫暫存器（通用 40001~40014、40020~40025 之外）：

| 設備 | 主要控制點 | 備註 |
| --- | --- | --- |
| condenser | `MANUAL_OUTPUT` % 冷卻水量、`MAKEUP_VALVE_CMD` % | |
| condensate_pump | `MANUAL_OUTPUT` % 泵速 | |
| feedwater_tank | `MANUAL_OUTPUT` %、`HEATING_SETPOINT` °C | |
| feedwater_pump | `MANUAL_OUTPUT` % 泵速、`OUTLET_VALVE_CMD` % | 啟動前要先開出口閥 |
| boiler | `MANUAL_OUTPUT` % 燃燒器、`PRIMARY_SETPOINT` bar(a)、`BLOWDOWN_VALVE_CMD` % | |
| steam_valve | `MANUAL_OUTPUT` % 閥位、`OPEN_RATE`／`CLOSE_RATE` %/s | |
| turbine | `MANUAL_OUTPUT` %、`INERTIA_PARAM`、`DAMPING_PARAM` | |
| generator | `PRIMARY_SETPOINT` MW 負載、`OPERATING_MODE` 0/1、`LOAD_RATE_LIMIT` MW/s | |

### 5.2 Coil：命令是「脈衝」

`START / STOP / RESET_TRIP / ACK_ALARM / TRIP_TEST / CLEAR_TOTALIZER` 都是脈衝型：
**寫 True 就好，設備處理完會自己清成 False，PLC 不需要（也不該）再寫 False。**

`EMERGENCY_STOP` 與 `FORCE_SAFE` 是**保持型**，寫 True 會一直有效，要自己寫 False 解除。
`generator` 另有 `BREAKER_CLOSE`（0x000A）／`BREAKER_OPEN`（0x000B）兩個脈衝 coil。

### 5.3 START 的允許條件

寫 START coil 後，設備會檢查（不通過就發 `COMMAND_REJECTED` 事件並記入 `REJECTED_COMMAND_COUNT`）：

1. 沒有未重置的跳機鎖存
2. 緊急停止未啟動
3. 不在維修模式
4. 該設備自己的 start permissives（例如熱井水位 ≥ 20% 才能啟動凝結水泵）

### 5.4 跳機重置：四個條件缺一不可

```
1. 40004 RESET_KEY       = 0xA55A (42330)
2. 40002 COMMAND_SEQUENCE = 沒用過的新值
3. 0x0003 RESET_TRIP      脈衝 True
4. 安全條件成立（跳機條件已消失）+ 緊急停止已解除
```

只有 `active` 會隨條件消失自動清除，`latched` 一定要靠這個流程解。
重置成功會發 `TRIP_RESET` 事件，設備回到 `OFF`，然後才能重新 START。

---

## 6. 例外碼處理

PLC 必須把這些當**正常回應**處理，不能因為收到例外就斷線重連：

| 例外 | 意義 | PLC 該做什麼 |
| ---: | --- | --- |
| 01 | 功能碼未支援 | 程式邏輯錯誤，改用 01/02/03/04/05/06/15/16/22/23/43-14 |
| 02 | 位址不存在，或寫入唯讀區 | 檢查 PDU offset 是否用成文件地址 |
| 03 | 數值超出工程範圍，或 count 非法 | clamp 到 min/max 再送 |
| 04 | 設備內部錯誤 | 記錄並重試 |
| **06** | **設備忙碌／租約在別人手上** | **等待後重試，這是最常見的**（scan 進行中或別的 IP 有控制權） |
| 0B | Unit ID 不對 | 改成 1 |

**設備不會因為回例外就關閉連線**，PLC 也不該關。真正需要重連的只有 socket 層錯誤。

診斷用暫存器（每台都有）：

| 位址 | 名稱 | 用途 |
| --- | --- | --- |
| 30032 | `MODBUS_REQUEST_COUNT` | 設備收到的請求數（模 65536） |
| 30033 | `REJECTED_COMMAND_COUNT` | **命令被安全邏輯拒絕的次數 → PLC 最該監控的值** |
| 30034 | `EXCEPTION_COUNT` | 回過的例外數 |
| 30049 | `ACCEPTED_COMMAND_COUNT` | 接受的命令數 |
| 30037 | `COMM_LOSS_SECONDS` | 目前通訊中斷秒數 |
| 30039 | `SNAPSHOT_GENERATION` | 每次快照還原 +1，PLC 可據此重置內部狀態（積分項！） |

> **快照還原後 PLC 要自己重置積分項**：設備狀態被瞬間換掉，PID 內部的積分累積若不跟著處理，
> 會在還原後暴衝。內建 DCS 是透過參與快照協議來解決（第 10 節）。

---

## 7. 控制迴路怎麼串

設備之間的物理耦合走 `sim_net`，PLC 看不到也不需要看；
PLC 要做的是「從 A 設備讀 PV，寫到 B 設備的 MV」。內建 DCS 的 5 個迴路：

| # | 迴路 | PV（從哪讀） | MV（寫到哪） | SP |
| --- | --- | --- | --- | --- |
| 1 | 鍋爐壓力 | `boiler.BOILER_PRESSURE` | `boiler.MANUAL_OUTPUT`（燃燒器 %） | 100 bar(a) |
| 2 | 鍋爐水位（三元素） | `boiler.LEVEL_INDICATED` + `STEAM_OUTFLOW` 前饋 − `FEEDWATER_FLOW` 回授 | `feedwater_pump.MANUAL_OUTPUT`（泵速 %） | 66.7% |
| 3 | 給水槽水位 | `feedwater_tank.TANK_LEVEL` | `condensate_pump.MANUAL_OUTPUT` | 60% |
| 4 | 汽輪機轉速 | `turbine.SPEED_RPM` | `steam_valve.MANUAL_OUTPUT`（閥位 %） | 3000 RPM |
| 5 | 負載（併網後） | `generator.ELECTRICAL_POWER` | `steam_valve.MANUAL_OUTPUT` | `generator.LOAD_DEMAND` |

迴路 4 與 5 **共用同一個 MV**：`generator.OPERATING_MODE ≥ 1`（併網）時用負載控制，
否則用轉速控制。切換時務必做 bumpless transfer（把新 PID 的積分項預載成目前輸出），
`controller/pid.py` 的 `to_auto(current_output)` 就是幹這個的。

三個必須寫死在 PLC 裡、優先於任何 PID 的安全邏輯：

```python
# 1. 超速無條件關閥（門檻 3150 RPM，比設備跳機門檻 3300 早）
if turbine.SPEED_RPM > 3150 or turbine.TRIPPED:
    steam_valve.MANUAL_OUTPUT = 0

# 2. 鍋爐跳機時燃燒器歸零，且積分項要一起清掉
if boiler.TRIPPED:
    boiler.MANUAL_OUTPUT = 0; pressure_pid.force_output(0)

# 3. 設備說不准給水就不准給
if boiler.FEEDWATER_PERMITTED < 1:
    feedwater_pump.MANUAL_OUTPUT = 0; flow_pid.force_output(0)
```

啟動期間還要加兩個限幅，否則一定過衝：升壓時燃燒器 ≤ 20%、升速時閥位 ≤ 15%
（`dcs.startup_burner_max_pct` / `startup_valve_max_pct`）。

### 跳機矩陣（第二層防護）

設備之間已經有 `sim_net` 互鎖，但 PLC 應該再做一層。偵測 `TRIPPED`（DI 10005）**上升緣**時：

| 來源跳機 | PLC 應執行 |
| --- | --- |
| turbine | `generator` 脈衝 `BREAKER_OPEN`、`steam_valve.MANUAL_OUTPUT=0`、`generator.PRIMARY_SETPOINT=0` |
| boiler | `boiler.MANUAL_OUTPUT=0`、`steam_valve.MANUAL_OUTPUT=0` |
| condenser | `generator.PRIMARY_SETPOINT=0`（降載） |
| feedwater_pump | `boiler.MANUAL_OUTPUT=0`（避免低水位） |
| condensate_pump | `feedwater_pump.MANUAL_OUTPUT=20`（保護給水槽） |
| steam_valve | `boiler.MANUAL_OUTPUT=0` |

只在邊緣觸發一次，不要每個 scan 重送（會蓋掉操作員的手動處置）。
完整規則見 `controller/trip_matrix.py`。

---

## 8. 通訊失效：每台設備的行為不同

PLC 掛掉或網路斷了，設備不會傻等。watchdog 逾時 + hold 2 秒後各自執行：

| 設備 | 策略 | 行為 |
| --- | --- | --- |
| boiler | `FAIL_LOW` | 燃燒器立即降到 0% |
| turbine | `FAIL_LOW` | 進入安全減速 |
| steam_valve | `FAIL_CLOSE` | 閥門關閉（預設失效位置） |
| feedwater_pump / feedwater_tank / generator | `HOLD_LAST` | 維持最後命令 |
| condenser / condensate_pump | `LOCAL_FALLBACK` | 切回本地自動控制 |

PLC 重新連上後要注意：**設備的輸出已經被改掉了**，PID 積分項與實際輸出脫節。
恢復前先讀回目前的 `BURNER_OUTPUT` / `ACTUAL_SPEED` / `VALVE_POSITION`，
用 `to_auto(目前輸出)` 做 bumpless 再接手。

---

## 9. 北向：把 PLC 自己做成 Modbus Server

要讓 SCADA/HMI 讀你的 PLC，用專案內建的 server（`common/modbus/server.py`），
它已經處理好例外碼、租約、不可變映像、43-14 裝置識別：

```python
from common.modbus.server import ModbusTcpServer, RegisterImage, AccessPolicy
from common.modbus.register_map import RegisterMap, Table

rmap = RegisterMap.build("plc", PROCESS_INPUTS, EXTRA_HOLDINGS, EXTRA_COILS)

def image_provider() -> RegisterImage:
    # 每個 scan 產生一份新的不可變映像，讀取端只取參考 -> 不會撕裂
    return RegisterImage(coils=(...), discretes=(...), inputs=(...), holdings=(...))

def write_handler(req) -> "ModbusException | None":
    plc.cmd_queue.append(req)   # 進佇列，下個 scan 才套用
    return None                 # 回 None = 接受；回 ModbusException.X = 拒絕

server = ModbusTcpServer(
    rmap, image_provider, write_handler,
    unit_id=1, host="0.0.0.0", port=502,
    busy_provider=lambda: plc.scanning,          # scan 中 -> 回 Exception 06
    access=AccessPolicy(lease_seconds=5.0, enforce_single_writer=True),
)
await server.start()
```

北向 map 建議只暴露聚合值（全廠負載、機組狀態、順序步驟、第一故障），
不要把 8 台設備的暫存器原樣轉發 —— 那樣 SCADA 會繞過 PLC 的安全邏輯。

---

## 10. 參與快照協議（強烈建議）

PLC 若不參與快照，「秒級重置環境」對它就是失效的：設備回到 60 MW 穩態，
PLC 的積分項、順序步驟、跳機矩陣旗標卻停在別的地方。

做法是以 `Role.CONTROLLER` 連上 plant-bus（`common/simbus/client.py`），
處理三種訊息：

| 訊息 | PLC 要做什麼 |
| --- | --- |
| `TICK` | 更新模擬時間（事件時戳用它，不要用真實時間） |
| `PAUSE` / `RESUME` | 暫停／恢復控制運算（暫停時不可繼續積分） |
| `SNAPSHOT_SAVE` | 回傳 `SNAPSHOT_DATA`：PID 積分項與 auto 狀態、順序步驟、跳機矩陣已觸發集合、模式 |
| `SNAPSHOT_RESTORE` | 套用後回 `RESTORE_ACK{ok, error}` |

參考 `controller/dcs/main.py` 的 `snapshot_state()` / `restore_state()`（約 30 行）。

不想接 plant-bus 的最低限度做法：輪詢 `SNAPSHOT_GENERATION`（30039），
發現變了就把所有 PID 重置並用當下輸出重新 `to_auto()`。

---

## 11. 冷啟動順序（PLC 要實作的 15 步）

每步都有「進入動作 / 完成條件 / 順序禁止（guard）」。guard 不成立就停在原步驟並拒絕前進 ——
這是順序控制的重點，不是「等時間到就往下走」。

| # | 步驟 | 進入動作 | 完成條件 | 順序禁止 |
| ---: | --- | --- | --- | --- |
| 1 | COOLING_WATER | condenser `MANUAL_OUTPUT=100` + START | `COOLING_WATER_AVAILABILITY > 90%` | |
| 2 | PULL_VACUUM | — | `CONDENSER_PRESSURE ≤ 0.12 bar(a)` | |
| 3 | CHECK_HOTWELL | — | `HOTWELL_LEVEL ≥ 20%` | |
| 4 | START_CONDENSATE_PUMP | `MANUAL_OUTPUT=40` + START | `RUNNING` | 熱井 < 20% 禁止啟動 |
| 5 | TANK_LEVEL | 水槽迴路轉 auto | `｜TANK_LEVEL − 60｜< 5%` | |
| 6 | START_FEEDWATER_PUMP | `OUTLET_VALVE_CMD=100`、`MANUAL_OUTPUT=40` + START | `RUNNING` | 給水槽 < 25% 禁止啟動 |
| 7 | BOILER_LEVEL | 三元素迴路轉 auto | `｜LEVEL_INDICATED − 66.7｜< 5%` | |
| 8 | BOILER_PURGE | boiler START | `DEVICE_STATE ∈ {2,6,7}` | 水位不在 30~80% 禁止點火 |
| 9 | IGNITE | `MANUAL_OUTPUT=15` | `FLAME_STATUS ≥ 2`（穩定） | |
| 10 | RAISE_PRESSURE | 壓力迴路轉 auto | `BOILER_PRESSURE ≥ 30 bar(a)` | |
| 11 | OPEN_MSV | steam_valve START + turbine START，轉速迴路轉 auto | `SPEED_RPM > 300` | 冷凝器 > 0.15 bar(a) 禁止啟動汽輪機 |
| 12 | RUN_UP | — | `｜SPEED_RPM − 3000｜ ≤ 30` | |
| 13 | SYNC_CHECK | — | `SYNC_PERMISSIVE == 0x3F`（6 個條件全滿足） | |
| 14 | CLOSE_BREAKER | generator START + `BREAKER_CLOSE` | `BREAKER_STATUS ≥ 1` | 轉速偏差 > 30 RPM 禁止併網 |
| 15 | RAMP_LOAD | `PRIMARY_SETPOINT = 60 MW` | `ELECTRICAL_POWER ≥ 57 MW` | |

每步都要有 timeout（120~1800 秒不等），逾時就停在該步並發事件，不要無限等待。
完整實作見 `controller/startup_sequence.py`。

---

## 12. 最小可執行範例

`examples/external_plc.py` 是一支不依賴任何設備類別、
純粹靠 `docs/register-map.csv` 當契約的外部 PLC 骨架，示範本文所有機制：

```bash
# 環境先起來（不含內建 DCS，把控制權留給你的 PLC）
docker compose --profile external-plc up -d

# 從主機執行（自動用 15021~15028 埠）
python examples/external_plc.py --host 127.0.0.1

# 只監看不寫入（不搶控制權）
python examples/external_plc.py --host 127.0.0.1 --read-only

# 只接一台設備做實驗
python examples/external_plc.py --host 127.0.0.1 --only boiler
```

它做了：CSV 驅動的位址解析、批次輪詢、watchdog 續租、例外碼分類處理、
跳機矩陣邊緣觸發、鍋爐壓力 PID、超速強制關閥、`SNAPSHOT_GENERATION` 變化時重置積分項。
拿它當骨架改成自己的控制策略即可。

---

## 13. 串接檢查清單

- [ ] 用 PDU offset 而不是文件地址送出請求
- [ ] 工程值有乘 `scale`、u32 有處理高 word 在前、i16 有處理負數
- [ ] 批次讀（IR 0~49 / DI 0~15 / HR 0~31），不逐點讀
- [ ] 每秒寫 `WATCHDOG_COUNTER`，並比對 `WATCHDOG_ECHO`
- [ ] 收到 Exception 06 會退讓重試，不會斷線
- [ ] 收到任何例外都不關閉 TCP 連線
- [ ] 命令是脈衝，不會補寫 False
- [ ] 重置有備齊 Reset Key + 新序號 + 安全條件
- [ ] 監看 `REJECTED_COMMAND_COUNT`，被拒絕會記錄原因
- [ ] 超速 3150 RPM 關閥的安全邏輯**寫在 PID 之外**且優先
- [ ] 跳機矩陣只在邊緣觸發一次
- [ ] `SNAPSHOT_GENERATION` 變化時重置 PID 積分項
- [ ] 通訊恢復後用 bumpless transfer 接手，不是直接切回 auto

相關文件：`docs/architecture.md`、`docs/sequence-of-operation.md`、`docs/register-map.csv`、
`docs/alarm-codes.csv`、`TESTING.md`。

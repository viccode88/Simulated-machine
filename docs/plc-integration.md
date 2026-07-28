# 外部 PLC 串接指南

本文說明 PLC 要如何透過 Modbus TCP 與這 8 台**自持設備**交換資料。

設備自己啟動、自己調節、自己保護，因此 PLC 的工作只有兩件事：
**資料交換**（南向輪詢、北向提供給 SCADA）與**邏輯判斷**（watchdog、脈衝展寬、
跳機矩陣、互鎖判斷）。**不要**在 PLC 裡寫 PID 或設定值——那會跟設備自己的
調節器互相打架。

參考實作有兩個，可直接對照原始碼閱讀：

* `integrations/openplc/thermal-plant-v4/pous/programs/main.st`（OpenPLC v4，預設）
* `examples/external_plc.py`（Python 骨架，只用 `docs/register-map.csv`）

---

## 0. 先釐清方向：PLC 有兩個面

```
   SCADA / HMI ──(北向：PLC 當 Modbus Server)──▶ 你的 PLC ──(南向：PLC 當 Modbus Client)──▶ 8 台設備
```

* **南向（必要）**：PLC 是 **Client／Master**，主動輪詢 8 台設備、下命令。本文主要在講這一段。
* **北向（選配）**：PLC 自己再開一個 Modbus Server 給上位系統讀。

> 預設的 OpenPLC 服務兩面都做：南向輪詢 8 台設備，北向在 502 開 Modbus Server，
> 由 `compose.yaml` 對映到主機的 `15020`。SCADA 一律連 PLC，不要直連設備
> （繞過 PLC 寫入會與 PLC 爭用單一寫入者租約，收到 Exception 06）。

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

啟動不含 OpenPLC 的環境，把 control_net 留給你自己的 PLC：

```bash
COMPOSE_PROFILES=no-plc docker compose up --build -d
```

設備照樣會自持運轉；你的 PLC 接上去之後負責資料交換與邏輯判斷即可。

---

## 2. 介面契約在哪裡

`docs/register-map.csv` 是唯一的介面契約來源（714 筆），欄位：

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
| 3 | REMOTE_AUTO | ✓ 用 PRIMARY_SETPOINT |
| 4 | MAINTENANCE | ✗ 拒絕 START |

開機預設是 **1 = LOCAL_AUTO**（`configs/plant.yaml` 的 `control.default_mode`）：
自持設備由本地調節器決定輸出，`MANUAL_OUTPUT` 在 AUTO 模式下被忽略。
PLC 不需要（也不應該）改這個值；要人工接管才寫 `0 = LOCAL_MANUAL`。

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

OpenPLC 專案的節奏（`tools/generate_openplc_gateway.py`）：cyclic task 20 ms、
南向讀取 250 ms、命令寫入 100 ms、watchdog 200 ms。建議照抄這個結構：

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

> **快照還原後 PLC 要自己重置邊緣記憶**：設備狀態被瞬間換掉，跳機邊緣、脈衝計時
> 這類內部狀態若不跟著重置，會誤判或漏判。控制器的積分項不必再擔心——調節器在
> 設備內，會跟著快照一起還原。

---

## 7. PLC 要做的邏輯（不是控制）

設備之間的物理耦合走 `sim_net`，PLC 看不到也不需要看；設備的閉迴路調節也在
設備內，PLC 同樣不需要參與。PLC 該做的是這四件事：

### 7.1 watchdog 與鏈路確認

每 200 ms 把每台設備的 `WATCHDOG_COUNTER`（40003）寫成新值（遞增、跳過 0），
再比對 `WATCHDOG_ECHO`（30030）有沒有跟著動。連續 3 秒沒動＝該設備通訊失效，
點亮 HMI 上的狀態即可——設備是自持的，**不需要**（也不該）為此改動它的輸出。

### 7.2 脈衝展寬

HMI 只送一個 `true`。PLC 把 `START`／`STOP`／`RESET_TRIP`／`ACK_ALARM`／
`TRIP_TEST`／`CLEAR_TOTALIZER`／`BREAKER_CLOSE`／`BREAKER_OPEN` 保持約 160 ms
（要大於南向寫入週期）再自動清零。`EMERGENCY_STOP` 與 `FORCE_SAFE` 是保持型，
**永遠不要自動清除**，必須由操作員明確解除。

`RESET_TRIP` 上升緣時，PLC 要一併填入 `RESET_KEY = 0xA55A` 並把
`COMMAND_SEQUENCE` 遞增（跳過 0）；脈衝結束後把 Reset Key 清回 0。

### 7.3 只寫命令，不寫控制值

PLC 的南向寫入應該只有：

| 寫什麼 | FC | 位址 |
| --- | --- | --- |
| 命令線圈 | 15 | Coil 0~7（發電機另有 9、10） |
| 命令暫存器 | 16 | Holding 1~3（`COMMAND_SEQUENCE` / `WATCHDOG_COUNTER` / `RESET_KEY`） |

設定值（40010/40011）、手動輸出（40012）、輸出限幅、PID 參數**都不要寫**。
它們仍然是可寫的（工程人員偶爾需要改目標負載），但那是刻意的人工動作，
不是 PLC 的週期性行為。北向地圖的 `plc_forwards` 欄位就是這條界線。

### 7.4 跳機矩陣（第二層防護）

設備之間已經有 `sim_net` 互鎖，但 PLC 應該再做一層。偵測 `TRIPPED`（DI 10005）
**上升緣**時下達命令：

| 來源跳機 | PLC 下達的命令 |
| --- | --- |
| turbine | `generator` 脈衝 `BREAKER_OPEN`、`steam_valve` 脈衝 `STOP` |
| boiler | `steam_valve` 脈衝 `STOP` |
| condenser | `generator` 脈衝 `BREAKER_OPEN` |
| feedwater_pump | `boiler` 脈衝 `STOP` |
| condensate_pump | 無（給水泵自己會保護給水槽） |
| steam_valve | `boiler` 脈衝 `STOP` |

另外，`turbine.SPEED_RPM > 3150`（比設備跳機門檻 3300 早）或汽輪機跳機時，
獨立對主蒸汽閥下 `STOP`——這是不依賴設備自己調速器的第二層超速保護。

只在邊緣觸發一次，不要每個 scan 重送（會蓋掉操作員的手動處置）。被 `STOP` 停下的
設備會維持停機，直到操作員按 `START`（這也是重新允許自持運轉的動作）。

---

## 8. 通訊失效：每台設備的行為不同

PLC 掛掉或網路斷了，機組**照常運轉**：八台設備的預設策略都是 `LOCAL_AUTO`，
本地調節器繼續維持程序量，只把品質降級並點亮 `CONTROL_WATCHDOG_LOST` 警報。

| 策略 | 行為 | 誰在用 |
| --- | --- | --- |
| `LOCAL_AUTO` | 本地控制繼續，只降級品質與警報 | 八台設備預設 |
| `HOLD_LAST` | 保持最後命令 | 需要傳統遠端控制語意時 |
| `FAIL_LOW` / `FAIL_CLOSE` / `FAIL_OPEN` | 輸出歸零／關閥／開閥 | 要示範失效安全時 |
| `TRIP` | 直接跳機 | 嚴格安全示範 |

要示範「PLC 失聯就失效安全」，改該設備 YAML 的 `comm.failure_policy` 即可。

PLC 重新連上後不需要做任何 bumpless 處理——控制從頭到尾都在設備內，
沒有脫節問題。只要重新開始 kick watchdog 就好。

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

北向 map 可以原樣轉發設備的唯讀資料（本專案的 OpenPLC 專案就是這樣做的，
見 `northbound-map.csv`），但**可寫入的部分只暴露命令**：SCADA 的工作是啟動、
停止、確認警報與觀察，設定值與輸出屬於設備自己。`plc_forwards` 欄位標明了
哪些點會真的被 PLC 寫到設備。

---

## 10. 參與快照協議（選配）

自持設備把調節器狀態一起放進快照，因此「秒級重置環境」對 PLC 的影響小很多：
PLC 沒有積分項要對齊，只有**邊緣記憶**（跳機上升緣、脈衝計時）需要重置。

最低限度做法：輪詢 `SNAPSHOT_GENERATION`（30039），發現變了就清掉跳機邊緣旗標與
脈衝狀態。`examples/external_plc.py` 的 `snapshot_changed()` 就是這樣做的。

想更完整（例如 PLC 內有自己的統計或順序狀態），可以用 `Role.CONTROLLER` 連上
plant-bus（`common/simbus/client.py`），處理四種訊息：

| 訊息 | PLC 要做什麼 |
| --- | --- |
| `TICK` | 更新模擬時間（事件時戳用它，不要用真實時間） |
| `PAUSE` / `RESUME` | 暫停／恢復邏輯運算 |
| `SNAPSHOT_SAVE` | 回傳 `SNAPSHOT_DATA`：自己的邊緣記憶與內部狀態 |
| `SNAPSHOT_RESTORE` | 套用後回 `RESTORE_ACK{ok, error}` |

---

## 11. 冷啟動順序：PLC 不需要實作

這是與舊版最大的差異。**沒有順序器**——冷啟動順序是設備的允許條件互相扣住後
自然浮現的，PLC 只要不擋路即可：

```
熱井有水 → 冷凝器抽真空 → 凝結水泵補給水槽 → 給水泵補鍋爐
→ 鍋爐吹掃點火升壓 → 壓力達 30 bar 主蒸汽閥開啟 → 汽輪機升速
→ 轉速達 90% 發電機勵磁 → 同步條件成立自動併聯 → 負載爬到目標
```

完整時間軸與各步驟的允許條件見 `docs/sequence-of-operation.md`。
PLC 想觀察進度就讀 30027 `SELF_HOLD_STATE` 與 30028 `PERMISSIVE_WORD`：
哪一個位元是 0，就是還在等哪一個條件。

若真的需要由 PLC 主導順序（例如作業程序要求），把 `.env` 的 `SELF_HOLD=false`
關掉自持，設備就退回純外部命令模式（保護與互鎖仍在）。

---

## 12. 最小可執行範例

`examples/external_plc.py` 是一支不依賴任何設備類別、
純粹靠 `docs/register-map.csv` 當契約的外部 PLC 骨架，示範本文所有機制：

```bash
# 環境先起來（不啟動 OpenPLC，把 control_net 留給你的 PLC）
COMPOSE_PROFILES=no-plc docker compose up -d

# 從主機執行（自動用 15021~15028 埠）
python examples/external_plc.py --host 127.0.0.1

# 只監看不寫入（不搶控制權）
python examples/external_plc.py --host 127.0.0.1 --read-only

# 只接一台設備做實驗
python examples/external_plc.py --host 127.0.0.1 --only boiler
```

它做了：CSV 驅動的位址解析、批次輪詢、watchdog 續租、例外碼分類處理、
跳機矩陣邊緣觸發（只下命令）、超速獨立停機、`SNAPSHOT_GENERATION` 變化時重置邊緣記憶，
以及在報表列出每台設備的自持狀態。拿它當骨架改成自己的邏輯即可。

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
- [ ] 超速 3150 RPM 的停機命令獨立於設備自己的調速器
- [ ] 跳機矩陣只在邊緣觸發一次，而且只下命令（不寫設定值／手動輸出）
- [ ] 週期性寫入只有命令線圈與 Holding 1~3
- [ ] `SNAPSHOT_GENERATION` 變化時重置邊緣記憶與脈衝狀態
- [ ] 通訊恢復後直接繼續 kick watchdog（沒有 bumpless 問題）

相關文件：`docs/architecture.md`、`docs/sequence-of-operation.md`、`docs/register-map.csv`、
`docs/alarm-codes.csv`、`TESTING.md`。

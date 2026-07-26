# 簡化火力發電廠工業控制模擬器

容器化的火力發電廠模擬環境：8 台獨立設備容器、各自的 Modbus TCP Server、
獨立狀態機與物理模型、內建 DCS／PLC、模擬同步匯流排、事件與歷史資料記錄器，
以及 **不需重啟 docker 的快速狀態恢復（snapshot）功能**。

```bash
cp .env.example .env
docker compose --profile standalone up --build
```

啟動後：

| 服務 | 位址 |
| --- | --- |
| HMI（含快照按鈕） | http://127.0.0.1:15082 |
| plant-bus 管理 API | http://127.0.0.1:15080/state |
| Historian | http://127.0.0.1:15081/events |
| Modbus TCP（鍋爐） | 127.0.0.1:15025 |

---

## 一、快速恢復狀態（本專案的加值功能）

測試工業控制邏輯最花時間的不是測試本身，而是「把機組再開一次」。
本專案在 plant-bus 內建**集中式快照協調器**：在 tick 邊界暫停模擬 →
向所有設備與 DCS 廣播快照請求 → 原子性套用 → 恢復執行。
整個過程是毫秒級，**容器不重啟、Modbus 連線不中斷、行程不重建**。

```bash
alias plantctl='python -m tools.plantctl'      # 或 pip install -e .

plantctl snapshot save steady-60mw -d "60 MW 穩態基準"
plantctl snapshot list
plantctl snapshot restore steady-60mw          # 忠實還原（含跳機鎖存與第一故障）
plantctl snapshot restore steady-60mw --clean  # 還原後清除鎖存，作為乾淨測試起點
plantctl rollback                              # 還原最後一次快照
```

快照內容涵蓋：

* 各設備的物理狀態（水量、壓力、轉速、閥位、慣性、溫度…）
* 設備狀態機、跳機鎖存、第一故障原因與前後 10 秒趨勢
* 警報鎖存與確認狀態、累積量與計數器
* Holding／Coil 暫存器內容、故障注入設定、通訊品質
* DCS 的 PID 積分項、bumpless 狀態、啟動順序步驟、跳機矩陣
* plant-bus 的 tick、模擬時間與程序量影像

還原選項：

| 選項 | 行為 |
| --- | --- |
| 預設 | 忠實還原，包含跳機鎖存與第一故障原因 |
| `--clean` | 還原後清除鎖存與警報，得到未跳機的乾淨測試起點 |
| `--keep-faults` | 保留目前注入的故障（快照內的故障設定不覆蓋） |
| `--preserve-totalizers` | 保留目前累積量（運轉時數、跳機次數…） |
| `--stay-paused` | 還原後維持暫停，方便單步除錯 |

每次還原都會把設備的 `30039 SNAPSHOT_GENERATION` 加一，
測試程式可以用它確認「環境確實已重置」。

其他模擬控制：

```bash
plantctl pause | resume | step 10 | speed 5     # 暫停、單步、加速
plantctl status                                  # 全廠狀態
plantctl watch                                   # 即時追蹤重點程序量
plantctl events --event TRIP_LATCHED             # 事件查詢
```

HTTP API（management_net，供 CI 與 harness 使用）：

```
GET  /state /signals /events /metrics /health
POST /sim/pause /sim/resume /sim/step /sim/speed
GET  /snapshot            POST /snapshot/save   POST /snapshot/restore
GET  /snapshot/{name}     DELETE /snapshot/{name}
POST /fault /fault/clear /signal/force
```

啟動時自動還原（例如 CI 每次都從同一基準開始）：

```bash
RESTORE_ON_BOOT=steady-60mw docker compose --profile standalone up
```

---

## 二、系統架構

```
           外部 PLC / HMI / 測試工具
                        │  Modbus TCP
        ┌───────────────┴───────────────┐  control_net
   ┌────┴────┐ ┌────────┐ ┌───────┐ ┌───┴────┐ ┌────────┐
   │ 冷凝器  │ │凝結水泵│ │給水槽 │ │給水泵  │ │ 鍋爐   │ …
   └────┬────┘ └────┬───┘ └───┬───┘ └───┬────┘ └───┬────┘
        └───────────┴─────────┴─────────┴──────────┘  sim_net（僅物理量）
                        │
                ┌───────┴────────┐
                │   plant-bus    │ 模擬時間、程序量路由、品質、快照
                └───────┬────────┘
                        │ management_net
              historian / HMI / metrics
```

* **control_net**：Modbus TCP、外部 PLC/DCS、HMI、測試工具、封包擷取。
* **sim_net**（`internal: true`）：只交換物理量，外部 PLC 無法繞過 Modbus 介面直接改狀態。
* **management_net**：metrics、日誌、歷史資料、健康檢查。

| 設備 | Compose service | 容器埠 | 主機埠 |
| --- | --- | ---: | ---: |
| DCS／PLC | `dcs-plc` | 502 | 15020 |
| 冷凝器 | `condenser` | 502 | 15021 |
| 凝結水泵 | `condensate-pump` | 502 | 15022 |
| 給水槽 | `feedwater-tank` | 502 | 15023 |
| 給水泵 | `feedwater-pump` | 502 | 15024 |
| 鍋爐 | `boiler` | 502 | 15025 |
| 主蒸汽閥 | `steam-valve` | 502 | 15026 |
| 汽輪機 | `turbine` | 502 | 15027 |
| 發電機 | `generator` | 502 | 15028 |

主機端預設只綁 `127.0.0.1`（`.env` 的 `BIND_ADDR`）；
需要從實驗室 LAN 連入時改成 `BIND_ADDR=0.0.0.0`。

詳細說明見 [docs/architecture.md](docs/architecture.md)。
外部 PLC／DCS 要如何串接這 8 台設備，見 [docs/plc-integration.md](docs/plc-integration.md)
（含可執行骨架 `examples/external_plc.py`）。
可直接匯入的 OpenPLC Editor 專案與 ScadaBR HMI，見
[docs/openplc-scadabr-integration.md](docs/openplc-scadabr-integration.md)。

---

## 三、Compose profiles

```bash
docker compose --profile standalone up --build       # 設備 + plant-bus + 內建 DCS + HMI + historian
docker compose --profile external-plc up --build     # 設備 + plant-bus + HMI + historian（無內建 DCS）
docker compose -f compose.yaml -f compose.secure.yaml --profile secure up --build
```

設備服務不綁 profile，因此任何模式都會啟動；只有控制器、HMI 與測試工具受 profile 控制。
`secure` profile 以 stunnel sidecar 在 802 埠提供 Modbus Security（TLS + X.509），
普通 Modbus TCP 仍保留，方便相容性測試與外部協定測試。

---

## 四、Modbus 介面

* 支援功能碼 **01 / 02 / 03 / 04 / 05 / 06 / 15 / 16 / 22 / 23 / 43-14**。
* 未啟用的功能碼回傳 **Exception 01** 且**不關閉連線**。
* 地址不存在或寫入唯讀區 → **Exception 02**；數值超出工程範圍 → **Exception 03**；
  設備切換狀態或資料鎖定 → **Exception 06**；內部錯誤 → **Exception 04**。
* 主要程序量採縮放整數，32 位元值高 word 在前；如需 Float32 則
  Big Endian byte order + High Word First。
* 暫存器映像為不可變快照整份替換，因此**不會出現撕裂讀取**。
* 寫入一律先進 command queue，下一個 scan cycle 才由狀態機與安全邏輯決定是否套用。

完整地址表（含「文件地址」與「PDU offset」兩欄）：
[docs/register-map.csv](docs/register-map.csv)，可用 `python -m tools.export_docs` 重新產生。

範例：

```bash
# 讀鍋爐壓力（30010 -> PDU offset 9，bar(a) ×100）
plantctl read --device boiler --register BOILER_PRESSURE

# 設定發電機負載 90 MW（40010）
plantctl write --device generator --register PRIMARY_SETPOINT --value 90

# 跳機重置：需要 Reset Key 0xA55A + 新的命令序號 + 安全條件成立
plantctl write --device boiler --register RESET_KEY --value 42330
plantctl write --device boiler --register COMMAND_SEQUENCE --value 12
plantctl write --device boiler --register RESET_TRIP --value 1 --coil
```

---

## 五、保護與第一故障原因

門檻全部來自 `configs/*.yaml`，程式中不硬編碼：

| 保護 | 警報 | 跳機 | 延遲 |
| --- | ---: | ---: | ---: |
| 鍋爐低水位 | <30% | <20% | 2 s |
| 鍋爐高水位 | >85% | >90% | 2 s |
| 鍋爐高壓 | >108 bar | >115 bar | 1 s |
| 汽輪機超速 | >3150 RPM | >3300 RPM | 0.2 s |
| 冷凝器壓力高 | >0.15 bar(a) | >0.25 bar(a)（汽輪機跳機） | 2 s |
| 熱井低水位 | <20% | <10%（泵跳機） | 2 s |
| 給水槽低水位 | <25% | <15%（泵跳機） | 3 s |

每個跳機都有 `active / latched / first_out / resettable` 四個屬性；
條件消失只清除 `active`，`latched` 必須由重置命令解除，且重置需同時滿足
Reset Key、Reset Coil 脈衝、安全條件、緊急停止已解除、命令序號為新值。
第一故障原因會記錄設備、代碼、模擬時間、真實時間、程序值、門檻、當時控制輸出
與前後 10 秒主要變數，且**不會被後續連鎖跳機覆蓋**。

代碼表：[docs/alarm-codes.csv](docs/alarm-codes.csv)

---

## 六、故障注入

故障注入只在 `LAB_MODE=true` 開放，且**協定層故障與物理層故障分開**：

```bash
# 程序故障：冷卻水能力降到 30%
plantctl fault set --target condenser --category process \
        --name cooling_water_availability --value 0.3

# 執行器故障：主蒸汽閥卡開
plantctl fault set --target steam_valve --category actuator \
        --name valve_mode --spec STUCK_OPEN

# 感測器故障：鍋爐水位 +10% 偏差
plantctl fault set --target boiler --category sensor --name level \
        --mode bias --bias 10

# 通訊故障：回應延遲 300 ms、5% 丟棄 response
plantctl fault set --target turbine --category comm --name modbus \
        --spec '{"response_delay_ms": 300, "drop_response_prob": 0.05}'

plantctl fault clear --target '*'
```

外部測試工具若要從同一個起點重複測試，可先存一個基準快照，
之後每輪用 `POST /snapshot/restore`（`clear_latches: true`）毫秒級還原，
再用 `tools/invariants.py` 檢查物理安全不變量（詳見 [docs/snapshot.md](docs/snapshot.md)）。

封包錄製與回放（重現問題序列）：

```bash
python -m tools.modbus_recorder record --listen 0.0.0.0:1502 --target boiler:502 --out cap.jsonl
python -m tools.modbus_recorder replay --target boiler:502 --file cap.jsonl
```

---

## 七、測試

```bash
pip install -e ".[dev]"
pytest -q                    # 180 個測試，約 40 秒（不需 docker）
pytest tests/modbus -q       # Modbus 規格驗收
pytest tests/physics -q      # 物理模型方向性與守恆
pytest tests/integration -q  # 整廠閉迴路、快照往返、跳機鎖存、持久化
```

`tests/harness.py` 提供行程內的迷你機組（lockstep 驅動 8 台設備），
因此物理與快照邏輯可以在 CI 內用秒級跑完，不必啟動容器。

情境測試（需要執行中的環境）：

```bash
plantctl scenario run scenarios/normal_startup.yaml
plantctl scenario run scenarios/load_step.yaml
plantctl scenario run scenarios/cooling_loss.yaml
plantctl scenario run scenarios/snapshot_roundtrip.yaml
```

完整測試步驟與逐條指令說明見 [TESTING.md](TESTING.md)。

---

## 八、目錄結構

```
thermal-plant-simulator/
├── compose.yaml / compose.secure.yaml / Dockerfile
├── common/           modbus（自製 server、register map、編碼）、device（框架、保護、警報、持久化、故障）、simbus
├── plant_bus/        lockstep 時間同步、程序量路由與品質、快照協調、HTTP API
├── devices/          8 台設備的物理模型與暫存器映射
├── controller/       PID、三元素水位、啟動順序、跳機矩陣、DCS 主程式
├── historian/ hmi/   事件與歷史資料、簡易 HMI
├── integrations/     OpenPLC Editor 專案與 ScadaBR 匯入檔
├── tools/            plantctl、情境執行器、不變量檢查、封包錄製、文件產生
├── configs/          全廠與各設備設定（所有門檻）
├── scenarios/        8 個驗收情境
├── docs/             架構、物理模型、暫存器表、代碼表、操作順序
└── tests/            unit / physics / modbus / integration / scenarios / regression
```

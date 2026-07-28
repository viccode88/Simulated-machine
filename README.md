# 簡化火力發電廠工業控制模擬器

容器化的火力發電廠模擬環境：8 台**自持**設備容器、各自的 Modbus TCP Server、
獨立狀態機、物理模型與本地調節器，加上 OpenPLC（v3／v4）作為資料交換與邏輯判斷的
PLC、模擬同步匯流排、事件與歷史資料記錄器，以及
**不需重啟 docker 的快速狀態恢復（snapshot）功能**。

```bash
cp .env.example .env
docker compose up --build
```

**開起來就會自己發電。** 八台設備是自持的：環境數值一達到可運行條件就自行啟動，
並由設備自己的調節器把程序量維持在可運行範圍。冷凝器抽真空 → 凝結水泵補給水槽 →
給水泵補鍋爐 → 鍋爐吹掃點火升壓 → 主蒸汽閥進汽升速 → 發電機自動同步併聯 →
緩慢加載到 60 MW，全程約 15 分鐘（模擬時間），**不需要任何控制器介入**。
`plantctl watch` 可以看它自己爬上去。

三個角色分工是這個專案的核心設計：

| 角色 | 負責 | 不負責 |
| --- | --- | --- |
| **設備**（8 台容器） | 自行啟停、本地閉迴路調節、保護與跳機、互鎖 | — |
| **PLC**（OpenPLC v3／v4） | 南北向資料交換、watchdog、脈衝展寬、跳機矩陣、互鎖判斷 | 任何控制運算（無 PID、無設定值） |
| **SCADA／HMI** | 啟動、停止、確認警報、觀察 | 設定值與輸出（設備自己決定） |

想「開機瞬間就在滿載」：第一次跑完整啟動，達到負載後執行

```bash
python -m tools.plantctl baseline        # 存成 steady-60mw 快照
```

再把 `.env` 改成 `RESTORE_ON_BOOT=steady-60mw`，之後每次 `docker compose up`
都會在數十毫秒內直接回到 60 MW 滿載（不必再等自持啟動）。

啟動後：

| 服務 | 位址 |
| --- | --- |
| HMI（含快照按鈕） | http://127.0.0.1:15082 |
| plant-bus 管理 API | http://127.0.0.1:15080/state |
| Historian | http://127.0.0.1:15081/events |
| PLC 北向 Modbus（SCADA 連這裡） | 127.0.0.1:15020 |
| OpenPLC v4 Editor API／v3 網頁介面 | 15443／15083 |
| Modbus TCP（鍋爐，設備直連） | 127.0.0.1:15025 |

---

## 一、快速恢復狀態（本專案的加值功能）

測試工業控制邏輯最花時間的不是測試本身，而是「把機組再開一次」。
本專案在 plant-bus 內建**集中式快照協調器**：在 tick 邊界暫停模擬 →
向所有設備廣播快照請求 → 原子性套用 → 恢復執行。
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
* 各設備本地調節器的積分項與追隨狀態、自持狀態與操作員停機鎖
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
RESTORE_ON_BOOT=steady-60mw docker compose up
```

---

## 二、系統架構

```
                    SCADA / HMI（只有啟動、停止、觀察）
                                │  Modbus TCP 502
                    ┌───────────┴────────────┐
                    │  OpenPLC v3 / v4       │ 資料交換 + 邏輯判斷
                    │  （不做任何控制運算）   │ watchdog / 脈衝 / 跳機矩陣
                    └───────────┬────────────┘
                                │  Modbus TCP  control_net
   ┌────┴────┐ ┌────────┐ ┌───────┐ ┌───┴────┐ ┌────────┐
   │ 冷凝器  │ │凝結水泵│ │給水槽 │ │給水泵  │ │ 鍋爐   │ …  ← 每台自持
   └────┬────┘ └────┬───┘ └───┬───┘ └───┬────┘ └───┬────┘     （本地調節器）
        └───────────┴─────────┴─────────┴──────────┘  sim_net（僅物理量）
                        │
                ┌───────┴────────┐
                │   plant-bus    │ 模擬時間、程序量路由、品質、快照
                └───────┬────────┘
                        │ management_net
              historian / HMI / metrics
```

* **control_net**：Modbus TCP、OpenPLC、HMI、測試工具、封包擷取。
* **sim_net**（`internal: true`）：只交換物理量，外部 PLC 無法繞過 Modbus 介面直接改狀態。
* **management_net**：metrics、日誌、歷史資料、健康檢查。

| 設備 | Compose service | 容器埠 | 主機埠 |
| --- | --- | ---: | ---: |
| PLC（OpenPLC 北向） | `openplc-v4` / `openplc-v3` | 502 | 15020 |
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

操作方法、錯誤判讀與排除，見 **[docs/operations-manual.md](docs/operations-manual.md)**。
自持控制的設計與各設備的調節迴路，見 [docs/self-holding.md](docs/self-holding.md)。
詳細架構見 [docs/architecture.md](docs/architecture.md)、
自持啟動順序見 [docs/sequence-of-operation.md](docs/sequence-of-operation.md)。
外部 PLC／SCADA 要如何串接，見 [docs/plc-integration.md](docs/plc-integration.md)
（含可執行骨架 `examples/external_plc.py`）。
可直接匯入的 OpenPLC 專案與 ScadaBR HMI，見
[docs/openplc-scadabr-integration.md](docs/openplc-scadabr-integration.md)。

---

## 三、Compose profiles（選 PLC）

八台設備、plant-bus、HMI 與 historian 一律啟動；profile 只決定要哪一種 PLC，
由 `.env` 的 `COMPOSE_PROFILES` 指定，預設 `openplc-v4`：

```bash
docker compose up --build                            # OpenPLC v4（預設）
COMPOSE_PROFILES=openplc-v3 docker compose up        # OpenPLC v3
COMPOSE_PROFILES=no-plc docker compose up            # 不啟動 PLC，設備照樣自持運轉
COMPOSE_PROFILES=openplc-v4 docker compose -f compose.yaml -f compose.secure.yaml up --build
```

| profile | PLC | 部署方式 |
| --- | --- | --- |
| `openplc-v4` | `ghcr.io/autonomy-logic/openplc-runtime` | OpenPLC Editor v4 由 15443（8443）部署專案 |
| `openplc-v3` | 由 `integrations/openplc/docker/Dockerfile.v3` 自原始碼建置 | 15083 網頁介面上傳 `.st` 並設定 Slave Devices |
| `no-plc` | 無 | 驗證「設備真的自持」用 |

> 命令列的 `--profile` 是**疊加**在 `COMPOSE_PROFILES` 之上，不是取代。
> v3 與 v4 會搶同一個主機埠（`PLC_MODBUS_PORT`，預設 15020），不要同時啟動。

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
* 自持設備在 AUTO 模式（40001 `CONTROL_MODE` 預設 1 = LOCAL_AUTO）下**忽略
  40012 `MANUAL_OUTPUT`**：執行器由本地調節器決定。要手動接管請先寫
  `CONTROL_MODE = 0`（LOCAL_MANUAL）。
* 自持狀態一律可觀測：30026 `LOCAL_OUTPUT`（本地調節器輸出）、
  30027 `SELF_HOLD_STATE`（0 停用 / 1 待機 / 2 自持運轉 / 3 操作員停機 /
  4 跳機鎖定 / 5 維修）、30028 `PERMISSIVE_WORD`（每個允許條件一個位元）。

完整地址表（含「文件地址」與「PDU offset」兩欄）：
[docs/register-map.csv](docs/register-map.csv)，可用 `python -m tools.export_docs` 重新產生。

範例：

```bash
# 讀鍋爐壓力（30010 -> PDU offset 9，bar(a) ×100）
plantctl read --device boiler --register BOILER_PRESSURE

# 啟停（SCADA 正常只需要這兩個命令；START 同時解除操作員停機鎖）
plantctl write --device boiler --register START --value 1 --coil
plantctl write --device boiler --register STOP --value 1 --coil

# 調整目標負載（設定值仍可寫，但屬於工程操作，不是日常 SCADA 動作）
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
pytest -q                    # 175 個測試，約 60 秒（不需 docker）
pytest tests/modbus -q       # Modbus 規格驗收
pytest tests/physics -q      # 物理模型方向性與守恆（設備切 LOCAL_MANUAL）
pytest tests/integration/test_self_hold.py -q   # 自持：無控制器冷啟動到 60 MW
pytest tests/integration -q  # 整廠、快照往返、跳機鎖存、持久化、OpenPLC 產物
```

`tests/harness.py` 提供行程內的迷你機組（lockstep 驅動 8 台設備）。因為設備自持，
harness 內**沒有任何控制器**：`plant.step()` 就會讓機組自己爬到 60 MW，
物理與快照邏輯可以在 CI 內用秒級跑完，不必啟動容器。

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
├── common/           modbus（自製 server、register map、編碼）、device（自持框架、調節器、保護、警報、持久化、故障）、simbus
├── plant_bus/        lockstep 時間同步、程序量路由與品質、快照協調、HTTP API
├── devices/          8 台設備的物理模型、本地自持控制與暫存器映射
├── historian/ hmi/   事件與歷史資料、簡易 HMI
├── integrations/     OpenPLC v4／v3 專案、v3 容器 Dockerfile、ScadaBR 匯入檔
├── tools/            plantctl、情境執行器、不變量檢查、封包錄製、文件產生
├── configs/          全廠與各設備設定（所有門檻）
├── scenarios/        8 個驗收情境
├── docs/             操作手冊、架構、物理模型、暫存器表、代碼表、操作順序
└── tests/            unit / physics / modbus / integration / scenarios / regression
```

# CLAUDE.md

給 Claude Code 在此 repo 工作時的指引。

## 專案是什麼

容器化的火力發電廠工業控制模擬器（`thermal-plant-simulator`，Python 3.11+）。
8 台**自持設備**各自跑在獨立容器裡：獨立狀態機、獨立物理模型、獨立本地調節器、
獨立 Modbus TCP Server。plant-bus 做 lockstep 時間同步與程序量路由，
OpenPLC（v3／v4）只做資料交換與邏輯判斷，SCADA／HMI 只能啟停與觀察。

三個角色的界線是這個專案的核心設計，改動時務必維持：

| 角色 | 負責 | **不可以**做 |
| --- | --- | --- |
| 設備 | 啟停判斷、閉迴路調節、保護跳機、互鎖 | — |
| PLC | 資料交換、watchdog、脈衝展寬、跳機矩陣 | 任何控制運算（無 PID、無設定值） |
| SCADA／HMI | START／STOP／ACK／RESET、觀察 | 決定設定值與輸出 |

## 常用指令

```bash
# 環境
pip install -e ".[dev]"
cp .env.example .env

# 測試（不需 docker，約 60 秒）
pytest -q
pytest tests/integration/test_self_hold.py -q   # 無控制器冷啟動到 60 MW
pytest tests/modbus -q                          # Modbus 規格驗收
pytest tests/physics -q                         # 物理方向性與守恆

# 執行
docker compose up --build                       # 預設 openplc-v4 profile
COMPOSE_PROFILES=no-plc docker compose up       # 驗證設備真的自持
COMPOSE_PROFILES=openplc-v3 docker compose up

# 操作（alias plantctl='python -m tools.plantctl'）
plantctl status | watch | events
plantctl pause | resume | step 10 | speed 5
plantctl snapshot save baseline
plantctl snapshot restore baseline --clean
plantctl baseline                               # 存 steady-60mw，供 RESTORE_ON_BOOT

# 產生文件與 PLC/SCADA 匯入檔
python -m tools.export_docs                     # 重新產生 docs/register-map.csv 等
```

## 目錄與職責

```
common/modbus/      自製 asyncio Modbus server、register map、編碼（pymodbus 只當 client）
common/device/      BaseDevice 自持框架、regulator、protection、alarm、persistence、faults
common/simbus/      設備 ↔ plant-bus 的 JSON-lines 協定與 client
plant_bus/app/      lockstep tick loop、程序量路由與品質、快照協調、HTTP API
devices/<name>/     8 台設備：物理模型 + 本地自持控制 + 暫存器映射
hmi/ historian/     簡易 HMI 網頁、事件與歷史資料
tools/              plantctl、scenario_runner、invariants、modbus_recorder、export_docs
configs/            plant.yaml + 各設備 yaml —— **所有門檻與增益都在這裡**
tests/harness.py    行程內迷你機組（MiniPlant），CI 不必起容器
```

## 硬性約定

1. **不要在程式碼裡硬編碼門檻、增益、延遲。** 一律從 `configs/*.yaml` 讀，
   透過 `cfg_get()` 取值。新增參數要同時更新對應 yaml 與 `docs/`。
2. **物理公式只能寫在 `devices/`。** plant-bus 不含任何物理，只做時間、路由與品質。
3. **Modbus handler 不直接改物理狀態。** 寫入先進 command queue，
   下一個 scan cycle 由 `_apply_commands()` 交給狀態機與安全邏輯決定是否套用。
4. **所有時間相關邏輯用模擬時間計時**（不是 wall clock），否則改速度或還原快照後行為會不一致。
5. **AUTO 模式下設備不回寫自己的 Holding Register。** 那些暫存器屬於操作端；
   本地輸出一律經 `30026 LOCAL_OUTPUT` 與程序量暫存器回報。
6. **改暫存器定義後要跑 `python -m tools.export_docs`**，讓 `docs/register-map.csv`
   與 OpenPLC／ScadaBR 匯入檔同步（`tests/integration/test_openplc_scadabr_artifacts.py` 會驗）。
7. 保護動作用 `sm.force()`（無視轉換表），一般狀態轉換用 `sm.to()`。

## 新增／修改設備的路徑

`devices/<name>/main.py` 繼承 `BaseDevice`，實作這幾個 hook：

| Hook | 意義 |
| --- | --- |
| `configure()` | 從 yaml 讀參數、建立 regulator |
| `start_permissives()` | **可不可以跑** — 安全與環境條件；同時決定 READY / INTERLOCKS_OK / `30028 PERMISSIVE_WORD` |
| `self_start_ready()` / `self_stop_request()` | **現在需不需要跑** — 本地需求（例如下游水位夠了就待機） |
| `step(dt)` | 物理模型 |
| `publish()` / `fill_registers()` | 對外的程序量與暫存器映射 |
| `protection_values()` | 餵給保護邏輯的量測值 |
| `snapshot_extra()` / `restore_extra()` | 快照要保留的額外狀態 |

冷啟動順序**沒有順序器**：靠允許條件互相扣住自然浮現
（熱井有水 → 冷凝器 → 凝結水泵 → 給水泵 → 鍋爐吹掃點火 → 主蒸汽閥 → 併聯加載）。
改允許條件等於改啟動順序，改完務必跑 `tests/integration/test_self_hold.py`。

## 除錯要點

* **「機組看起來沒在動」通常不是故障。** 完整冷啟動約 **930 模擬秒（約 15 分鐘即時）**：
  0–2 s 設備連上匯流排、2 s 自行 START、30 s 鍋爐吹掃、約 230 s 併聯、約 930 s 到 60 MW。
  用 `plantctl watch` 看 `sim_time` 有沒有前進；要快轉用 `plantctl speed 10`，
  要直接滿載用 `RESTORE_ON_BOOT=steady-60mw`。
* HMI 的「設備狀態」只列 `expected_devices`（8 台）；`historian` 之類的
  observer 參與者列在另一區，不代表設備狀態。
* plant-bus 每 tick 等設備回 `TICK_DONE`，超過 `simulation.tick_timeout`（0.35 s）
  就記 `DEVICE_TICK_TIMEOUT` 並繼續 —— 逾時只會讓即時倍率下降，不會停住模擬。
* 跳機是**鎖存**的：條件消失只清 `active`，`latched` 要 RESET_KEY（0xA55A）+
  新的 COMMAND_SEQUENCE + RESET_TRIP coil 才解除。第一故障原因不會被連鎖跳機覆蓋。
* 故障注入只在 `LAB_MODE=true` 開放。

## 測試慣例

* `tests/harness.py` 的 `MiniPlant` 用 lockstep 在行程內驅動 8 台設備，
  **不含任何控制器** —— `plant.step()` 就會讓機組自己爬到 60 MW。
  寫整合測試優先用它，不要起 docker。
* `tests/physics/` 測物理時把設備切 `LOCAL_MANUAL`，才不會被本地調節器干擾。
* 回歸測試放 `tests/regression/test_reported_defects.py`。
* 新增 pytest 檔案時遵守 `pyproject.toml` 的 `asyncio_mode = "auto"`。

## 文件

`docs/self-holding.md`（自持設計與各設備迴路）、`docs/architecture.md`、
`docs/sequence-of-operation.md`、`docs/operations-manual.md`（操作與排錯）、
`docs/register-map.csv`、`docs/alarm-codes.csv`、`TESTING.md`。
文件是中文的，新增說明請保持中文。

# 測試操作手冊

從零到完整驗收的每一道指令。分兩條路線：

* **路線 A（離線）**：只跑 `pytest`，不需要 Docker，約 20 秒，驗證物理模型、Modbus 規格、保護邏輯、快照往返。
* **路線 B（容器）**：啟動 8 台設備容器，做整廠情境、故障注入、TLS。

---

## 0. 前置準備

| 需求 | 版本 | 檢查指令 |
| --- | --- | --- |
| Python | ≥ 3.11 | `python3 -V` |
| Docker Desktop | Compose v2 | `docker compose version` |

```bash
cd ~/Documents/ais3_end/thermal-plant-simulator

# 建立虛擬環境（建議，避免污染系統 Python）
python3 -m venv .venv
source .venv/bin/activate

# 安裝套件 + 開發相依 + plantctl 指令
pip install -e ".[dev]"

# 環境變數檔
cp .env.example .env
```

`pip install -e ".[dev]"` 會做三件事：安裝 `pymodbus / PyYAML / aiohttp`、安裝 `pytest / pytest-asyncio`、
把 `plantctl` 註冊成可執行指令（等同 `python -m tools.plantctl`）。

> 不想安裝套件也可以用 `alias plantctl='python -m tools.plantctl'`，但 `pytest` 仍需要相依套件。

編輯 `.env`，**要測故障注入與情境測試就必須打開 LAB_MODE**：

```bash
LAB_MODE=true        # 開放故障注入區域與 TRIP_TEST（8 個情境有 5 個需要）
BIND_ADDR=127.0.0.1  # 只綁本機；要從 LAN 連入才改 0.0.0.0
SELF_HOLD=true       # 設備自持：允許條件成立就自行啟動（預設）
MODBUS_TRACE=false   # 需要逐筆封包日誌時改 true
```

---

## 路線 A：離線測試（不需 Docker）

`tests/harness.py` 提供行程內的迷你機組，用 lockstep 驅動 8 台設備。
設備是自持的，所以 harness 裡**沒有任何控制器**：`plant.step()` 就會讓機組
自己冷啟動到 60 MW。物理、保護、自持、快照邏輯都能在秒級跑完。

```bash
pytest -q
```

**預期輸出**：`175 passed in ~70s`

### 分層執行與各層驗證內容

| 指令 | 測試數 | 驗證什麼 |
| --- | ---: | --- |
| `pytest tests/unit -q` | 34 | 保護門檻與延遲、暫存器映射建構、u32/i16/縮放編碼、警報鎖存與確認狀態機 |
| `pytest tests/physics -q` | 21 | 各設備物理模型的方向性與守恆（設備切 LOCAL_MANUAL 以隔離物理） |
| `pytest tests/modbus -q` | 22 | Modbus 規格驗收：支援的功能碼、Exception 01/02/03/04/06 的觸發條件、例外後連線不關閉、無撕裂讀取 |
| `pytest tests/integration -q` | 38 | **自持冷啟動（無控制器到 60 MW）**、整廠行為、快照往返、跳機鎖存、持久化、OpenPLC/ScadaBR 產物契約 |
| `pytest tests/scenarios -q` | 17 | `scenarios/*.yaml` 8 個情境檔的結構、訊號名稱、門檻合法性（靜態檢查，不需執行中環境） |
| `pytest tests/regression -q` | 43 | 已修缺陷的回歸測試：控制時間基準、PLC 只下命令不寫控制值、回報過的錯誤行為不再重現 |

其中最能代表這個專案的是：

```bash
pytest tests/integration/test_self_hold.py -q   # 完全沒有控制器，機組自己開到 60 MW
```

### 常用旗標

```bash
pytest -v                              # 列出每個測試名稱
pytest -x                              # 第一個失敗就停
pytest tests/unit/test_protection.py -v   # 只跑單一檔案
pytest -k "snapshot" -v                # 只跑名稱含 snapshot 的測試
pytest --durations=10                  # 找出最慢的 10 個測試
pytest -q 2>&1 | tail -20              # 只看結尾摘要
```

---

## 路線 B：容器環境測試

### 1. 啟動

```bash
docker compose up --build -d
```

八台設備、plant-bus、HMI 與 historian 一律啟動；profile 只決定 PLC：

| Profile | 內容 |
| --- | --- |
| `openplc-v4`（預設） | 8 台自持設備 + plant-bus + **OpenPLC v4** + HMI + historian |
| `openplc-v3` | 同上，改用 OpenPLC v3 Runtime（自原始碼建置） |
| `no-plc` | 不啟動 PLC——設備自持，照樣會自己併聯發電 |
| `secure` | 加掛 stunnel sidecar，802 埠提供 Modbus Security（TLS） |

### 2. 確認全部健康

```bash
docker compose ps                      # 11 個容器都要 running / healthy
docker compose logs -f plant-bus       # 看模擬時間是否在推進（Ctrl-C 離開）
```

首次啟動要等 image build（數分鐘），之後約 30 秒全部 healthy。

### 3. 連線與健康檢查

```bash
# plant-bus 管理 API
curl -s http://127.0.0.1:15080/health | python3 -m json.tool
curl -s http://127.0.0.1:15080/state  | python3 -m json.tool | head -40
curl -s http://127.0.0.1:15080/metrics | head -20

# Modbus 可回應（讀 30008 Register Map Version，成功回 0）
python -m tools.healthcheck modbus 127.0.0.1 15025 && echo "boiler OK"
python -m tools.healthcheck http http://127.0.0.1:15080/health && echo "bus OK"

# 8 台設備逐一檢查
for p in 15020 15021 15022 15023 15024 15025 15026 15027 15028; do
  python -m tools.healthcheck modbus 127.0.0.1 $p && echo "port $p OK" || echo "port $p FAIL"
done
```

瀏覽器開啟：

| 服務 | 位址 |
| --- | --- |
| HMI（含快照按鈕） | <http://127.0.0.1:15082> |
| plant-bus 狀態 | <http://127.0.0.1:15080/state> |
| Historian 事件 | <http://127.0.0.1:15081/events> |

### 4. 等待冷啟動完成

設備自持，開機後會自動完成冷啟動。加速觀察：

```bash
plantctl speed 5          # 5 倍速
plantctl status           # 全廠狀態快照
plantctl watch            # 即時追蹤重點程序量（Ctrl-C 離開）
plantctl events --limit 30
```

穩態判準（約 3~5 分鐘模擬時間）：鍋爐壓力 ~100 bar(a)、汽輪機 ~3000 RPM、發電機 ~60 MW。

---

## 5. Modbus 介面驗收

### 讀寫

```bash
# 讀鍋爐壓力（文件地址 30010 -> PDU offset 9，bar(a) ×100）
plantctl read --device boiler   --register BOILER_PRESSURE
plantctl read --device boiler   --register LEVEL_ACTUAL
plantctl read --device turbine  --register SPEED_RPM
plantctl read --device generator --register ACTIVE_POWER

# 寫入：發電機負載設定 90 MW（40010 PRIMARY_SETPOINT）
plantctl write --device generator --register PRIMARY_SETPOINT --value 90

# 寫 Coil
plantctl write --device generator --register BREAKER_OPEN --value 1 --coil
```

裝置名稱：`condenser` `condensate_pump` `feedwater_tank` `feedwater_pump`
`boiler` `steam_valve` `turbine` `generator`（皆用底線）。
完整暫存器名稱查 `docs/register-map.csv`（688 筆，含「文件地址」與「PDU offset」兩欄）。

### 例外碼驗收（規格重點）

```bash
python - <<'PY'
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient("127.0.0.1", port=15025, timeout=3); c.connect()

# Exception 02：讀取不存在的位址
print("不存在位址 ->", c.read_input_registers(9999, count=1, device_id=1))

# Exception 02：寫入唯讀區（Input Register 區不可寫）
print("寫唯讀區   ->", c.write_register(9999, 1, device_id=1))

# Exception 03：數值超出工程範圍（PRIMARY_SETPOINT 上限 130 bar）
print("超出範圍   ->", c.write_register(9, 60000, device_id=1))

# 例外後連線必須仍活著
print("連線仍可用 ->", c.read_input_registers(9, count=1, device_id=1).registers)
c.close()
PY
```

預期：前三行回 Exception Response（01/02/03 對應 IllegalFunction / IllegalAddress / IllegalValue），
第四行仍讀得到值 —— **例外不關閉連線**是驗收重點之一。

未啟用的功能碼（例如 FC 08 Diagnostics）應回 **Exception 01**，同樣不斷線。

### 跳機重置三要素

重置必須同時滿足 Reset Key + 新的命令序號 + Reset Coil 脈衝 + 安全條件成立 + 緊急停止已解除：

```bash
plantctl write --device boiler --register RESET_KEY --value 42330      # 0xA55A
plantctl write --device boiler --register COMMAND_SEQUENCE --value 12  # 必須是新值
plantctl write --device boiler --register RESET_TRIP --value 1 --coil
plantctl read  --device boiler --register TRIP_WORD                    # 應歸零
```

只做其中一步應該**無效**，這正是要驗證的行為。

---

## 6. 快照功能驗收（本專案加值功能）

```bash
plantctl snapshot save steady-60mw -d "60 MW 穩態基準"
plantctl snapshot list
plantctl snapshot show steady-60mw

plantctl snapshot restore steady-60mw                    # 忠實還原（含跳機鎖存與第一故障）
plantctl snapshot restore steady-60mw --clean            # 清除鎖存與警報，乾淨測試起點
plantctl snapshot restore steady-60mw --keep-faults      # 保留目前注入的故障
plantctl snapshot restore steady-60mw --preserve-totalizers  # 保留運轉時數/跳機次數
plantctl snapshot restore steady-60mw --stay-paused      # 還原後維持暫停，方便單步除錯

plantctl rollback                                        # 還原最後一次快照
plantctl snapshot delete steady-60mw
```

### 驗證「真的重置了」且「容器沒重啟」

```bash
# 還原前後比較 30039 SNAPSHOT_GENERATION，每還原一次 +1
plantctl read --device boiler --register SNAPSHOT_GENERATION
plantctl snapshot restore steady-60mw
plantctl read --device boiler --register SNAPSHOT_GENERATION   # 應該 +1

# 容器啟動時間不變 = 沒有重啟、Modbus 連線沒中斷
docker inspect -f '{{.State.StartedAt}}' boiler
```

### 還原耗時（毫秒級）

```bash
time plantctl snapshot restore steady-60mw
```

### 模擬控制

```bash
plantctl pause          # 在 tick 邊界暫停
plantctl step 10        # 單步執行 10 個 tick
plantctl resume
plantctl speed 5        # 5 倍速
plantctl speed 1        # 回到即時
```

### 開機自動還原（CI 用）

```bash
docker compose down
RESTORE_ON_BOOT=steady-60mw docker compose up -d
```

---

## 7. 情境測試（8 個驗收情境）

**順序很重要**：`normal_startup` 會在結尾存下 `steady-60mw`，其餘 7 個情境都以它為起點還原。
5 個含故障注入的情境需要 `LAB_MODE=true`。

```bash
# 第一步：從冷態跑完啟動順序並存基準（最久，約 5~10 分鐘）
plantctl scenario run scenarios/normal_startup.yaml

# 其餘情境（每個都會自動還原 steady-60mw --clean，彼此獨立）
plantctl scenario run scenarios/load_step.yaml           # §19.2 負載 60->90 MW
plantctl scenario run scenarios/load_rejection.yaml      # §19.3 甩載 90->0 MW
plantctl scenario run scenarios/cooling_loss.yaml        # §19.4 冷卻能力 100%->30%
plantctl scenario run scenarios/feedwater_pump_trip.yaml # §19.5 給水泵跳機
plantctl scenario run scenarios/valve_stuck_open.yaml    # §19.6 主蒸汽閥卡開
plantctl scenario run scenarios/sensor_bias.yaml         # 感測器偏差 +10%
plantctl scenario run scenarios/snapshot_roundtrip.yaml  # 快照往返驗收
```

一次跑完全部（第一個先跑，後面依序）：

```bash
plantctl scenario run scenarios/normal_startup.yaml || exit 1
for f in scenarios/load_step.yaml scenarios/load_rejection.yaml \
         scenarios/cooling_loss.yaml scenarios/feedwater_pump_trip.yaml \
         scenarios/valve_stuck_open.yaml scenarios/sensor_bias.yaml \
         scenarios/snapshot_roundtrip.yaml; do
  echo "===== $f ====="
  plantctl scenario run "$f" || echo "FAIL: $f"
done
```

每個情境會逐項印出 `[PASS] / [FAIL]`，結尾是 `結果：PASS（xx.xs）`，離開碼 0 = 通過。
最後一步 `check_invariants` 檢查物理安全不變量（質量守恆、壓力上限、轉速上限等）。

各情境驗收重點：

| 情境 | 驗收什麼 |
| --- | --- |
| `normal_startup` | 冷啟動順序完成、冷凝器 0.06~0.10 bar(a)、鍋爐 98~102 bar、水位 63.7~69.7%、轉速 2990~3010 RPM |
| `load_step` | 轉速短暫下降 → 蒸汽閥開大 → 壓力下降 → 燃燒器提高 → 水位脹縮 → 重新穩定 |
| `load_rejection` | 甩載後調速器讓轉速恢復；閥門卡住則超速跳機並鎖存 |
| `cooling_loss` | 冷凝器壓力上升 → 高壓警報 → >0.25 bar(a) 汽輪機跳機 → 斷路器開 → 主蒸汽閥關；故障清除後**跳機仍鎖存** |
| `feedwater_pump_trip` | 給水歸零 → 水位下降 → 低水位警報 → 低低水位鍋爐跳機 → 燃燒器關閉 → **不允許自動重新點火** |
| `valve_stuck_open` | 位置偏差警報 → 鍋爐壓力下降 → 汽輪機加速 → 超速保護；記錄 FAIL_TO_CLOSE 與 TURBINE_OVERSPEED 的**先後順序** |
| `sensor_bias` | 顯示值與實際值分開驗證：控制器被騙而減少給水，實際水位下降到跳機 |
| `snapshot_roundtrip` | 跳機後存檔 → 還原鎖存仍在 → `--clean` 還原得到未跳機起點，全程不重啟容器 |

---

## 8. 故障注入（需 LAB_MODE=true）

協定層故障與物理層故障分開，四個類別：

```bash
# process：冷卻水能力降到 30%
plantctl fault set --target condenser --category process \
        --name cooling_water_availability --value 0.3

# actuator：主蒸汽閥卡開
plantctl fault set --target steam_valve --category actuator \
        --name valve_mode --spec STUCK_OPEN

# sensor：鍋爐水位量測 +10% 偏差
plantctl fault set --target boiler --category sensor --name level \
        --mode bias --bias 10

# comm：回應延遲 300 ms、5% 丟棄 response
plantctl fault set --target turbine --category comm --name modbus \
        --spec '{"response_delay_ms": 300, "drop_response_prob": 0.05}'

# 觀察後果
plantctl watch
plantctl events --event TRIP_LATCHED
plantctl events --device boiler --limit 20

# 清除全部故障
plantctl fault clear --target '*'
```

第一故障原因驗證（連鎖跳機時**不應被後續跳機覆蓋**）：

```bash
plantctl read --device boiler --register FIRST_OUT_CODE
plantctl events --event FIRST_OUT --limit 5
curl -s http://127.0.0.1:15081/events | python3 -m json.tool | head -40
```

代碼對照見 `docs/alarm-codes.csv`。

### 強制訊號（繞過設備，直接改模擬匯流排）

```bash
plantctl signal boiler.level_pct 25          # 強制
plantctl signal boiler.level_pct --release   # 釋放
```

---

## 9. 外部協定測試工具的搭配

本專案不內建協定測試工具；設備只負責「被打了還要活著且守規格」。
用外部工具（自製 client、Modbus 測試軟體等）驗證時，建議這樣配置環境：

```bash
# 讓崩潰能被外部工具觀察到（不自動重啟）、並開啟逐筆封包日誌
LAB_MODE=true MODBUS_TRACE=true DEVICE_RESTART=no \
  COMPOSE_PROFILES=no-plc docker compose up --build -d
```

每一輪從同一個起點重複測試（毫秒級，不需重啟容器）：

```bash
plantctl snapshot save test-baseline -d "測試基準"
plantctl snapshot restore test-baseline --clean   # 每輪開始前，順便清掉跳機鎖存
```

送完封包後檢查設備是否仍存活、物理安全不變量是否仍成立：

```bash
plantctl status                                  # 設備存活與 offline_devices
curl -s http://127.0.0.1:15080/state | head -40  # 匯流排狀態
curl -s "http://127.0.0.1:15080/events?limit=200"
```

`tools/invariants.py` 的 `InvariantChecker` 可直接餵 `/state` 的內容，
用同一組不變量（質量守恆、壓力／轉速上限、跳機鎖存不得自行解除）做判定。

### 封包錄製與回放（重現問題序列）

```bash
# 以中間人模式錄製：測試工具連 1502，實際轉送到 boiler
python -m tools.modbus_recorder record --listen 0.0.0.0:1502 \
       --target 127.0.0.1:15025 --out cap.jsonl

# 回放
python -m tools.modbus_recorder replay --target 127.0.0.1:15025 \
       --file cap.jsonl --delay 0.005
```

---

## 10. TLS / Modbus Security

```bash
docker compose -f compose.yaml -f compose.secure.yaml --profile secure up --build -d
openssl s_client -connect 127.0.0.1:15802 -showcerts </dev/null | head -20
```

802 埠由 stunnel sidecar 提供 TLS 終結（主機映射 15802），普通 Modbus TCP 仍保留以便相容性測試。

---

## 11. 文件重新產生與清理

```bash
python -m tools.export_docs      # 重新產生 docs/register-map.csv、alarm-codes.csv

docker compose down          # 停止，保留 volume（快照與歷史資料還在）
docker compose down -v       # 連 volume 一起刪（完全重來）
docker compose logs --tail 100 boiler             # 單一設備日誌
```

---

## 附錄 A：最短驗收路徑

```bash
# 1. 離線（20 秒）
pip install -e ".[dev]" && pytest -q                  # 期望 175 passed

# 2. 啟動（LAB_MODE=true）
sed -i '' 's/^LAB_MODE=.*/LAB_MODE=true/' .env
docker compose up --build -d
sleep 30 && docker compose ps

# 3. 健康
curl -s http://127.0.0.1:15080/health
python -m tools.healthcheck modbus 127.0.0.1 15025 && echo OK

# 4. 情境（含快照往返）
plantctl scenario run scenarios/normal_startup.yaml
plantctl scenario run scenarios/snapshot_roundtrip.yaml
plantctl scenario run scenarios/cooling_loss.yaml

# 5. 快照秒級重置
time plantctl snapshot restore steady-60mw --clean
```

---

## 附錄 B：疑難排解

| 症狀 | 原因 / 處理 |
| --- | --- |
| `plantctl: command not found` | 沒 `pip install -e .`，或忘記 `source .venv/bin/activate`。改用 `python -m tools.plantctl` |
| `無法連線 http://127.0.0.1:15080/...` | plant-bus 未啟動或未 healthy：`docker compose ps`、`docker compose logs plant-bus` |
| 情境報 `snapshot steady-60mw not found` | 沒先跑 `normal_startup.yaml`，它才是產生基準快照的那一個 |
| `fault set` 被拒絕 | `.env` 的 `LAB_MODE` 不是 `true`；改完要重新 `docker compose up -d` |
| 埠被占用 | 改 `.env` 的 `BIND_ADDR` 或修改 compose 的主機埠 |
| 情境逾時 FAIL | 機組尚未到穩態就開始檢查，先 `plantctl status` 確認，或 `plantctl speed 5` 加速後重跑 |
| `pytest` 出現 import error | Python < 3.11，或未在專案根目錄執行 |
| 從 LAN 連不進來 | `.env` 的 `BIND_ADDR` 預設 `127.0.0.1`，改成 `0.0.0.0` 後重啟 |

---

## 附錄 C：埠與檔案對照

| 設備 / 服務 | 容器名 | 主機埠 |
| --- | --- | ---: |
| plant-bus 管理 API | `plant-bus` | 15080 |
| Historian | `historian` | 15081 |
| HMI | `hmi` | 15082 |
| PLC（OpenPLC 北向） | `openplc-v4` / `openplc-v3` | 15020 |
| 冷凝器 | `condenser` | 15021 |
| 凝結水泵 | `condensate-pump` | 15022 |
| 給水槽 | `feedwater-tank` | 15023 |
| 給水泵 | `feedwater-pump` | 15024 |
| 鍋爐 | `boiler` | 15025 |
| 主蒸汽閥 | `steam-valve` | 15026 |
| 汽輪機 | `turbine` | 15027 |
| 發電機 | `generator` | 15028 |
| Modbus TLS（secure profile） | `modbus-tls` | 15802 |

HTTP API（management_net，供 CI 與 harness 使用）：

```
GET    /health /state /signals /events /metrics
POST   /sim/pause  /sim/resume  /sim/step  /sim/speed
GET    /snapshot              POST /snapshot/save   POST /snapshot/restore
GET    /snapshot/{name}       DELETE /snapshot/{name}
POST   /fault  /fault/clear  /signal/force
```

參考文件：`docs/architecture.md`、`docs/physics-model.md`、`docs/sequence-of-operation.md`、
`docs/snapshot.md`、`docs/register-map.csv`、`docs/alarm-codes.csv`。

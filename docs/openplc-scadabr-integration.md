# OpenPLC 與 ScadaBR 整合

本專案附帶一套以 `docs/register-map.csv` 自動產生的 OpenPLC 專案（v4 與 v3）與
ScadaBR 1.2 匯入檔。

設備是自持的，因此 OpenPLC 專案只做**資料交換與邏輯判斷**：南向輪詢、watchdog、
脈衝展寬、跳機矩陣、超速保護。它不含任何 PID 或設定值運算，
南向寫入只有命令線圈與 40002～40004 命令暫存器。

資料路徑固定為：

```text
ScadaBR HMI ── Modbus TCP/502 ──> OpenPLC Runtime
                                      ├── 15021 condenser
                                      ├── 15022 condensate_pump
                                      ├── 15023 feedwater_tank
                                      ├── 15024 feedwater_pump
                                      ├── 15025 boiler
                                      ├── 15026 steam_valve
                                      ├── 15027 turbine
                                      └── 15028 generator
```

ScadaBR **只連 OpenPLC**，不可再直接連八台設備。設備會依來源 IP 維持單一寫入者租約；
若 HMI 繞過 PLC 寫入，會與 OpenPLC 爭用租約並收到 Modbus Exception 06。

## 產物

- `integrations/openplc/thermal-plant-v4/`：可由 OpenPLC Editor v4 開啟的原始碼專案
  （compose 服務 `openplc-v4`，預設 profile）。
- `integrations/openplc/thermal-plant-v3/`：OpenPLC v3 的 `.st` 程式、`mbconfig.cfg`、
  Slave Devices 設定表與北向位址表（compose 服務 `openplc-v3`）。
- `integrations/openplc/docker/Dockerfile.v3`：v3 Runtime 容器（官方無發佈映像，自原始碼建置）。
- `integrations/scadabr/`：ScadaBR 1.2 匯入 JSON 與操作說明。
- `tools/generate_openplc_gateway.py`、`tools/generate_openplc_v3_gateway.py`、
  `tools/generate_scadabr_gateway.py`：由唯一介面契約 `docs/register-map.csv`
  重建產物，避免人工複製位址造成漂移。

> **v3 與 v4 的位址不同**。v4 可以自由指定 IEC 位址，因此用本文的扁平佈局；
> v3 的 Modbus master 影像由 Runtime 固定（`%IW100+`／`%QW100+`／`%IX100.0+`／
> `%QX100.0+`，每個緩衝區上限 400 筆），而且沒有空間放 FC3 讀回。
> ScadaBR 匯入檔對應的是 **v4** 佈局；用 v3 時請改用
> `integrations/openplc/thermal-plant-v3/northbound-map.csv` 的位址。

## 位址模型

設備順序固定為：

```text
condenser, condensate_pump, feedwater_tank, feedwater_pump,
boiler, steam_valve, turbine, generator
```

第 `n` 台設備使用 `word_base = n × 64`、`bit_base = n × 16`：

| 設備表 | 設備端功能碼 | OpenPLC 本地區 | ScadaBR 北向範圍 |
| --- | ---: | --- | --- |
| Input Register | FC04 | `%IW(word_base + offset)` | Input Register |
| Discrete Input | FC02 | `%IX(bit_base + offset)` | Input Status |
| Holding Register 命令 | FC16 | `%QW(word_base + offset)` | Holding Register（只有 offset 1~3 會被 PLC 寫到設備） |
| Coil 命令 | FC15 | `%QX(bit_base + offset)` | Coil Status |
| Holding Register 讀回 | FC03 | `%IW(512 + word_base + offset)` | PLC 診斷用 |

每個 CSV 訊號在 ScadaBR 中保留相同的 device、name、工程單位與縮放。`u32` 採高 word
在前，`*_HI` 與緊接的 `*_LO` 合併為一個 32-bit 點，避免同一組 registers 被重疊輪詢。

## 啟動與匯入

1. 啟動模擬器（預設 profile 就會帶起 OpenPLC v4）：

   ```bash
   docker compose up --build -d                       # OpenPLC v4
   COMPOSE_PROFILES=openplc-v3 docker compose up -d   # 改用 v3
   ```

2. 在 OpenPLC Editor 開啟 `integrations/openplc/thermal-plant-v4/`，編譯並部署到
   OpenPLC Runtime v4。

3. 確認 Runtime 的 Modbus Server 已監聽 `0.0.0.0:502`，且八個 Remote Devices
   都已連線。

4. 在 ScadaBR 1.2 匯入 `integrations/scadabr/` 內的 JSON。匯入檔不含使用者、
   密碼、歷史點值或全域系統設定，會沿用既有 ScadaBR 安裝的管理員帳號。

5. 開啟 `Thermal Plant Overview`，先確認所有設備的 watchdog 與通訊品質，再操作
   各設備頁面。

產物預設是 **container 模式**（Remote Device 用 compose 服務名 + 502），因為
`compose.yaml` 已經把 OpenPLC 放在 `control_net` 上。Runtime 若跑在桌面而非
compose，用 `python3 tools/generate_openplc_gateway.py --host-mode host` 重新產生，
Remote Device 會改成 `127.0.0.1:15021` 至 `:15028`。若 Runtime 在另一台電腦，將
`.env` 的 `BIND_ADDR` 設為 `0.0.0.0`，並把 Remote Device host 改成模擬器主機的 LAN IP。

ScadaBR 資料源預設為 `localhost:502`。ScadaBR 若不與 OpenPLC 同機，匯入後只需修改
該資料源的 host，不要改各點位的 offset。

## 命令與安全行為

- `START`、`STOP`、`RESET_TRIP`、`ACK_ALARM`、`TRIP_TEST`、
  `CLEAR_TOTALIZER`、`BREAKER_CLOSE`、`BREAKER_OPEN` 是脈衝命令。HMI 只送
  `true`；PLC 將命令展寬後自動清零。
- `EMERGENCY_STOP` 與 `FORCE_SAFE` 是保持命令，可由 HMI 明確開啟與解除。
- HMI 觸發 `RESET_TRIP` 時，PLC 會填入 `RESET_KEY = 42330`，並產生新的非零
  `COMMAND_SEQUENCE`；設備仍會自行檢查安全條件。
- PLC 每 200 ms 更新八台設備的非零 watchdog，並比對 `WATCHDOG_ECHO`。
- 跳機矩陣與超速先於一般操作，且**一律以命令表達**：發電機 `BREAKER_OPEN`、
  下游設備 `STOP` 脈衝。PLC 不寫燃燒器輸出或負載設定——那些是設備自己的事。
- 正常操作只需要 `START`／`STOP`：`START` 解除操作員停機鎖並讓設備恢復自持，
  `STOP` 把設備鎖在停機。設定值與手動輸出在 AUTO 模式下不會生效。
- HMI 可直接觀察自持狀態：`LOCAL_OUTPUT`(30026)、`SELF_HOLD_STATE`(30027)、
  `PERMISSIVE_WORD`(30028)。
- `feedwater_tank` 是被動設備，HMI 不提供無效的 START／STOP；`generator` 頁面
  另外提供 breaker 開／關（v4 才有；v3 的 coil 區只到 offset 7）。

## 重新產生與驗證

修改 `docs/register-map.csv` 後執行：

```bash
python tools/generate_openplc_gateway.py          # v4
python -m tools.generate_openplc_v3_gateway       # v3
python tools/generate_scadabr_gateway.py
pytest -q tests/integration/test_openplc_scadabr_artifacts.py
```

產生器支援的檢查模式與其他選項請以各整合目錄的 `README.md` 為準。

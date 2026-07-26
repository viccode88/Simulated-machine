# OpenPLC 與 ScadaBR 整合

本專案附帶一套以 `docs/register-map.csv` 自動產生的 OpenPLC Editor 專案與
ScadaBR 1.2 匯入檔。資料路徑固定為：

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

- `integrations/openplc/thermal-plant-v4/`：可由 OpenPLC Editor 開啟的原始碼專案。
- `integrations/openplc/thermal-plant-v4/northbound-map.csv`：OpenPLC 對 ScadaBR
  提供的扁平位址表。
- `integrations/scadabr/`：ScadaBR 1.2 匯入 JSON 與操作說明。
- `tools/generate_openplc_gateway.py`、`tools/generate_scadabr_gateway.py`：由唯一介面契約
  `docs/register-map.csv` 重建產物，避免人工複製位址造成漂移。

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
| Holding Register 命令 | FC16 | `%QW(word_base + offset)` | Holding Register |
| Coil 命令 | FC15 | `%QX(bit_base + offset)` | Coil Status |
| Holding Register 讀回 | FC03 | `%IW(512 + word_base + offset)` | PLC 診斷用 |

每個 CSV 訊號在 ScadaBR 中保留相同的 device、name、工程單位與縮放。`u32` 採高 word
在前，`*_HI` 與緊接的 `*_LO` 合併為一個 32-bit 點，避免同一組 registers 被重疊輪詢。

## 啟動與匯入

1. 啟動不含內建 DCS 的模擬器，將設備控制權留給 OpenPLC：

   ```bash
   docker compose --profile external-plc up --build -d
   ```

2. 在 OpenPLC Editor 開啟 `integrations/openplc/thermal-plant-v4/`，編譯並部署到
   OpenPLC Runtime v4。

3. 確認 Runtime 的 Modbus Server 已監聽 `0.0.0.0:502`，且八個 Remote Devices
   都已連線。

4. 在 ScadaBR 1.2 匯入 `integrations/scadabr/` 內的 JSON。匯入檔不含使用者、
   密碼、歷史點值或全域系統設定，會沿用既有 ScadaBR 安裝的管理員帳號。

5. 開啟 `Thermal Plant Overview`，先確認所有設備的 watchdog 與通訊品質，再操作
   各設備頁面。

產物預設假設 OpenPLC Runtime 與模擬器在同一台主機，Remote Device 使用
`127.0.0.1:15021` 至 `127.0.0.1:15028`。若 Runtime 容器已加入
`control_net`，用 `python3 tools/generate_openplc_gateway.py --host-mode container`
切換成 Compose service names；未加入該網路的桌面容器可使用
`host.docker.internal:15021` 至 `:15028`。若 Runtime 在另一台電腦，將 `.env`
的 `BIND_ADDR` 設為 `0.0.0.0`，並把 Remote Device host 改成模擬器主機的 LAN IP。

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
- 跳機矩陣與超速先於一般操作：包含關閉發電機 breaker、關主蒸汽閥、燃燒器歸零、
  發電機負載歸零與給水降至安全輸出。
- `feedwater_tank` 是被動設備，HMI 不提供無效的 START／STOP；`generator` 頁面
  另外提供 breaker 開／關。

## 重新產生與驗證

修改 `docs/register-map.csv` 後執行：

```bash
python tools/generate_openplc_gateway.py
python tools/generate_scadabr_gateway.py
pytest -q tests/integration/test_openplc_scadabr_artifacts.py
```

產生器支援的檢查模式與其他選項請以各整合目錄的 `README.md` 為準。

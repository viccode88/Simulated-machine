# ScadaBR 1.2：OpenPLC 平坦廠區閘道

[`scadabr.json`](scadabr.json) 是可直接從 ScadaBR 1.2「匯入／匯出」頁面匯入的設定。它只包含必要的 `dataSources`、`dataPoints`、`watchLists` 與 `graphicalViews`，不會覆蓋或建立使用者、系統設定、歷史點值，也不帶入附件的舊 XID。

## 拓撲

```text
8 個 simulator device Modbus servers
              │
              ▼
 OpenPLC（南向輪詢、邏輯、平坦位址映像）
              │ Modbus/TCP :502, Unit ID 1
              ▼
 ScadaBR（單一 MODBUS_IP data source，250 ms）
```

ScadaBR 應連 OpenPLC 的 northbound Modbus/TCP server，不要再各自直連八個 simulator device。預設 host 是 `localhost`，只適用於 ScadaBR 與 OpenPLC 位於同一台主機且 OpenPLC 在 TCP 502 監聽的情況。

若 OpenPLC 在別台主機或容器中，可重新生成：

```bash
python3 tools/generate_scadabr_gateway.py --host 192.0.2.10
```

也可在匯入前只修改 JSON 中唯一 data source 的 `host`；port 固定為 `502`、`slaveId` 固定為 `1`。Docker Compose 通常可將 `--host` 設為 OpenPLC service name。

## 生成與驗證

生成器唯一資料來源是 [`../../docs/register-map.csv`](../../docs/register-map.csv)：

```bash
python3 tools/generate_scadabr_gateway.py
python3 tools/generate_scadabr_gateway.py --check
```

`--check` 會重新解析 CSV、重新生成記憶體中的文件、依 ScadaBR 1.2 官方 Java VO/importer 欄位做靜態驗證，並逐 byte 比對已提交的 JSON，因此可同時發現 schema、位址、權限或產物過期。

平坦映像的配置固定為：

| CSV table | OpenPLC image | 設備區塊 |
| --- | --- | ---: |
| `INPUT` | `%IW` / Input Register | 64 words |
| `DISCRETE` | `%IX` / Discrete Input | 16 bits |
| `HOLDING` | `%QW` / Holding Register | 64 words |
| `COIL` | `%QX` / Coil | 16 bits |

設備順序為 `condenser`、`condensate_pump`、`feedwater_tank`、`feedwater_pump`、`boiler`、`steam_valve`、`turbine`、`generator`。所以每個 ScadaBR locator 的 offset 都是「設備 index × block + CSV `pdu_offset`」。

32 位元 `u32` 只建立 `_HI` 所在 offset 的一個 `FOUR_BYTE_INT_UNSIGNED` 點，使用 High Word First，緊接的 `_LO` 不會重複建立。`i16` 使用 `TWO_BYTE_INT_SIGNED`。工程縮放一律是 `multiplier = 1 / scale`，`engineeringUnits` 與畫面 suffix 都保留 CSV 的原始單位字串。

## 匯入與權限

1. 先啟動八個 simulator device 與 OpenPLC，確認 OpenPLC northbound TCP 502 可到達。
2. 用 ScadaBR 管理員登入，開啟「Import/Export」，貼上或上傳 `scadabr.json` 後執行匯入。
3. 匯入預設引用既有的 `admin` 使用者作為 9 個 watchlist 與 9 個 graphical view 的 owner；JSON 故意沒有 `users` root array。若安裝中沒有 `admin`，用 `--owner 現有帳號` 重新生成。
4. 若操作員不是 owner/admin，管理員必須授予該使用者 data point 的 **set/write** 權限，以及 graphical view 的 **SET** 權限。只給 read 權限時，HMI 會顯示資料但不會接受命令。

每個可寫 coil/holding point 的 locator `settableOverride` 都是 `true`；HMI 上的控制元件也另有一層 `settableOverride: true`。Input Register 與 Discrete Input 永遠是唯讀。

## HMI 行為

`Thermal Plant Overview` 顯示八個 device 的就緒、運轉、跳機、警報、狀態與關鍵 PV。八張 detail view 顯示完整操作狀態、關鍵 PV、有效設定值與命令：

- `START`、`STOP`、`RESET_TRIP`、`ACK_ALARM` 與 generator breaker 命令使用原生 `SCRIPT` point component，按鈕只寫 `TRUE`；PLC/device 的 one-shot 會自行清除。
- `EMERGENCY_STOP` 與 `FORCE_SAFE` 是保持型，使用原生 `BUTTON`，可明確投入與解除。
- `RESET_TRIP` 前先把 `RESET_KEY` 設成十進位 `42330`（`0xA55A`），並將 `COMMAND_SEQUENCE` 改成未用過的新值。
- `feedwater_tank` detail view 刻意不放無效的 `START`／`STOP`。
- generator detail view 含 breaker status、`BREAKER_CLOSE` 與 `BREAKER_OPEN`。
- 寫入先由 OpenPLC/device command queue 接收，設備在下一掃描才套用；操作後應看 `DEVICE_STATE`、`RUNNING`、`ACCEPTED_COMMAND_COUNT`／`REJECTED_COMMAND_COUNT`，不要把立即 read-back 當成成功判定。

## ScadaBR 1.2 schema 依據

靜態驗證對應 ScadaBR 官方 1.2 原始碼中的：

- [`ImportTask`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/web/dwr/beans/ImportTask.java)
- [`ModbusIpDataSourceVO`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/vo/dataSource/modbus/ModbusIpDataSourceVO.java)
- [`ModbusPointLocatorVO`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/vo/dataSource/modbus/ModbusPointLocatorVO.java)
- [`DataPointVO`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/vo/DataPointVO.java)
- [`View`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/view/View.java)、[`WatchList`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/vo/WatchList.java)
- 原生 [`SIMPLE`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/view/component/SimplePointComponent.java)、[`HTML`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/view/component/HtmlComponent.java)、[`BUTTON`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/br/org/scadabr/view/component/ButtonComponent.java) 與 [`SCRIPT`](https://github.com/ScadaBR/ScadaBR/blob/v1.2/src/com/serotonin/mango/view/component/ScriptComponent.java) component。

# 物理模型

所有數值都是模擬設計基準，不代表特定真實機組。參數集中在 `configs/*.yaml`。

## 額定條件

| 項目 | 額定值 |
| --- | ---: |
| 發電機輸出 | 100 MW |
| 鍋爐壓力 | 100 bar(a) |
| 主蒸汽溫度 | 500 °C |
| 額定蒸汽流量 | 100 kg/s |
| 鍋爐正常水位 | 66.7 % |
| 給水槽正常水位 | 60 % |
| 冷凝器正常壓力 | 0.08 bar(a) |
| 汽輪機額定轉速 | 3000 RPM |
| 主蒸汽閥額定流量 | 120 kg/s |
| 給水泵／凝結水泵額定流量 | 120 kg/s |
| 冷凝能力 | 110 kg/s |
| 鍋爐／給水槽有效水量 | 約 30,000 kg |
| 熱井有效水量 | 約 20,000 kg |

壓力一律以絕對壓力 `bar(a)` 參與公式。

## 鍋爐

```
dM/dt        = Mfeedwater - Mevaporation - Mblowdown - Mleak
Level_actual = 100 × (M - Mmin) / (Mmax - Mmin)
Level_ind    = Level_actual + Kswell × (Mevaporation - Mfeedwater)      # 汽包脹縮
Mevap_target = RatedSteamFlow × BurnerOutput × WaterAvailability
dMevap/dt    = (Mevap_target - Mevap) / TauBoiler                       # Tau = 20 s
dP/dt        = Kpressure × (Mevap - Msteam_out - Mrelief) - Kloss × (P - Pambient)
T_sat        ≈ 100 × P^0.25                                            # 1 bar→100°C，100 bar→316°C
T_steam      = T_sat + SuperheatMax × BurnerOutput × ramp(P, 5, 60)
```

* 燃燒器升速限制 5 %/s、降速限制 10 %/s。
* `WaterAvailability` 在水位 5～20% 之間線性降至 0。
* 安全閥在 113 bar 開始排放（容量 40 kg/s），是 115 bar 跳機前的第一道防線；
  可用 `actuator: relief_disabled` 故障關閉它來測試超壓跳機。
* 狀態：`OFF → PURGING → IGNITING → PRESSURIZING → RUNNING`，任何時候可 `TRIPPED`。
* 跳機後：燃燒器立即 0%、切斷燃料、發布 `boiler.tripped` 讓主蒸汽閥快關，
  並依第一故障原因決定給水策略（高高水位停止給水；低低水位允許補水但不得自動復燃）。

## 主蒸汽閥

```
dPos/dt = clamp(Command - Actual, -CloseRate, OpenRate)
r       = Pdownstream / Pupstream
FlowFactor = 1                                     若 r ≤ rcritical(0.55)
           = sqrt((1-r)/(1-rcritical))             否則
Msteam  = Krated × Opening × (Pup/Prated) × sqrt(Tref/Tsteam) × FlowFactor
```

* 正常全行程開啟 5 s、關閉 3 s；跳機快關 0.4 s。
* 故障模式：`STUCK_OPEN`、`STUCK_CLOSED`、`STUCK_POSITION`、`SLOW_TRAVEL`、
  `POSITION_FEEDBACK_BIAS`、`FAIL_TO_CLOSE`、`ACTUATOR_POWER_LOSS`；預設失效位置 `FAIL_CLOSE`。
* 關閥命令超過 3 秒仍未關到位 → `FAIL_TO_CLOSE` 鎖存（保留與超速跳機的先後順序）。

## 汽輪機

```
Efficiency  = 1 - VacuumPenalty × (Pexhaust - 0.08)
Pmech       = Kturbine × Msteam × (Psteam/Prated)^0.2 × Efficiency
dω/dt       = (Pmech - Pelec - D × (ω - ω0)) / EquivalentInertia
RPM         = ω × 60 / (2π)
Mexhaust    → 一階遲滯追隨 Msteam（內部蒸汽庫存 τ = 2 s）
```

`EquivalentInertia = 3.18 MW·s²/rad` 相當於 H ≈ 5 s 的 100 MW 機組：
滿載甩載時約 300 RPM/s，若閥門未快關即會在 1 秒內觸及 3300 RPM 超速跳機。

## 發電機

* **孤島模式（預設）**：`Pelectrical = LoadDemand`，負載直接變成軸上反向轉矩。
* **強電網模式**：`Pelectrical = Pmech + K × (RPM - 3000)`，轉速被電網鎖定，
  蒸汽閥改為控制有功功率。
* 相角差以滑差積分模擬：`dθ/dt = 360 × (f - f_grid)`，因此併聯前需要微幅速差讓相角掃入允許範圍。
* 斷路器閉合允許：轉速接近 3000 RPM、頻率 49.5～50.5 Hz、電壓 95～105%、
  相角差 < 10°、汽輪機未跳機、無電氣保護動作。
* 汽輪機跳機 → 斷路器立即打開、電氣功率降為 0（甩載造成的加速由汽輪機模型自然呈現）。

## 冷凝器

```
Mcapacity = RatedCapacity × CoolingWaterAvailability × HeatTransferFactor
Excess    = max(0, Mexhaust - Mcapacity)
Pmin      = 0.04 + 0.0004 × Mexhaust                    # 100 kg/s 時約 0.08 bar(a)
dP/dt     = Koverload × Excess - Kvacuum × VacuumOutput × (P - Pmin) + AirLeak
dMhotwell/dt = Mcondensed + Mmakeup - Mcondensate_pump - Mleak
T_condensate = Antoine 反算飽和溫度（0.08 bar → 41.6 °C）
```

`Koverload = 0.02 bar/(kg/s)/s`：排汽超過冷凝能力時真空快速惡化，
20 kg/s 的超載就會讓壓力以 0.4 bar/s 上升。

## 泵浦（凝結水泵／給水泵）

```
PumpHead = ShutoffHead × Speed²
Flow     = RatedFlow × Speed × sqrt(max(0, 1 - ΔP/PumpHead)) × CavitationFactor × ValveFactor
```

* `CavitationFactor` 依來源水位在 `cavitation_level_low`～`cavitation_level_ok` 之間降至 0；
  汽蝕時流量與馬達電流波動、振動上升，持續超過 20 秒即跳機。
* 給水泵必須先達到能建立排出揚程的最低轉速（`head_floor`），
  否則即使旋轉也無法對抗鍋爐壓力形成有效進水——這正是「泵浦排出壓力必須大於鍋爐壓力」的體現。
* 給水泵另有出口閥：關閉時流量趨近零、排出壓力上升。

## 給水槽（除氧器）

```
dM/dt = Mcondensate_in - Mfeedwater_out - Mleak - Moverflow
Level = 100 × (M - Mmin) / (Mmax - Mmin)
Tank pressure = 飽和壓力(水溫)          # 150 °C → 約 4.76 bar(a)
```

水位高於 100% 時模擬溢流；低水位警報、低低水位禁止給水泵啟動或使其跳機。

## 典型反應對照

| 事件 | 反應 |
| --- | --- |
| 蒸汽閥開大 | 機械功率與轉速上升 |
| 發電機負載增加 | 轉速短暫下降，調速器開閥，鍋爐壓力短暫下降，水位出現脹縮 |
| 發電機甩載 | 轉速快速上升；閥門正常則回穩，卡住則超速跳機 |
| 冷卻水能力下降 | 冷凝器壓力上升、汽輪機效率與功率下降，超過 0.25 bar(a) 跳機 |
| 凝結水泵停止 | 熱井水位上升、給水槽水位下降 |
| 給水泵跳機 | 鍋爐水位逐漸下降 → 低水位警報 → 低低水位跳機 |
| 鍋爐壓力上升 | 相同泵浦轉速下給水流量下降 |

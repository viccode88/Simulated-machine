# Thermal Plant OpenPLC v4 gateway

Source-only OpenPLC Editor v4 project generated from `docs/register-map.csv`.
The stale `build/` output of the reference `v4.rar` is intentionally excluded.

The eight devices are **self-holding**: they start themselves when their
permissives are satisfied and run their own regulators.  This project is
therefore a pure gateway plus interlock logic:

* southbound: poll every device (FC4/FC2/FC3) and write only commands
  (FC15 coils, FC16 offsets 1..3)
* northbound: one flat Modbus image on `0.0.0.0:502` for SCADA/HMI
* logic: watchdog + echo supervision, pulse stretching, trip matrix, overspeed

It computes **no** control value.  Setpoints, manual outputs, output limits and
PID parameters are never written: they belong to the devices.

Generated southbound mode: **container**.

## Generate and verify

From the simulator repository root:

```sh
python3 tools/generate_openplc_gateway.py
python3 tools/generate_openplc_gateway.py --check
```

Container mode is the default because `compose.yaml` defines the `openplc-v4`
service on `control_net`.  Use `--host-mode host` when the Runtime runs on the
desktop instead of in Compose; that regenerates the remotes with
`127.0.0.1:1502x`.

| Device | Container mode | Host mode |
| --- | --- | --- |
| `condenser` | `condenser:502` | `127.0.0.1:15021` |
| `condensate_pump` | `condensate-pump:502` | `127.0.0.1:15022` |
| `feedwater_tank` | `feedwater-tank:502` | `127.0.0.1:15023` |
| `feedwater_pump` | `feedwater-pump:502` | `127.0.0.1:15024` |
| `boiler` | `boiler:502` | `127.0.0.1:15025` |
| `steam_valve` | `steam-valve:502` | `127.0.0.1:15026` |
| `turbine` | `turbine:502` | `127.0.0.1:15027` |
| `generator` | `generator:502` | `127.0.0.1:15028` |

Every device uses Modbus TCP Unit ID 1.  FC4, FC2, and FC3 reads run every
250 ms.  FC15/FC16 writes run every 100 ms and therefore
continually refresh the controller lease and the watchdog.

## Flat northbound layout

For device index `i` in the fixed order below:

```text
word_base = 64 * i
bit_base  = 16 * i
```

| Northbound table | Local image | Content |
| --- | --- | --- |
| FC4 Input Registers | `%IW[word_base + 0..49]` | device FC4 process/diagnostic inputs |
| FC2 Discrete Inputs | `%IX[bit_base + 0..15]` | device FC2 status bits |
| FC4 Input Registers | `%IW[512 + word_base + 0..31]` | device FC3 holding readback |
| FC1/5/15 Coils | `%QX[bit_base + offset]` | device FC15 command coils |
| FC3/6/16 Holding Registers | `%QW[word_base + offset]` | command registers (see below) |

Fixed device order:

```text
0 condenser
1 condensate_pump
2 feedwater_tank
3 feedwater_pump
4 boiler
5 steam_valve
6 turbine
7 generator
```

`northbound-map.csv` is the exact HMI import/reference map.  Its offsets are
zero-based PDU offsets; do not send the 3xxxx/4xxxx documentation address on the
wire.  The `plc_forwards` column says whether the PLC actually writes that point
down to the device.  Only the command coils and holding offsets
1..3 are forwarded; everything else is
read-only data exchange, because the devices regulate themselves.

## Values and HMI behavior

Values remain raw 16-bit Modbus words.  Decode with:

```text
engineering_value = raw / scale
```

`i16` uses two's-complement; `u32` is high-word first.  The CSV contains units,
types, limits, writable flags, pulse flags, and descriptions for HMI widgets.

Self-holding is observable from the HMI without any extra logic:

| Register | Meaning |
| --- | --- |
| `30026 LOCAL_OUTPUT` | the device's own regulator output |
| `30027 SELF_HOLD_STATE` | 0 disabled, 1 standby, 2 self-holding, 3 operator stop, 4 trip locked, 5 maintenance |
| `30028 PERMISSIVE_WORD` | one bit per start permissive |

The ST program advances each non-zero watchdog every 200 ms and checks that the
FC4 echo keeps progressing; an unchanged mismatch for 3 s raises the device's
comm-lost flag (a status only - the devices keep running on local control).
It implements the rising-edge trip matrix as *commands*: a `STOP` pulse on the
downstream device and `BREAKER_OPEN` on the generator, plus a `STOP` pulse on
the main steam valve above 3150 RPM.  A device stopped this way
stays stopped until an operator presses `START`, which is also what releases it
back into self-holding operation.

HMI writes to pulse coils (`START`, `STOP`, `RESET_TRIP`, `ACK_ALARM`,
`TRIP_TEST`, `CLEAR_TOTALIZER`, plus generator breaker commands) are held for
approximately 160 ms and then cleared.  The
latched `EMERGENCY_STOP` and `FORCE_SAFE` coils are never auto-cleared.
On a `RESET_TRIP` rising edge the PLC increments the command sequence (skipping
zero) and applies reset key `0xA55A` for the pulse window.

Normal operation only needs `START` and `STOP`: `START` clears the operator-stop
lock and lets the device self-hold again, `STOP` latches it stopped.

## Safety notes

- Keep the OpenPLC cyclic task at 20 ms unless pulse and watchdog
  scan constants are reviewed together.
- OpenPLC Runtime v4 serializes remote groups.  Cycle time is a target, not a
  hard real-time guarantee.
- Modbus TCP has no authentication.  Restrict port 502 to the control/HMI
  network.
- `EMERGENCY_STOP` and `FORCE_SAFE` are maintained commands.  The operator must
  explicitly write `false` after the plant is safe and reset authorization is
  established.

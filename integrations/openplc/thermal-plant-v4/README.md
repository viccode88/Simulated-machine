# Thermal Plant OpenPLC v4 gateway

This is a source-only OpenPLC Editor v4 project generated from
`docs/register-map.csv` and the source layout of the supplied `v4.rar`.
The archive's stale `build/` output is intentionally excluded.

Generated southbound mode: **host**.

## Generate and verify

From the simulator repository root:

```sh
python3 tools/generate_openplc_gateway.py
python3 tools/generate_openplc_gateway.py --check
```

Host mode is the default because this repository does not define an OpenPLC
Compose service on `control_net`, and it matches the supplied `v4.rar`.
Use `--host-mode container` only when the OpenPLC Runtime has explicitly been
attached to `control_net`; that regenerates the remotes with service names and
port 502.

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
continually refresh the controller lease.

## Flat northbound layout

The OpenPLC Modbus server listens on **0.0.0.0:502**.  SCADA/HMI clients connect
to the OpenPLC Runtime, not directly to the devices.  For device index `i` in
the fixed order below:

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
| FC3/6/16 Holding Registers | `%QW[word_base + offset]` | device FC16 commands/setpoints |

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
zero-based PDU offsets.  Do not send the 3xxxx/4xxxx documentation address on
the wire.

## Values and HMI behavior

Values remain raw 16-bit Modbus words.  Decode with:

```text
engineering_value = raw / scale
```

`i16` uses two's-complement; `u32` is high-word first.  The CSV contains units,
types, limits, writable flags, pulse flags, and descriptions for HMI widgets.

The ST program explicitly copies every contract-safe value into `%QW` on its
first scan (located-variable declaration initializers alone are not reliable
with Runtime v4), advances each non-zero watchdog every 200 ms, and checks that
each FC4 watchdog echo continues to progress.  A stale mismatch for 3 seconds
applies the documented communication fail-safe policy.  It also implements the
same rising-edge trip matrix as `examples/external_plc.py` and closes the main
steam valve above 3150 RPM.

HMI writes to pulse coils (`START`, `STOP`, `RESET_TRIP`, `ACK_ALARM`,
`TRIP_TEST`, `CLEAR_TOTALIZER`, plus generator breaker commands) are held for
approximately 160 ms and then cleared.  The
latched `EMERGENCY_STOP` and `FORCE_SAFE` coils are never auto-cleared.
On a `RESET_TRIP` rising edge the PLC increments the command sequence (skipping
zero) and applies reset key `0xA55A` for the pulse window.

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

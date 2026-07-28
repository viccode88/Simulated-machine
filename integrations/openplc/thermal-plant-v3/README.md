# Thermal Plant OpenPLC v3 gateway

OpenPLC Runtime v3 version of the gateway.  Same role as the v4 project: the
八 devices are self-holding, so the PLC only exchanges data and evaluates
logic - watchdog, echo supervision, pulse stretching, trip matrix, overspeed.
It never writes a setpoint or a manual output.

Generated southbound mode: **container**.

```sh
python3 -m tools.generate_openplc_v3_gateway
python3 -m tools.generate_openplc_v3_gateway --check
```

## Why v3 needs its own map

v3 does not let the editor choose IEC addresses.  Its Modbus master packs every
configured slave device into fixed buffers (`modbus_master.cpp`):

```text
%IX100.0+   discrete inputs      %QX100.0+   coils
%IW100+     input registers      %QW100+     holding registers (write)
```

Each buffer holds at most 400 entries, so with eight devices this project polls
**49 input words** per device (392 of 400) and therefore has:

* no FC3 holding readback (v4 has it at `%IW512+`) - SCADA cannot read back
  setpoints through v3;
* one 8-bit coil block per device, so the generator breaker coils (offsets
  9/10, v4 only) are not exposed.  The generator synchronises itself, and the
  trip matrix uses `STOP`, so no plant function is lost.

The device **order below is what fixes every address** - do not reorder it.

| # | Device | Container mode | Host mode | Input regs | Discretes | Coils | Commands |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 0 | `condenser` | `condenser:502` | `127.0.0.1:15021` | %IW100..%IW148 | %IX100.0 | %QX100.0 | %QW100 |
| 1 | `condensate_pump` | `condensate-pump:502` | `127.0.0.1:15022` | %IW149..%IW197 | %IX102.0 | %QX101.0 | %QW103 |
| 2 | `feedwater_tank` | `feedwater-tank:502` | `127.0.0.1:15023` | %IW198..%IW246 | %IX104.0 | %QX102.0 | %QW106 |
| 3 | `feedwater_pump` | `feedwater-pump:502` | `127.0.0.1:15024` | %IW247..%IW295 | %IX106.0 | %QX103.0 | %QW109 |
| 4 | `boiler` | `boiler:502` | `127.0.0.1:15025` | %IW296..%IW344 | %IX108.0 | %QX104.0 | %QW112 |
| 5 | `steam_valve` | `steam-valve:502` | `127.0.0.1:15026` | %IW345..%IW393 | %IX110.0 | %QX105.0 | %QW115 |
| 6 | `turbine` | `turbine:502` | `127.0.0.1:15027` | %IW394..%IW442 | %IX112.0 | %QX106.0 | %QW118 |
| 7 | `generator` | `generator:502` | `127.0.0.1:15028` | %IW443..%IW491 | %IX114.0 | %QX107.0 | %QW121 |

## Setup

1. Start the simulator with the v3 profile:

   ```sh
   COMPOSE_PROFILES=openplc-v3 docker compose up --build
   ```

2. Open the v3 web interface on <http://127.0.0.1:15083> (default login
   `openplc` / `openplc`).

3. Programs -> Upload `thermal-plant-v3.st`, then compile it.

4. Slave Devices -> add the eight devices exactly in the order and with the
   ranges in `slave-devices.csv`.  Alternatively copy `mbconfig.cfg` into the
   runtime's webserver directory.

5. Settings -> enable **Modbus** (the northbound server on port 502, published
   as `127.0.0.1:15020`), then Start PLC.

`northbound-map.csv` is the SCADA/HMI address list for this layout.  Note that
the ScadaBR import shipped in `integrations/scadabr/` targets the **v4** flat
layout; for v3 use these addresses instead.

## What the operator does

`START` clears the operator-stop lock and lets the device hold itself again;
`STOP` latches it stopped.  Everything else - setpoints, ramps, sequencing -
is the device's own business.

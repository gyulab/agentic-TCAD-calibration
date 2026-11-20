# Example device

The device definition that all three implementations read: the bottom-gate In-W-O (IWO) transistor of the
manuscript, with the 2%-W reference device at two channel lengths (`ref_L1`, `ref_L2`) and a 4%-W device for the
recalibration example (`recal_L1`).

| File | Content |
|---|---|
| `device.toml` | Devices, geometries, calibration and test curves, gate sweep, metric current levels, structure, materials, region names, mesh refinement and the device wording of the agent instructions |
| `DEVICE_INFO.md`, `DEVICE_INFO_recal.md` | Device and measurement facts given to the agent (calibration and recalibration) |
| `deck0/` | Start deck: the structure of Table I of the manuscript, basic device models and literature values, without calibrated parameters |

For your own device, copy this folder to `devices/<name>/`, edit the three parts and set `TCADCAL_DEVICE` to the
copy before creating a workspace. The [device setup guide](../../docs/device-setup.md) lists the steps.

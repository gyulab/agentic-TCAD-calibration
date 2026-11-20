# Device and measurement (fixed facts)

## Device
| Item | Value |
|---|---|
| Device | Fabricated bottom-gate transistor with an amorphous In-W-O (IWO) channel, 2 % W |
| Channel length L (source/drain inner-edge gap) | 1.0 um |
| Channel width W | 5.0 um (2D simulation, AreaFactor = 5, current in A for the full width) |
| Source/drain overlap with the gate | 1.5 um per side (3.0 um total) |
| IWO thickness | 3.5 nm |
| Gate electrode | Pd, bottom gate |
| Gate dielectric | HfO2, 7.0 nm, bottom gate |
| Passivation | Conformal HfO2, 5.0 nm |
| Source/drain contacts | Pd, 40 nm, on top of the channel |
| Dielectric constants | k_IWO = 9, k_HfO2 = 17 (fixed inputs) |

The 2D structure in `state/decks/v000/sde_dvs.cmd` implements these values. The harness
reads every mesh back and stops if any dimension differs.

## Measurement
- Semiconductor parameter analyzer, three-terminal I_D-V_G at fixed V_D, source grounded,
  room temperature.
- Gate sweep from -1.0 V to 2.0 V in 50 mV steps. Only the forward branch (-1.0 V to 2.0 V,
  61 points) is provided and used.
- Calibration curves: V_D = 0.05 V and V_D = 1.0 V (`inputs/meas/`). Columns: `VG_V`, `ID_A`
  (signed drain current as measured; values near zero in the off state are instrument noise).

## Measurement protocol facts (supplied measurement facts)
- One die. Each drain bias was swept once. Only the two calibration curves are provided.
- No capacitance-voltage (C-V) measurement exists for this device.
- One channel length is available for this calibration. There is no channel-length series of the same die.

## Start model (deck v000)
- Electrostatics and drift-diffusion with Fermi statistics, 300 K.
- IWO: no band-gap narrowing (the material parameter file is a silicon template; its band-gap
  narrowing constants are not used), SRH recombination with field-enhanced lifetime (Hurkx),
  nonparabolic conduction band, constant mobility.
- Electron affinity 4.3 eV and band gap 2.9 eV (literature values cited in the parameter file).
- Source/drain: Schottky contacts with a work function parameter.
- Active parameters and start values: gate work function 4.4 eV, source/drain work function
  4.7 eV, effective donor density 1e18 cm^-3, electron mobility 10 cm^2/Vs.

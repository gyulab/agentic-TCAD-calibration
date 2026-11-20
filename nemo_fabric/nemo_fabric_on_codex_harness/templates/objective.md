# Objective J, device metrics and calibration target (fixed before calibration)

J compares the TCAD and measured transfer curves term by term. Each term is the TCAD-minus-measured
difference divided by its tolerance, squared. J is the mean of the terms, so J <= 1 means the model
is within tolerance on average. The calibration target is stricter: every term within tolerance.

    J = mean over the terms of ((TCAD - measured) / tolerance)^2

## Terms (14 on the two calibration curves)
| Term | Definition | Tolerance | Number |
|---|---|---|---|
| V_th | V_G at \|I_D\| = [[VTH_PER_SQUARE]] x W/L = [[VTH_LEVEL]] (W/L = [[W_UM]] um / [[L_UM]] um), at each V_D | 50 mV | 2 |
| SS profile | SS in one-decade current windows from the measured detection limit to [[VTH_LEVEL]] (see below); the term is the window with the largest relative error, at each V_D | 15 % of the measured SS of that window | 2 |
| log10 I_on | maximum \|I_D\| of the sweep, at each V_D | 0.05 dec | 2 |
| E_lin | RMS of (I_TCAD - I_meas) / I_on,meas where I_meas >= I_on,meas / 10 (on-state linear current), at each V_D | 0.10 | 2 |
| Curve error | mean r_i^2 over the points of one operating region, at each V_D, for the subthreshold, turn_on and on regions | per point, see below | 6 |

A metric that cannot be extracted (for example V_th when the TCAD current never reaches [[VTH_LEVEL]])
counts as 3 tolerances.

**Reported in every evaluation, not J terms:**
- the drain-bias threshold shift V_th(V_D = [[VD_HI]] V) - V_th(V_D = [[VD_LO]] V), TCAD and measured
- the peak transconductance g_m,max = max dI_D/dV_G of each curve and the V_G of the peak, TCAD and measured

## SS profile
- **Windows.** One decade of current wide, in half-decade steps, from the detection limit of each measured
  curve up to [[VTH_LEVEL]], plus one window ending at [[VTH_LEVEL]]. A window with fewer than 2 measured points is
  widened upward in half-decade steps (at most to [[VTH_LEVEL]]), and its SS uses its actual span. The windows
  are fixed from the measured curves before the calibration. Every evaluation lists them.
- **Extraction.** SS of a window = the V_G span between the crossings of its two edge currents divided by its
  span in decades. Crossings are interpolated linearly in log10 I between points, on the curve's monotone
  envelope (the V_th extraction). TCAD and measured use the same windows, the same V_G points (TCAD on the
  measured grid) and the same extraction.
- **Term.** Error of a window = (SS_TCAD - SS_meas) / (0.15 x SS_meas). The SS-profile term of a curve is
  the error of its worst window. If the TCAD curve does not cross a window edge, it counts as 3
  tolerances.
- **Reported with it.** The SS of every window (TCAD, measured), the minimum SS of each curve, and the
  average SS between 1 and 100 nA (reported, not a term of J).

## Curve error
For each calibration curve (V_D = [[VD_LO]] V and [[VD_HI]] V) and each of its [[N_FORWARD]] forward points i:

    g_X,i     = log10( max(|I_X,i|, I_DL) )          X = TCAD or measured
    S_X,i     = |d g_X / d V_G|                      local slope
    tol_i^2   = (0.05 V x max(S_meas,i, S_TCAD,i))^2 + 0.04^2 + (noise_i)^2
    r_i       = (g_TCAD,i - g_meas,i) / tol_i

- **50 mV gate-voltage tolerance.** In steep parts of the curve a 50 mV shift gives |r| = 1.
- **0.04 dec current tolerance** (about 10 %). In the on state a 10 % error gives |r| = 1.
- **Detection limit I_DL** = 10 x the robust noise of the off-state readings at V_G <= [[NOISE_VG_MAX]] V.
  Readings below I_DL carry no shape information. They only require the TCAD current to stay
  below I_DL. noise_i is the measured noise expressed in decades at that current.
- **One term per region.** Each region counts equally, whatever its number of points. A region
  term is the mean r_i^2 over its points.
- **Points below the detection limit** (off region) are added to the subthreshold term but not to
  its point count. They add nothing while the TCAD current stays below I_DL. TCAD current above
  I_DL there costs as much per point as a subthreshold error.

## Operating regions
| Region | Measured current range |
|---|---|
| off | below the detection limit |
| subthreshold | detection limit to [[VTH_LEVEL]] |
| turn_on | [[VTH_LEVEL]] to I_on / 10 |
| on | above I_on / 10 |

For each region the tools also report the mean signed log error, the mean r^2 and the fraction of
points with the same sign.

## Calibration target
Every term within tolerance (each term <= 1). Then J <= 1 as well.

## Gate work-function alignment
The aligned gate work function is a calibrated degree of freedom (an effective value). It enters only
through the flat-band voltage. Changing it by d moves the whole
transfer curve by d along V_G.
- After each simulation, the tools choose d (within +-0.3 V and the approved bounds, the same for
  both V_D) to minimise J, and record the aligned work function.
- The same d moves both curves, so the alignment does not change the V_th shift.
- The alignment needs a defined threshold. If the simulated current stays below 100 nA at either V_D,
  V_th is undefined and no alignment is made (d = 0). The evaluation then reports
  `alignment defined: false`.
- Every evaluation also reports its metrics at the rendered gate work function, before the alignment
  shift. A probe therefore shows a parameter's effect both at a fixed gate work function and after the
  re-alignment.
- The simulated sweep covers [[VG_SIM_START]] to [[VG_SIM_STOP]] V for this purpose. J uses the measured [[VG_START]] to [[VG_STOP]] V
  points only.

## Acceptance rule
A trial is accepted only if all of these hold:
- every Sentaurus run converged
- J decreased by at least 1e-3
- the residuals named as the target of the step became smaller

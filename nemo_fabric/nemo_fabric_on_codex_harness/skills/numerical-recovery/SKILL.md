---
name: numerical-recovery
description: The numerical validity of every evaluation (status, current balance, verified-range flag), the predefined recovery sequence for non-converged runs on the fixed mesh, and why a numerical failure is never evidence about the physics.
---

# Numerical validity and recovery

## Validity of every evaluation
Every evaluation carries a `validity` (shown by `evaluate.py --show` and in every job result):

| Validity | Meaning | What you may do with it |
|---|---|---|
| `VALID` | Converged at every bias and the terminal currents balance | Accept it, cite it as evidence |
| `SOLVER_FAILED` | The recovery sequence was exhausted | Nothing: excluded from decisions. It counts in the run budget |
| `FALSE_CONVERGENCE` | The solver accepted a state whose terminal currents do not balance (\|I_D + I_S + I_G\| > 0.004 dec of I_D above the detection limit) | Nothing: excluded from decisions |

**One fixed mesh.** The whole run uses one mesh, verified before the run. No run of this calibration changes it. An
evaluation whose parameters lie outside the range verified before the run carries `envelope_flag`
(`within_verified_range: false` and the parameters outside). It stays `VALID`. After the run the user re-runs the
final card on the refined mesh (every spacing halved) without refitting. A final card that moves beyond the numerical
criterion there does not count as a verified calibration. Mention flagged cards in the step record.

**Rule.** A failed, falsely converged or mesh-sensitive computation is a numerical finding. It is never evidence that a
model or a parameter cannot explain the measurement. Only a poor fit among `VALID` evaluations says something about the
physics. A fit job reports `numerics` (fraction of converged points, failures next to the best point); if the fit stalled
with failures near its best point, `stall_numerically_limited` is true and no physics-model addition is opened by it.

## Recovery sequence (applied automatically to every non-converged run, in order)

| Level | Change |
|---|---|
| 0 nominal | InitialStep 1e-2, MaxStep 0.05 (of the [[VG_SIM_START]] to [[VG_SIM_STOP]] V sweep), MinStep 1e-9 (drain ramp 1e-8), Iterations 200, Digits 5, ErrRef 1e10 cm^-3 |
| 1 bias_step | level 0 plus InitialStep 1e-3, MaxStep 0.01, MinStep 1e-11 (drain ramp 1e-10) |

- Each attempted level counts as one sdevice run in the budget.
- A run that fails before any solve step is a deck or syntax error; recovery is skipped.
- The mesh and the solver settings are fixed (skill `deck-patching`); only the tools change the solver settings, as
  listed above. The mesh is never changed during the run.
- If level 1 still fails, the evaluation is `SOLVER_FAILED` and excluded from model-update decisions. Inside DFO-LS such a
  point receives a large residual so the solver moves away from it.
- Report repeated failures in the journal (parameter values and bias). Narrowing a bound requires a new bounds proposal
  with a basis; it is not a numerical fix.

## Verification runs
There are none during this run: the mesh is fixed, and `tools/verify.py` reports that. The user checks the final
card on the refined mesh after the run, outside the run budget.

---
name: calibration-loop
description: Step-by-step procedure, tool commands, reviews, step records and turn protocol of the physics-first calibration loop. Read before the first action of a calibration run.
---

# Calibration loop

## Tools (run from the workspace root; each prints JSON)
| Command | What it does |
|---|---|
| `python3 tools/state.py show` | Accepted model, parameters and bounds, deck versions, budget, tolerances, stop conditions, whether a physics-model addition is allowed |
| `python3 tools/state.py history [-n 30]` | Event log |
| `python3 tools/evaluate.py [--deck vNNN] [--set NAME=VALUE ...] [--label TEXT]` | Background job: Sentaurus at both calibration biases, gate work-function alignment, J and its terms, device metrics, V_th shift, region residuals, plot |
| `python3 tools/evaluate.py --show E#####` | View a finished evaluation |
| `python3 tools/ofat.py --params A,B,C [--deck vNNN]` | Background job: probe runs, one per parameter, reported as changes of V_th, SS, I_on, E_lin, the V_th shift and the region residuals. A probe that fails numerically is retried once in the opposite direction |
| `python3 tools/dfols.py --params A,B [--deck vNNN] [--recheck] [--from E#####]` | Background job: DFO-LS on the listed parameters jointly. A call is repeated while it improves J by >= 2 %. The result has a `stall_report` (trend, terms outside tolerance, trade-offs, sensitivity, cost) and your `options`. `--recheck` runs one call from a new probe design (step 0.2). The start is the accepted card, `--from` a stored evaluation, or by default the best point of the latest cancelled or failed fit on that deck |
| `python3 tools/jobs.py show J####` / `list` / `cancel J####` | Job status and result |
| `python3 tools/state.py propose P###` | Validate `proposals/P###.json` before requesting approval |
| `python3 tools/state.py set-bounds P###` | Apply an approved bounds proposal |
| `python3 tools/state.py accept E##### --record notes/steps/<name>.json` | Accept an evaluation with its step record |
| `python3 tools/state.py reject E#####\|J#### --reason "..."` | Record a rejected trial with its physical reading |
| `python3 tools/deck_patch.py --proposal P###` | Apply an approved physics-model addition and submit its smoke test |
| `python3 tools/deck_patch.py --show vNNN` / `--diff vNNN` | Inspect deck versions |
| `python3 tools/kb_search.py "query" [-k 8]` / `--show p####` | Search / read the Sentaurus Device User Guide |
| `python3 tools/diagnose.py --show/--bands/--compare/--observability/--run ...` | Inside the device (skill `device-internals`) |
| `python3 tools/vd_consistency.py [--eval E#####]` | Long-channel drain-bias test, measured and TCAD |
| `python3 tools/extract.py [--eval E#####]` | Same extraction on measured and TCAD curves |
| `python3 tools/identify.py J####` | Identifiability and sensitivity from a probe job |
| `python3 tools/state.py freeze P###` | Freeze the accepted model after an approved final proposal |

The gate work function (WF_gate) is aligned after every simulation and is not one of your search
parameters. It is still a calibrated quantity: when it stops at an approved bound, explain it like
any other bound. All other values are in the parameter's own unit. DFO-LS works in scaled coordinates
inside the approved bounds (log10 for densities).

## Procedure
1. `state show`, read the inputs, evaluate the start model (`evaluate.py --label start`), and end
   with `WAIT_JOB <id>`.
2. When it is done:
   - write a baseline step record
   - `state accept <eval> --record notes/steps/S_baseline.json`
3. **Review G1 (bounds).** Write `proposals/P001.json` (type `bounds`) for every deck parameter.
   For WF_gate the bounds limit the work-function alignment. Each range needs a physical or
   literature basis. Validate (`state propose P001`) and end with `APPROVAL_REQUIRED P001`. After
   approval run `state set-bounds P001`.
4. **Physics reading** (skill `residual-diagnostics`): which metric or region is outside tolerance,
   which device quantity governs it.
5. **Probe** the parameters (`ofat.py`). Decide which parameters control the deficient quantity.
6. **DFO-LS** on those parameters (`dfols.py --params ...`).
7. **Accept or reject.** If `acceptable` is true and the targeted residual shrank, write the step
   record and run `state accept`. Only a `VALID` evaluation can be accepted (skill `numerical-recovery`). Otherwise
   `state reject` with the reason. Accepted values come
   from the tools only: a DFO-LS point, a probe point, or the smoke test of an approved patch. An
   evaluation at values you set yourself is a diagnostic and cannot be accepted.
8. **Physics-model addition.** If `state show` reports `model_addition_allowed: true`, write the
   deficiency (residual-diagnostics, section 4), search for that deficiency only
   (literature-evidence), and submit a `model_patch` proposal.
9. **Stop** when the target is met, or no physically justified step remains, or the budget is used.
   Then submit the `final` proposal, run `state freeze P###` after approval, and end with
   `FINAL_DONE`.

## Step record (`notes/steps/<name>.json`, required by `state accept`)
```json
{
  "targeted": ["SS@[[VD_LO_G]]", "SS@[[VD_HI_G]]"],
  "physical_reading": "<which device quantity is off and why, 1-2 sentences, with the numbers>",
  "action": "<parameter update: which parameters and what they represent; or physics-model addition: model, form, sources>",
  "plausibility": "<fitted values against the stated physical or literature range>",
  "remaining": "<what is still outside tolerance, its physical reading, and the next step>",
  "internal_observation": {"eval": "E#####", "V_D": [[VD_HI_G]], "bias": "threshold", "quantity": "<which inside_the_device quantity>",
                           "observation": "<the relevant summary values, or: the interior diagnostics identify no additional mechanism>",
                           "is_model_prediction": true},
  "at_bound_response": {"limited_terms": "<only when a parameter or the gate alignment sits at a bound: which J terms the bound limits>",
                        "compensation": "<which other parameters moved to make up for it>",
                        "independent_basis_for_extension": "<an independent source for a wider range, or none>"}
}
```
- `targeted` items are the J term names: `V_th@<VD>`, `SS@<VD>`, `I_on@<VD>`, `E_lin@<VD>`,
  `V_th_shift` or `region:<subthreshold|turn_on|on>@<VD>` (`region:off@<VD>` is also accepted).
- The tool fills in the before and after metrics, checks that every targeted residual became
  smaller (mechanism check), flags parameters at a bound, and appends the step to
  `state/STEP_LOG.md`.
- The baseline record has `"targeted": []`.
- **Run rule `band_reading`:** every record, the baseline included, needs `internal_observation`.
  - Cite the relevant values of the `inside_the_device` summary that every evaluation view and job result carries.
  - For a parameter step that is all it takes. When the interior diagnostics identify no additional mechanism, say so.
  - `bias` is `off`, `subthreshold`, `threshold`, `on` or a V_G value. An optional `location` is one of
    `source contact`, `source edge`, `channel`, `drain edge`, `drain contact`, `gate interface`, `back interface`,
    `film`, `not localized`.
  - It is a model prediction, never an evidence item (skill `device-internals`).
- When a parameter or the gate alignment stops at an approved bound, the record also needs `at_bound_response`. A bound
  reached is a finding to explain, not by itself a reason to widen the range.
- **Run rule `step_evidence`** (listed under `run_rules` by `state show`): every record after the
  baseline also needs
  `"evidence": {"manual": [{"chunk": "p####", "quote": "<verbatim>"}], "literature": [{"key": "<note name>", "quote": "<verbatim>"}]}`
  with at least one item. The evidence supports the physical reading or the plausibility statement.
  A manual quote must match the chunk text. A literature key names your note
  `kb/literature/<key>.md`, which must record the DOI and contain the quote. `state accept` checks
  both.

## Proposal format (`proposals/P###.json`, ids in order)
Common fields: `id`, `type` (`bounds` | `model_patch` | `final`), `title`, `rationale`.
- **`bounds`:** `"bounds": {"NAME": {"lo": <n>, "hi": <n>, "basis": "<physical or literature source of the range>"}}`
  - **Run rule `physics_order`** (priority rule in CLAUDE.md): also
    - `"residual"`: the residual this proposal answers (metrics or regions outside tolerance, V_D, numbers)
    - `"evidence"`: at least one manual chunk and one literature note, each with a verbatim quote, found for
      this reading after the residual was read
    - per parameter: `"reading"` (the physical reading for this parameter) and `"direction"` (`down`, `up` or
      `both`, relative to the reference value: the reference card's value, or the start value in a run from the
      start deck). `down` requires `hi` <= the reference value and `up` requires `lo` >= it. `both` requires
      `"both_evidence"` (an evidence block supporting a change either way). The direction is the one your own
      reading and sources state. The gate alignment (WF_gate) takes no direction, only a basis.
- **`model_patch`:**
  - `"deficiency"`: `targeted` (J term names), `measured_vs_TCAD`, `physical_reading`,
    `why_existing_parameters_cannot_fix`, `probe_job` (a finished `ofat` or `dfols` job id),
    `internal_observation` (with `location` and `supports`, the decision it informs) and, under run rule
    `band_reading`, `band_diagram`: `{"eval": "E#####", "V_D": [[VD_HI_G]], "figure": "evals/E#####_bands_vd[[VD_HI_G]].png", "observation":
    "<what the band diagram and the charge distribution of the accepted model show, with numbers>", "interpretation":
    "<your reading, with the alternative readings it does not exclude>", "is_model_prediction": true}`, from
    `python3 tools/diagnose.py --bands E##### --vd <V_D>`.
  - `"patch"`: `base_deck`, `edits` [{`file`, `old`, `new`}], `new_params` [{`name`, `token`, `init`,
    `lo`, `hi`, `scale`, `unit`, `meaning`, `basis`}].
  - `"evidence"`: `manual` (at least 1 chunk with a verbatim quote) and `literature` (at least 2
    papers with DOI, verbatim quote, and `supports`, naming how the source bears on the deficiency).
  - `"expected_effect"`: which targeted residual should change, in which direction, and why
    physically.
  - `"candidates"`: the physics-model classes you considered for this deficiency, at least two,
    ranked (`rank` 1 = the chosen one). Each has `rank`, `model_class` (the Sentaurus model and
    its form), `mechanism` (how it acts on the deficient device quantity), `predicted_effect` (sign
    and rough size on the targeted residual, and side effects in other bias regions), `evidence`
    (manual chunk ids and literature keys), `decision` (`chosen` or `rejected`) and `reason`.
- **`final`:** `"eval"` (the accepted eval id), `"summary"` (model, every J term against its
  tolerance, open issues) and `"insight"`, the device-physics reading of the calibrated model:
  - `descriptors`: one entry per deck parameter except WF_gate, with `name`, `value` (with unit),
    `physical_meaning`, `resolved_residual` (the residual it fixed, with the step id),
    `literature_comparison` (the value against the cited range), `constraint_class`
    (`well constrained`, `weakly constrained` or `not constrained`) and `constraint_evidence`
    (probe and DFO-LS evidence: the metric change per probe step, collinearity with another
    parameter, or a value at its bound).
  - Under run rule `band_reading` also `interior_check` and `gate_alignment`.
    - `interior_check`: `band_diagrams` (the figures `evals/<accepted eval>_bands_vd[[VD_LO_G]].png` and `_vd[[VD_HI_G]].png` from
      `diagnose.py --bands`), `film_vertical`, `contacts_lateral`, `charge_balance` and `contact_voltage_drop`, each
      with numbers and labelled as model predictions.
    - `gate_alignment`: `phi_G_aligned_eV`, `phi_G_minus_chi_eV`, `dV_trapped_estimate_V`, `dV_donor_estimate_V`,
      `phi_G_charge_compensated_estimate_eV` (copy them from `inside_the_device.gate_alignment_estimates` of the
      accepted evaluation), `interpretation` and `constraint_class`. The interpretation says what these approximate
      estimates indicate compared with the gate metal, and what these curves cannot separate. They are not an
      extracted work function.
  - `device_conclusions`: what the calibrated model says about this device (contacts, charge,
    sub-gap states, transport), each tied to the residual that supports it.
  - `limits`: what these two transfer curves cannot separate. State that the descriptors are
    calibration-effective, not microscopic extractions. List the model families and parameter bounds that were
    tested. A stall shows that no further improvement was found within them; it does not show that no other
    physics can act on the residual.
  - `follow_up`: measurements or TCAD studies that would separate the weakly constrained
    descriptors or test the conclusions.
  - **Run rule `reference_comparison`** (a run that starts from a reference model): also
    `reference_comparison`, one row per deck parameter, the aligned gate work function included,
    with `name`, `reference_value`, `final_value` (as in the accepted card), `change`,
    `physical_meaning` (of the change), `resolved_residual` (which difference between the reference
    model and the new data it resolved, with the step id), `literature_comparison`, `evidence`
    (same format as the step record) and `constraint_class`. Then `process_conclusions`: what the
    changes suggest about the difference between the two devices, in hedged wording, with only the
    changes that the data constrain.

Also write a short human-readable `proposals/P###.md` with the same content for the user.

## Journal
Append to `notes/journal.md` before ending each turn: observation (numbers), physical reading,
decision, reason, next step.

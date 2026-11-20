# Device setup

Install the environment for your chosen implementation before preparing the manual index or creating a workspace.
Run Python commands from that implementation's directory with its own environment. Measurement, device and run paths
refer to the package root, which contains the three implementations.

## Using it on your own device

The device is defined in one folder under `devices/` at the package root, which all three implementations read.
`devices/example/` holds the manuscript's bottom-gate In-W-O transistor. The code does not change for another device.
The repository contains no measured data or parameter values from the authors' calibrations.

1. Copy `devices/example/` to `devices/<your device>/` and set `TCADCAL_DEVICE` to the copy before creating a
   workspace.
2. Edit `device.toml`. Each entry is commented.
   - `[devices]`, `[geometries]`, `[splits]`: your devices with their channel length and gate overlap, the two
     calibration curves and the test curves without retuning. `[splits_recal]` holds the curves of a second device
     recalibrated from a reference model (`--recalibrate`, see below).
   - `[sweep]`, `[metrics]`: the measured gate sweep and the current levels of the metrics.
   - `[structure]`, `[materials]`, `[regions]`, `[deck_variables]`: the dimensions, permittivities, materials and
     region names that every rendered deck and every mesh read-back is checked against.
   - `[mesh]`: the mesh refinement of your structure deck.
   - `[text]`: the device wording that fills the `[[NAME]]` places of the agent instructions in `templates/` and
     `skills/`.
3. Save each I_D versus V_G sweep as a CSV file with columns `VG_V`, `VD_V` and `ID_drain_A` at
   `experiment/raw/<device>/<device>_VD_p<VD>V.csv`, with `p` for the decimal point. For the example calibration
   device these are `experiment/raw/ref_L1/ref_L1_VD_p0p05V.csv` and `experiment/raw/ref_L1/ref_L1_VD_p1V.csv`.
   Put the forward branch first. A reverse branch after it is ignored.
4. Fill `DEVICE_INFO.md` (and `DEVICE_INFO_recal.md` for a recalibration) with your device's structure, process
   and measurement facts. The agent receives this file.
5. Replace the files in `deck0/`, keeping their names and the `@...@` parameter tokens. Name the structure defines
   of your deck in `[deck_variables]`.
6. Optional, in `lib/tcadcal/config.py` of the implementation: start-deck parameter cards checked on your production
   mesh (`V000_VERIFIED_SEED`). Without them, every start-deck evaluation is flagged as outside the numerically
   verified range. Add identifiers for your own publications on the device to `WEB_BLOCK_TERMS`. This prevents the
   agent from copying a published calibration from the web.

J computes its subthreshold-swing windows from each measured curve.

## Not included

- The manuscript's measured data and calibration results.
- Synopsys Sentaurus, its license, manual and material files. Put the support files of `[materials]` in
  `device.toml` from your own installation in the folder named by `TCAD_SUPPORT_DIR`. For the example, `IWO.par` is a
  semiconductor material parameter file (the example uses the silicon file) with relative permittivity set to 9. The
  band gap, electron affinity, density-of-states masses and mobility of IWO are set in
  `devices/example/deck0/sdevice_iwo_process.par`. For `datexcodes.txt`, use the installation's material list with
  an entry for the channel semiconductor (`IWO` in the example).
- The manual index. After installing the environment, run this from your chosen implementation's directory.
  You also need `pdftotext` from Poppler:
  `env/bin/python -c "import sys; sys.path.insert(0, 'lib'); from tcadcal import kb, config as C; from pathlib import Path; kb.build(C.MANUAL_PDF, Path('kb_cache/manual'))"`
- Run records and API keys.

## Platform and environment

- Linux x86-64. The workflow was tested on an 8-core workstation with two parallel Sentaurus runs, four threads each.
- Synopsys Sentaurus W-2024.09-SP1 with a valid license.
- For `claude_code/`: Python 3.11 (`requirements.txt`) and Claude Code with access to a Claude model.
- For `nemo_fabric/nemo_fabric_on_claude_harness/` and `nemo_fabric/nemo_fabric_on_codex_harness/`: Python 3.12
  (each directory's `fabric/requirements.lock`), an NVIDIA API key for the hosted Nemotron endpoint and an optional
  Exa key for web search.

Environment variables:

| Variable | Purpose |
|---|---|
| `SENTAURUS_ROOT` | Sentaurus installation (its `bin/` holds `sde` and `sdevice`) |
| `SNPSLMD_LICENSE_FILE`, `LM_LICENSE_FILE` | Your Synopsys license server |
| `SDEVICE_MANUAL_PDF` | Path to the Sentaurus Device user guide PDF |
| `TCADCAL_DEVICE` | Device folder (default: `devices/example` at the package root) |
| `TCAD_SUPPORT_DIR` | Folder with the support files of the device, `IWO.par` and `datexcodes.txt` for the example (see Not included) |
| `CLAUDE_BIN` | Claude Code executable (default: `claude` on the PATH) |
| `FABRIC_TEST_CLAUDE_CLI_PATH` | `nemo_fabric_on_claude_harness/` only: Claude Code executable for the adapter (default: the bundled 2.1.299, the reported calibrations used 2.1.318) |
| `TCADCAL_SECRETS_DIR` | NeMo Fabric implementations only: folder with one key per file (`nvidia_api_key`, `litellm_master_key`, optional `exa_api_key`), mode 600 |
| `TCADCAL_THREADS`, `TCADCAL_SLOTS`, `TCADCAL_GLOBAL_SLOTS`, `TCADCAL_GLOBAL_SLOT_DIR`, `TCADCAL_SNAPSHOT_WORKERS` | Optional: Sentaurus threads per run, parallel runs per workspace and per machine, and snapshot workers (defaults in `lib/tcadcal/config.py`) |

## Calibration defaults and recalibration

The default objective is the manuscript's 14-term J (`--objective J_objective`). After each simulation, the tools
set the gate offset by minimizing J (`--alignment full_objective`, also the default). `--cap` sets the
Sentaurus budget. Runs are written to the package root's runs directory: `../runs/<run-id>/` from the Claude Code
implementation or two levels up from either NeMo Fabric implementation.

To recalibrate a second device, first run `postrun.py select` on the reference run. This example recalibrates the
second device of `[splits_recal]` in `device.toml` (the 4%-W device in the example) while holding the reference model's source/drain work function fixed:

```
env/bin/python make_workspace.py --run-id RUN-4W --cap 400 --recalibrate --start-from RUN-01 --fixed WF_sd
```

## Code layout

Each implementation contains:

- `lib/tcadcal/`: objective J, gate offset, DFO-LS fitting, parameter sensitivity tests, deck patching and checks, numerical recovery, verification.
- `hooks/`: tool guard and tool-call logging for the agent session.
- `skills/`, `templates/`: the agent instructions and skills, with `[[NAME]]` places for the device wording.
- `make_workspace.py`: builds the agent's working directory.
- `orchestrate.py`, `fabric_orchestrate.py` or `codex_orchestrate.py`: the entry point of the chosen implementation.
- `approve.py`: records the user's decision on an agent proposal.
- `postrun.py`: selects the reported model after the agent run, checks its numerics and runs transfer tests
  without retuning.
- `tests/`: checks on synthetic curves.

At the package root, `devices/example/` holds the example device: `device.toml`, `DEVICE_INFO.md`,
`DEVICE_INFO_recal.md` and the start deck `deck0/` (the structure in Table I of the manuscript, basic device models and
literature values, without calibrated parameters).

Choose an implementation and use the environment, configuration, templates and entry point in that directory.
All three share the package-level `devices/`, `experiment/` and `runs/` locations, so use different run IDs.

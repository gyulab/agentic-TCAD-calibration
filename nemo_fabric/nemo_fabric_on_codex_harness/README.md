# NeMo Fabric with Codex

This version combines an open harness, [Codex 0.154.0](https://github.com/openai/codex), with an open LLM,
[NVIDIA Nemotron 3.5 Super VL (120B-A12B)](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b/modelcard).
Codex is open source under Apache-2.0, and the model weights are available to download. NeMo Fabric 0.6.1 sends
model requests to NVIDIA's hosted endpoint through a local LiteLLM gateway.

You can run this version without a Claude Code installation or Claude account.
The agent recommends changes. You review each proposal and record your decision with `approve.py`.

## 1. Install and check the code

From the package root, where the main `README.md` is:

```bash
cd nemo_fabric/nemo_fabric_on_codex_harness
python3.12 -m venv env
env/bin/python -m pip install -r fabric/requirements.lock
env/bin/python -m pip check
env/bin/python -m pytest -q tests
env/bin/python codex_orchestrate.py --help
```

Use Linux x86-64 and Python 3.12. Name the environment `env/` and keep it in this directory, where hooks and the
gateway expect it. The commands call the environment's Python directly, so you do not need to activate it.
The lock file installs the Codex adapter, CLI, numerical libraries and pytest.

The tests create temporary workspaces from synthetic curves. They check workspace creation, Codex configuration,
tools, guards and the wait for your review. You can run them without an API key, measured data, a manual or Sentaurus.

## 2. Prepare your device inputs

Follow [Using it on your own device](../../README.md#using-it-on-your-own-device) and edit the files in this
directory. Supply your own measurements, simulator files, manual and API keys.

- Copy `../../devices/example/` to `../../devices/<your device>/`, edit `device.toml`, `DEVICE_INFO.md` and
  `deck0/`, and set `TCADCAL_DEVICE` to the copy.
- Put the raw curves at `../../experiment/raw/<device>/<device>_VD_p<VD>V.csv`. For the example calibration device
  the paths are `../../experiment/raw/ref_L1/ref_L1_VD_p0p05V.csv` and
  `../../experiment/raw/ref_L1/ref_L1_VD_p1V.csv`. Each CSV needs `VG_V`, `VD_V`, `ID_drain_A` columns,
  with the increasing gate-voltage branch first. The example forward branch has 61 points from -1 to 2 V in 0.05 V steps.
- Provide the support files of `device.toml` (`[materials]`) from your own Sentaurus installation. For the example,
  `IWO.par` must set both `Epsilon` and `Epsilon_aniso` to 9, and `datexcodes.txt` needs an `IWO` entry.

In the terminal where you will run the orchestrator, set your paths and license server:

```bash
export SENTAURUS_ROOT=/path/to/sentaurus
export SNPSLMD_LICENSE_FILE=port@your-license-server
export SDEVICE_MANUAL_PDF=/path/to/sdevice_ug.pdf
export TCAD_SUPPORT_DIR=/path/to/material-support
```

Export `LM_LICENSE_FILE` too if your site uses it. Install `pdftotext` from Poppler, then build the manual index
from inside `nemo_fabric_on_codex_harness/` with this environment:

```bash
env/bin/python -c "import sys; sys.path.insert(0, 'lib'); from pathlib import Path; from tcadcal import kb, config as C; print(kb.build(C.MANUAL_PDF, Path('kb_cache/manual')))"
```

The index consists of `kb_cache/manual/chunks.jsonl` and `meta.json`. Have these files, the calibration curves and
the two material-support files ready before creating a workspace. Workspace creation does not start Sentaurus.

## 3. Configure the gateway

Choose a private directory for the credentials:

```bash
export TCADCAL_SECRETS_DIR="$HOME/.config/tcadcal"
install -d -m 700 "$TCADCAL_SECRETS_DIR"
```

Create one file per credential in your editor:

| Filename | Value |
|---|---|
| `nvidia_api_key` | Your NVIDIA hosted-endpoint API key |
| `litellm_master_key` | A gateway password you choose, beginning with `sk-` |
| `exa_api_key` | Optional Exa web-search key |

```bash
chmod 600 "$TCADCAL_SECRETS_DIR/nvidia_api_key" "$TCADCAL_SECRETS_DIR/litellm_master_key"
# If present: chmod 600 "$TCADCAL_SECRETS_DIR/exa_api_key"
fabric/gateway.sh 4000
```

Keep this terminal open. The gateway listens on `127.0.0.1:4000`, writes to `tmp/gateway_4000.log` and calls the
model in `fabric/litellm.yaml`. Use the same `TCADCAL_SECRETS_DIR` for the gateway and orchestrator.

## 4. Create and start a run

Open a second terminal in `nemo_fabric_on_codex_harness/`. Set the simulator and credentials environment variables
from steps 2 and 3, then run:

```bash
env/bin/python make_workspace.py --run-id RUN-CX --cap 800
env/bin/python codex_orchestrate.py --run-id RUN-CX
```

The defaults are the Fabric backend, `nemotron-3.5-super-vl`, objective J (`J_objective`) and full-objective gate
alignment. `--cap` limits the total number of `sdevice` attempts. An evaluation normally uses two attempts, one
for each calibration drain bias; numerical recovery can use more. Build each workspace once and choose a unique
run ID, since all implementations write to the package's `../../runs/` directory.

The workspace is `../../runs/RUN-CX/workspace/`. Workspace creation writes `AGENTS.md` and includes it in the
manifest. This file contains the common calibration instructions and the Codex tool instructions. `CLAUDE.md`
is the shared instruction source, and `.claude/skills/` holds the skills. These files do not start Claude Code.
Codex reads files through shell commands, writes proposals and notes with `ws_files`, and uses Exa for web access.
The guard limits shell access to reading files and running the calibration tools.

When a tool submits a background job, the agent ends its turn with `WAIT_JOB J####`. The orchestrator waits for that
job and resumes the same conversation. Codex runs without its OS sandbox so that these jobs survive a tool call;
`hooks/codex_guard.py` checks tool calls and prevents the agent from writing approvals or changing harness files.

## 5. Review a proposal

When the agent emits `APPROVAL_REQUIRED P001`, the orchestrator waits and records the pending request in
`../../runs/RUN-CX/orchestrator/gate_pending.json`. Open a third terminal in `nemo_fabric_on_codex_harness/` to read the proposal:

```bash
env/bin/python approve.py --run-id RUN-CX --show P001
```

Read the recommendation and validation output. Copy the proposal SHA and record your decision with one of these
commands, replacing `SHA_FROM_SHOW` and the comment:

```bash
env/bin/python approve.py --run-id RUN-CX P001 approve "Your review comment" --reviewed-sha SHA_FROM_SHOW
# Or:
env/bin/python approve.py --run-id RUN-CX P001 reject "Your review comment" --reviewed-sha SHA_FROM_SHOW
```

The record lists you as the approver (`author`) and requires a matching SHA. A missing or stale SHA is refused.
Displaying a proposal leaves the decision pending. The run waits until you record a decision.
The agent cannot run `approve.py`.

## 6. Inspect and finish

Read the turn log at `../../runs/RUN-CX/orchestrator/turns.jsonl` and the full transcripts in its `turns/` directory.
Look in `../../runs/RUN-CX/workspace/` for evaluations, plots, proposals and decisions. Once the final proposal
has been approved and the agent has frozen the state, run:

```bash
env/bin/python postrun.py select --run-id RUN-CX
env/bin/python postrun.py check --run-id RUN-CX
env/bin/python postrun.py transfer --run-id RUN-CX
```

`select` reads existing results. `check` and `transfer` start additional Sentaurus runs and need the simulator
environment and the corresponding measured curves. Their output goes to `../../runs/RUN-CX/final/`.

## If a step fails

- If workspace inputs are missing, prepare the listed files and rerun `make_workspace.py`. The builder checks for
  missing inputs before creating the workspace.
- If the gateway key is missing, set `TCADCAL_SECRETS_DIR` in the orchestrator terminal and provide `litellm_master_key`.
- If the gateway is unavailable, inspect `tmp/gateway_4000.log` and check the NVIDIA key and endpoint in `fabric/litellm.yaml`.
- For simulator or license errors, check `SENTAURUS_ROOT` and the license variables in the orchestrator terminal.
- If the run is waiting for review, follow step 5. Only you can record the decision.

# Claude Code with Claude Opus 5.5

This implementation produced the main calibration in the manuscript. The agent runs in Claude Code
(CLI 2.1.318) with Claude Opus 5.5. You review every proposal before the agent applies it.

## Setup and run

Use Linux x86-64, Python 3.11 and Claude Code with access to a Claude model. The reported calibrations used
Claude Code 2.1.318 (`npm install -g @anthropic-ai/claude-code@2.1.318`). Set `CLAUDE_BIN` to it if another
`claude` comes first on your PATH. Keep the environment in this directory under `env/`, where the hooks expect it.
The tests use synthetic curves and do not require Sentaurus.

Before creating a workspace, follow the [device setup guide](../docs/device-setup.md) to prepare your measurements,
configuration, start deck, material-support files and manual index. Calibration needs your own Sentaurus
W-2024.09-SP1 installation and license.

Run the following commands from the package root. The orchestrator waits at proposal reviews: leave it running,
then use the approval commands in a second terminal in this directory. Copy the SHA printed by the proposal view
into the decision command. You can reject a proposal by changing the decision argument to reject.
Run the post-run commands after the agent finishes with an approved final proposal and frozen state.

```
cd claude_code
python3.11 -m venv env && env/bin/pip install -r ../requirements.txt
env/bin/python -m pytest -q tests                     # synthetic checks, no Sentaurus
env/bin/python make_workspace.py --run-id RUN-01 --cap 800
env/bin/python orchestrate.py --run-id RUN-01         # the agent works turn by turn
env/bin/python approve.py --run-id RUN-01 --show P001 # read an agent proposal
env/bin/python approve.py --run-id RUN-01 P001 approve "comment" --reviewed-sha SHA_FROM_SHOW
env/bin/python postrun.py select --run-id RUN-01      # the reported model, by the fixed selection rule
env/bin/python postrun.py check --run-id RUN-01       # halved mesh and gate offset in the deck (4 Sentaurus runs)
env/bin/python postrun.py transfer --run-id RUN-01    # tests without retuning at the other biases and devices
```

The last two commands start additional Sentaurus runs. Transfer tests also need the measurements for the test
biases and devices. The [calibration defaults and recalibration instructions](../docs/device-setup.md#calibration-defaults-and-recalibration)
explain objective J, gate alignment, the simulation budget and how to recalibrate a second device.

## Run files

Runs are written to the package root's `runs/` directory, shared with the NeMo Fabric implementations. Choose unique
run IDs. See the [output file guide](../README.md#outputs) for evaluation, proposal and final-result paths.

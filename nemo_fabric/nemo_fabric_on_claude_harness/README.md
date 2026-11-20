# NeMo Fabric with Claude Code

This version uses NeMo Fabric 0.6.1 with the open LLM
[NVIDIA Nemotron 3.5 Super VL (120B-A12B)](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b/modelcard).
Claude Code is the agent harness. A local LiteLLM gateway forwards model requests to NVIDIA's hosted endpoint.

For an open harness with the same open LLM, use [`nemo_fabric_on_codex_harness/`](../nemo_fabric_on_codex_harness/).

From the package root:

```bash
cd nemo_fabric/nemo_fabric_on_claude_harness
python3.12 -m venv env
env/bin/python -m pip install -r fabric/requirements.lock
env/bin/python -m pip check
env/bin/python -m pytest -q tests
```

Prepare the measurements, device configuration, deck, Sentaurus support files, manual index and API-key files
described in [the main README](../../README.md). Run the setup commands from this directory with `env/bin/python`.
Set `SENTAURUS_ROOT`, `SNPSLMD_LICENSE_FILE` (and `LM_LICENSE_FILE` if used), `SDEVICE_MANUAL_PDF`,
`TCAD_SUPPORT_DIR` and `TCADCAL_SECRETS_DIR` to your own values.

The lock file installs the Claude adapter, which bundles Claude Code 2.1.299. The reported calibrations used
Claude Code 2.1.318. To run that version, install it and point the adapter to it:

```bash
npm install -g @anthropic-ai/claude-code@2.1.318
export FABRIC_TEST_CLAUDE_CLI_PATH="$(npm prefix -g)/bin/claude"
```

Start `fabric/gateway.sh 4000` in one terminal. In another terminal in this directory, with the same environment:

```bash
env/bin/python make_workspace.py --run-id RUN-NF --cap 800 --agent-backend fabric --agent-model nemotron-3.5-super-vl
env/bin/python fabric_orchestrate.py --run-id RUN-NF
```

When the agent submits a proposal, display it with `approve.py --run-id RUN-NF --show P001` in this environment.
Review the proposal and its SHA, then record your decision with `approve.py --run-id RUN-NF P001 approve "comment"
--reviewed-sha SHA_FROM_SHOW` (or `reject`). The agent recommends changes; you decide whether to accept them.
The run waits for your decision. After it finishes, use `postrun.py` as described in the main README.

Runs are written to `../../runs/`. Choose run IDs distinct from those used by the other implementations.

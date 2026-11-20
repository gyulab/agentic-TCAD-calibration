# NeMo Fabric implementations

Both versions use [NVIDIA Nemotron 3.5 Super VL (120B-A12B)](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b/modelcard),
an open LLM with downloadable weights. NeMo Fabric 0.6.1 sends requests to NVIDIA's hosted endpoint through a local
LiteLLM gateway. Choose the agent harness:

- [Codex harness](nemo_fabric_on_codex_harness/README.md): open harness + open LLM.
- [Claude Code harness](nemo_fabric_on_claude_harness/README.md): Claude Code with the same open LLM.

Install the environment in the directory you choose. Both versions read measurements from the package root's
`experiment/` directory and write results to `runs/`. See the [main README](../README.md) for the device inputs.

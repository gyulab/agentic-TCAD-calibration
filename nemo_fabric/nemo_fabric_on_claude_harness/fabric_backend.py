from __future__ import annotations

import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FABRIC_ENV_BIN = HERE / "env" / "bin"

EXA_URL = "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa"
EXA_SERVER = "exa"
EXA_TOOLS = (f"mcp__{EXA_SERVER}__web_search_exa", f"mcp__{EXA_SERVER}__web_fetch_exa")
# The built-in tools of the CLI path (orchestrate.TOOLS) without WebSearch and WebFetch.
BUILTIN_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill", "TodoWrite")
BLOCKED_TOOLS = ("WebSearch", "WebFetch", "Agent", "Task", "NotebookEdit")
APPEND_LINE = "Follow CLAUDE.md and the calibration-loop skill."


def agent_env(ws: Path, root: Path, *, context_tokens: int | None) -> dict[str, str]:
    """orchestrate.clean_env plus the Claude Code switches needed for a non-Claude model behind a gateway."""
    env = {"PATH": f"{HERE / 'env' / 'bin'}:/usr/local/bin:/usr/bin:/bin:{FABRIC_ENV_BIN}",
           "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "TCADCAL_WS": str(ws), "BASH_DEFAULT_TIMEOUT_MS": "300000",
           "BASH_MAX_TIMEOUT_MS": "600000", "DISABLE_AUTOUPDATER": "1", "TMPDIR": str(root / "tmp"),
           # not a Claude model: send no effort level, load MCP tools up front, no telemetry or update traffic
           "CLAUDE_CODE_EFFORT_LEVEL": "auto", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "OTEL_SDK_DISABLED": "true",
           "API_TIMEOUT_MS": "900000"}
    env.update({k: os.environ[k] for k in ("SENTAURUS_ROOT", "SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE")
                if k in os.environ})
    if context_tokens:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(int(context_tokens))
    return env


def build_config(ws: Path, odir: Path, *, run_id: str, model: str, base_url: str, api_key_env: str,
                 provider: str = "litellm", context_tokens: int | None = None, turn_timeout: float = 7200.0,
                 max_turns: int = 100000, exa_url: str = EXA_URL, exa_key_env: str | None = "EXA_API_KEY",
                 relay: bool = True, root: Path | None = None) -> Any:
    from nemo_fabric import (EnvironmentConfig, FabricConfig, HarnessConfig, InstructionConfig, InstructionsConfig,
                             MetadataConfig, ModelConfig, RuntimeConfig, ToolsConfig)
    root = root or HERE.parents[1]
    cfg = FabricConfig(
        metadata=MetadataConfig(name=f"tcadcal-{run_id}".lower()),
        harness=HarnessConfig(adapter_id="nvidia.fabric.claude", resolution="preinstalled",
                              settings={"permission_mode": "dontAsk", "setting_sources": ["project"]}),
        models={"default": ModelConfig(provider=provider, model=model, base_url=base_url, api_key_env=api_key_env)},
        runtime=RuntimeConfig(artifacts=str(odir / "fabric_artifacts"), timeout_seconds=turn_timeout,
                              max_turns=max_turns),
        environment=EnvironmentConfig(provider="local", workspace=str(ws),
                                      env=agent_env(ws, root, context_tokens=context_tokens)),
        instructions=InstructionsConfig(system=InstructionConfig(content=APPEND_LINE, mode="append")),
        tools=ToolsConfig(enabled=list(BUILTIN_TOOLS) + list(EXA_TOOLS), blocked=list(BLOCKED_TOOLS)),
    )
    headers = {"x-api-key": "${" + exa_key_env + "}"} if exa_key_env and os.environ.get(exa_key_env) else None
    cfg.add_mcp_server(EXA_SERVER, transport="streamable-http", url=exa_url, exposure="harness_native",
                       custom_headers=headers)
    if relay:
        rdir = odir / "relay"
        cfg.enable_relay(project=f"tcadcal-{run_id}".lower(), output_dir=str(rdir), observability={
            "version": 4,
            "atof": {"enabled": True, "sinks": [{"type": "file", "output_directory": str(rdir / "atof"),
                                                 "filename": "events.jsonl", "mode": "append"}]},
            "atif": {"enabled": True, "output_directory": str(rdir / "atif")},
            "opentelemetry": {"enabled": False}})
    return cfg


def result_record(res: Any) -> dict[str, Any]:
    """Flatten a Fabric RunResult into the fields orchestrate.run_turn records, plus the Fabric ids."""
    from collections.abc import Mapping
    out = dict(res.output) if isinstance(res.output, Mapping) else {}
    err = res.error or out.get("error")   # the adapter puts a failed Claude result's error (with api_error_status) here
    return {"status": str(res.status), "runtime_id": res.runtime_id, "invocation_id": res.invocation_id,
            "session_id": out.get("session_id"), "response": out.get("response"), "num_turns": out.get("num_turns"),
            "usage": out.get("usage"), "model_usage": out.get("model_usage"), "cost_usd": out.get("cost_usd"),
            "duration_ms": out.get("duration_ms"), "subtype": out.get("subtype"), "failed": out.get("failed"),
            "error": (err if isinstance(err, dict) else (getattr(err, "__dict__", None) or str(err))) if err else None,
            "events": out.get("events") or []}


SECRETS = Path(os.environ.get("TCADCAL_SECRETS_DIR", str(Path.home() / ".config" / "tcadcal")))
SECRET_ENV = {"FABRIC_GATEWAY_KEY": "litellm_master_key", "EXA_API_KEY": "exa_api_key"}


def load_secrets() -> dict[str, bool]:
    """Read the gateway and Exa keys into this process's environment (never printed). Returns which were found."""
    found = {}
    for env, name in SECRET_ENV.items():
        f = SECRETS / name
        found[env] = f.is_file() and f.stat().st_size > 0
        if found[env]:
            os.environ[env] = "".join(f.read_text().split())
    return found

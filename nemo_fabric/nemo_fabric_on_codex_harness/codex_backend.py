"""NeMo Fabric backend with the open-source Codex harness (adapter nvidia.fabric.codex, Codex 0.154.0, Apache-2.0).

The sibling nemo_fabric_on_claude_harness implementation runs the agent in Claude Code. This backend runs the same workspace, instructions, skills and tools,
and the same guard decisions, in Codex, so that the workflow runs on open-source software only: NeMo Fabric, Codex,
LiteLLM and an open-weight model. What differs from the Claude Code harness:

- Codex reads AGENTS.md: CLAUDE.md plus a short section that names the Codex equivalents of the Claude Code tools
  (TOOL_NOTE).
- Codex has no Read, Glob, Grep, Write or Edit tools behind the gateway. Reads are read-only shell commands, writes go
  through the ws_files MCP server (hooks/ws_files_mcp.py). hooks/codex_guard.py decides both with the rules of
  hooks/guard.py.
- Codex's built-in web search, sub-agents, apps, browser, computer use, image generation and plugins are switched off.
  Web access is the Exa MCP tools, as in the Claude Code backend.
- The shell runs without Codex's OS sandbox. On Linux that sandbox gives every command its own process namespace, so the
  Sentaurus job runner that the agent tools start in the background would end with the tool command. The guard decides
  every call, as in the Claude Code harness.
- The model entry comes from Codex's own catalog (entry gpt-5.4: standard tools) with the model name, the context window,
  image input and no apply_patch tool. The catalog and the context window are written to the config.toml of the
  runtime's Codex home, which Codex reads at start.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FABRIC_ENV_BIN = HERE / "env" / "bin"
PY = HERE / "env" / "bin" / "python"                 # the environment of this folder (hooks, ws_files server)
EXA_URL = "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa"
DEVELOPER_LINE = "Follow AGENTS.md and the calibration-loop skill."
CATALOG_BASE = "gpt-5.4"
TOOL_NOTE = """
## Tools in this harness
This session runs in Codex. The text above names the tools of the Claude Code harness. Use these equivalents:
- Read, Glob and Grep: the shell commands cat, head, tail, sed -n, nl, wc, ls, find and grep (or rg) on files inside
  this directory. Only files inside this directory are available.
- Write and Edit: the ws_files tools write_file and edit_file. They write only in proposals/, notes/ and kb/literature/.
  Nothing may be written with the shell.
- Bash: `python3 tools/<tool>.py <args>` as above. The output of these commands may be piped into head, tail, grep,
  sed -n, wc, sort, uniq, cut or nl, commands may be joined with ; && ||, and 2>&1 is fine.
- Skill: the skills of this session (calibration-loop and the others in .claude/skills/). Read a skill's SKILL.md
  when you need it.
- Jobs: when a tool submits a job and returns a job id, end the turn immediately with WAIT_JOB <id>.
  The orchestrator waits and resumes this conversation with the completed job. Do not poll jobs.py.
- Reviews: prepare a recommendation and end with APPROVAL_REQUIRED <id>. Only the author decides.
  Never run approve.py or write an approval.
- Images: view_image for plot files.
- WebSearch and WebFetch: the exa tools web_search_exa and web_fetch_exa.
"""
SENTAURUS_ENV = ("SENTAURUS_ROOT", "SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE")
SYSTEM_SKILLS = ("imagegen", "openai-docs", "plugin-creator", "review-agent", "skill-creator", "skill-installer")
DISABLED_FEATURES = ("multi_agent", "apps", "browser_use", "browser_use_external", "computer_use", "image_generation",
                     "plugins", "tool_suggest", "goals", "sleep_tool", "skill_mcp_dependency_install",
                     "workspace_dependencies", "in_app_browser", "remote_plugin", "plugin_sharing")


def codex_bin() -> Path:
    spec = importlib.util.find_spec("codex_cli_bin")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("the Codex CLI is not installed (env/bin/python -m pip install -r fabric/requirements.lock)")
    return Path(list(spec.submodule_search_locations)[0]) / "bin" / "codex"


def write_catalog(path: Path, model: str, *, context_tokens: int, text_only: bool) -> dict:
    """Codex model catalog with one entry for the gateway model, derived from Codex's own gpt-5.4 entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CODEX_HOME": str(path.parent)}
    p = subprocess.run([str(codex_bin()), "debug", "models"], capture_output=True, text=True, env=env, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"codex debug models failed: {p.stderr.strip()[-600:]}")
    src = json.loads(p.stdout)
    models = src["models"] if isinstance(src, dict) else src
    m = json.loads(json.dumps(next(x for x in models if x.get("slug") == CATALOG_BASE)))
    m.update({"slug": model, "display_name": model, "description": f"{model} through the local gateway",
              "apply_patch_tool_type": None, "supports_search_tool": False, "context_window": int(context_tokens),
              "max_context_window": int(context_tokens), "input_modalities": ["text"] if text_only else ["text", "image"],
              "upgrade": None, "priority": 1})
    # the entry's base instructions name the OpenAI model; the agent runs on another model
    m["base_instructions"] = re.sub(r"based on GPT-5(?:\.\d+)?", "running on the configured model", m["base_instructions"])
    left = re.findall(r"GPT-?\d[\w.]*", m["base_instructions"])
    if left:
        raise RuntimeError(f"base instructions still name an OpenAI model: {sorted(set(left))}")
    path.write_text(json.dumps({"models": [m]} if isinstance(src, dict) else [m], indent=1))
    return {"catalog": str(path), "base": CATALOG_BASE}


def codex_home(odir: Path) -> Path:
    """CODEX_HOME that the Fabric Codex adapter uses for a custom model provider."""
    return odir / "fabric_artifacts" / ".fabric" / "codex" / "custom-provider-home"


def write_process_config(odir: Path, catalog: str, context_tokens: int) -> Path:
    """config.toml of the runtime's CODEX_HOME. Codex reads the model catalog and the context window when its process
    starts; the adapter's config_overrides reach the thread request only."""
    home = codex_home(odir)
    home.mkdir(parents=True, exist_ok=True)
    p = home / "config.toml"
    kept = [ln for ln in (p.read_text().splitlines() if p.exists() else [])
            if not ln.startswith(("model_catalog_json", "model_context_window"))]
    p.write_text(f"model_catalog_json = {json.dumps(str(catalog))}\nmodel_context_window = {int(context_tokens)}\n"
                 + ("\n".join(kept) + "\n" if kept else ""))
    return p


def agent_env(ws: Path, root: Path, home: Path) -> dict[str, str]:
    """HOME is a private empty directory: Codex reads user-level skills and settings from HOME, and the agent sees only
    the workspace's instructions and skills."""
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": f"{HERE / 'env' / 'bin'}:/usr/local/bin:/usr/bin:/bin", "TCADCAL_WS": str(ws),
           "TMPDIR": str(root / "tmp"), "HOME": str(home)}
    # the Sentaurus jobs that the agent tools start read the installation and the license from the environment
    env.update({k: os.environ[k] for k in SENTAURUS_ENV if k in os.environ})
    return env


def hooks_config() -> dict[str, Any]:
    def cmd(script: str) -> list[dict]:
        return [{"matcher": "*", "hooks": [{"type": "command", "command": f"{PY} {HERE / 'hooks' / script}",
                                            "timeout": 60}]}]
    return {"PreToolUse": cmd("codex_guard.py"), "PostToolUse": cmd("codex_log_tool.py")}


def build_config(ws: Path, odir: Path, *, run_id: str, model: str, base_url: str, api_key_env: str,
                 provider: str = "litellm", context_tokens: int = 500000, turn_timeout: float = 7200.0,
                 text_only: bool = False, exa_url: str = EXA_URL, exa_key_env: str | None = "EXA_API_KEY",
                 root: Path | None = None) -> Any:
    """FabricConfig for one Codex runtime (base_url: the gateway's /v1 path, Responses API)."""
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    from nemo_fabric import EnvironmentConfig, FabricConfig, HarnessConfig, MetadataConfig, ModelConfig, RuntimeConfig
    from nemo_fabric.models import SkillConfig
    root = root or HERE.parents[1]
    cat = write_catalog(odir / "codex_catalog" / "catalog.json", model, context_tokens=context_tokens, text_only=text_only)
    write_process_config(odir, cat["catalog"], context_tokens)
    overrides: dict[str, Any] = {"model_catalog_json": cat["catalog"], "model_context_window": int(context_tokens),
                                 "web_search": "disabled", "features.hooks": True, "hooks": hooks_config(),
                                 "bypass_hook_trust": True,
                                 # codex_guard.py decides every ws_files call, as guard.py decides Write and Edit
                                 "mcp_servers.ws_files.default_tools_approval_mode": "approve",
                                 "check_for_update_on_startup": False, "analytics.enabled": False,
                                 "skills.config": [{"path": str(codex_home(odir) / "skills" / ".system" / s / "SKILL.md"),
                                                    "enabled": False} for s in SYSTEM_SKILLS]}
    for f in DISABLED_FEATURES:
        overrides[f"features.{f}"] = False
    skills = sorted(str(p.parent) for p in (ws / ".claude" / "skills").glob("*/SKILL.md"))
    cfg = FabricConfig(
        metadata=MetadataConfig(name=f"tcadcal-{run_id}".lower()),
        harness=HarnessConfig(adapter_id="nvidia.fabric.codex", resolution="preinstalled",
                              settings={"sandbox": "danger-full-access", "approval_mode": "deny_all",
                                        "developer_instructions": DEVELOPER_LINE, "config_overrides": overrides}),
        models={"default": ModelConfig(provider=provider, model=model, base_url=base_url, api_key_env=api_key_env)},
        runtime=RuntimeConfig(artifacts=str(odir / "fabric_artifacts"), timeout_seconds=turn_timeout),
        environment=EnvironmentConfig(provider="local", workspace=str(ws), env=agent_env(ws, root, odir / "agent_home")),
        skills=SkillConfig(paths=skills),
    )
    cfg.add_mcp_server("ws_files", transport="stdio", url=str(PY), args=[str(HERE / "hooks" / "ws_files_mcp.py"), str(ws)])
    headers = {"x-api-key": "${" + exa_key_env + "}"} if exa_key_env and os.environ.get(exa_key_env) else None
    cfg.add_mcp_server("exa", transport="streamable-http", url=exa_url, custom_headers=headers)
    return cfg


def result_record(res: Any) -> dict[str, Any]:
    """Flatten a Fabric RunResult into the fields recorded by codex_orchestrate.py."""
    from collections.abc import Mapping
    out = dict(res.output) if isinstance(res.output, Mapping) else {}
    err = res.error or out.get("error")
    return {"status": str(res.status), "runtime_id": res.runtime_id, "invocation_id": res.invocation_id,
            "session_id": out.get("thread_id"), "turn_id": out.get("turn_id"), "response": out.get("response"),
            "usage": out.get("usage"), "failed": out.get("failed"),
            "error": (err if isinstance(err, dict) else (getattr(err, "__dict__", None) or str(err))) if err else None,
            "events": out.get("events") or [],
            "num_turns": 1 + sum(1 for e in (out.get("events") or []) if isinstance(e, dict)
                                 and e.get("type") in ("commandExecution", "mcpToolCall", "imageView",
                                                       "fileChange", "webSearch", "dynamicToolCall"))}


def write_agents_md(ws: Path) -> str:
    """AGENTS.md = CLAUDE.md + TOOL_NOTE (the only instruction text that the Codex harness adds)."""
    text = (ws / "CLAUDE.md").read_text().rstrip("\n") + "\n" + TOOL_NOTE
    (ws / "AGENTS.md").write_text(text)
    return text


SECRETS = Path(os.environ.get("TCADCAL_SECRETS_DIR", str(Path.home() / ".config" / "tcadcal")))
SECRET_ENV = {"FABRIC_GATEWAY_KEY": "litellm_master_key", "EXA_API_KEY": "exa_api_key"}


def load_secrets() -> dict[str, bool]:
    """Load gateway and optional Exa credentials without including them in run records."""
    found = {}
    for env, name in SECRET_ENV.items():
        path = SECRETS / name
        value = "".join(path.read_text().split()) if path.is_file() else ""
        found[env] = bool(value)
        if value:
            os.environ[env] = value
    return found

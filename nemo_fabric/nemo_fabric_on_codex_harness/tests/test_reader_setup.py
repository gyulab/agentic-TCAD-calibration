"""Exercise the documented CLI from a copy of the public code and synthetic inputs only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import pytest


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture()
def reader(tmp_path):
    harness = tmp_path / "package" / "nemo_fabric" / "nemo_fabric_on_codex_harness"
    shutil.copytree(SOURCE, harness, ignore=shutil.ignore_patterns(
        "env", "__pycache__", "*.pyc", ".pytest_cache", "kb_cache", "support", "tmp"))
    shutil.copytree(SOURCE.parents[1] / "devices", tmp_path / "package" / "devices")
    # Hooks use env/bin/python, just as they do in a reader's installation.
    (harness / "env").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    support = tmp_path / "synthetic-support"
    support.mkdir()
    (support / "IWO.par").write_text("Epsilon { epsilon = 9 }\nEpsilon_aniso { epsilon = 9 }\n")
    (support / "datexcodes.txt").write_text("Synthetic test fixture; never passed to Sentaurus.\n")
    kb = harness / "kb_cache" / "manual"
    kb.mkdir(parents=True)
    (kb / "chunks.jsonl").write_text(json.dumps({
        "id": "p0001", "pdf_page": 1, "chapter": "Synthetic fixture", "section": "Tools",
        "text": "Synthetic fixture for testing manual search; no vendor manual content."}) + "\n")
    (kb / "meta.json").write_text(json.dumps({"source": "synthetic fixture", "n_chunks": 1}))
    raw = harness.parents[1] / "experiment" / "raw" / "ref_L1"
    raw.mkdir(parents=True)
    vg = np.round(np.linspace(-1, 2, 61), 4)
    current = np.where(vg < .2, 1e-7 * 10 ** ((vg - .2) / .12), 1e-7 + 2e-5 * np.maximum(vg - .2, 0))
    current += 1e-12 * (1 + .1 * np.sin(37 * vg))
    for vd, tag in ((.05, "0p05"), (1., "1")):
        pd.DataFrame({"VG_V": vg, "VD_V": vd, "ID_drain_A": current}).to_csv(
            raw / f"ref_L1_VD_p{tag}V.csv", index=False)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env.update({"HOME": str(tmp_path), "TCAD_SUPPORT_DIR": str(support),
                "SENTAURUS_ROOT": str(tmp_path / "no-simulator")})
    return harness, env


def cli(reader, *args, check=True, cwd=None):
    harness, env = reader
    return subprocess.run([sys.executable, *args], cwd=cwd or harness, env=env, text=True, capture_output=True,
                          timeout=120, check=check)


def build(reader):
    cli(reader, "make_workspace.py", "--run-id", "READER", "--cap", "0")
    return reader[0].parents[1] / "runs" / "READER"


def test_workspace_tools_and_codex_configuration(reader):
    root = build(reader)
    ws = root / "workspace"
    meta = json.loads((ws / ".tcadcal_workspace.json").read_text())
    assert meta["agent_backend"] == "fabric"
    assert meta["agent_harness"] == "codex"
    assert meta["objective"] == "J_objective"
    assert meta["alignment_loss"] == "full_objective"
    assert meta["hashes"]["AGENTS.md"] == hashlib.sha256((ws / "AGENTS.md").read_bytes()).hexdigest()
    assert "WAIT_JOB <id>" in (ws / "AGENTS.md").read_text()
    assert "Only the author decides" in (ws / "AGENTS.md").read_text()
    assert not (ws / ".claude" / "settings.json").exists()
    assert json.loads((ws / "state" / "budget.json").read_text()) == {"used": 0, "cap": 0}
    result = cli(reader, "tools/state.py", "show", cwd=ws)
    assert "v000" in result.stdout
    # No model request: build_config reads the installed CLI's local model catalog.
    code = '''from pathlib import Path
import json
import codex_backend as B
ws = Path(__import__('sys').argv[1])
cfg = B.build_config(ws, ws.parent / 'orchestrator', run_id='READER', model='nemotron-3.5-super-vl',
                     base_url='http://127.0.0.1:4000', api_key_env='FABRIC_GATEWAY_KEY')
print(cfg.model_dump_json())
'''
    cfg = json.loads(cli(reader, "-c", code, str(ws)).stdout)
    assert cfg["harness"]["adapter_id"] == "nvidia.fabric.codex"
    assert cfg["models"]["default"]["base_url"] == "http://127.0.0.1:4000/v1"
    assert cfg["harness"]["settings"]["sandbox"] == "danger-full-access"
    assert str(reader[0] / "env" / "bin" / "python") in json.dumps(cfg)
    config = root / "orchestrator/fabric_artifacts/.fabric/codex/custom-provider-home/config.toml"
    assert "model_context_window = 500000" in config.read_text()


def test_missing_inputs_leave_no_partial_workspace(reader):
    (reader[0] / "kb_cache/manual/chunks.jsonl").unlink()
    result = cli(reader, "make_workspace.py", "--run-id", "MISSING", "--cap", "0", check=False)
    assert result.returncode != 0
    assert "Workspace inputs missing" in result.stderr
    assert "chunks.jsonl" in result.stderr
    assert not (reader[0].parents[1] / "runs/MISSING").exists()


def test_gate_waits_for_author_and_checks_reviewed_version(reader):
    root = build(reader)
    ws = root / "workspace"
    prop = ws / "proposals/P001.json"
    prop.write_text(json.dumps({"type": "bounds", "summary": "Synthetic review fixture"}))
    sha = hashlib.sha256(prop.read_bytes()).hexdigest()
    (ws / "proposals/P001.validated.json").write_text(json.dumps({"ok": True, "sha256": sha, "type": "bounds"}))
    result = cli(reader, "approve.py", "--run-id", "READER", "--show", "P001")
    assert sha in result.stdout
    approval = ws / "approvals/P001.json"
    assert not approval.exists()
    for args in (("--reviewed-sha", "0" * 64), ("--reviewed-sha", sha, "--approver", "agent")):
        refused = cli(reader, "approve.py", "--run-id", "READER", "P001", "approve", *args, check=False)
        assert refused.returncode != 0
        assert not approval.exists()
    code = ("from pathlib import Path; import orchestration as O; "
            "print(O.wait_approval(Path(__import__('sys').argv[1]), Path(__import__('sys').argv[2]), 'P001', .02))")
    waiter = subprocess.Popen([sys.executable, "-c", code, str(ws), str(root / "orchestrator")],
                              cwd=reader[0], env=reader[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not (root / "orchestrator/gate_pending.json").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / "orchestrator/gate_pending.json").exists()
        assert waiter.poll() is None
        assert not approval.exists()
        # This is a synthetic author-decision fixture, never a decision on a real run.
        cli(reader, "approve.py", "--run-id", "READER", "P001", "reject", "Synthetic author decision",
            "--reviewed-sha", sha)
        stdout, stderr = waiter.communicate(timeout=10)
        assert waiter.returncode == 0, stderr
        assert "reject" in stdout
        record = json.loads(approval.read_text())
        assert record["approver"] == "author"
        assert record["decision"] == "reject"
        assert not (root / "orchestrator/gate_pending.json").exists()
    finally:
        if waiter.poll() is None:
            waiter.terminate()
            waiter.communicate(timeout=10)

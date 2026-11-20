"""Guard parity: the same actions, sent once as Claude Code tool calls (hooks/guard.py) and once as Codex tool calls
(hooks/codex_guard.py), must get the same decision. Codex-only actions (shell writes, sed -i, find -exec, escalation,
a working directory outside the workspace) must be refused. No model, no Sentaurus.

    env/bin/python -m pytest -q tests/test_codex_guard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import codex_guard as CG  # noqa: E402
import guard as G  # noqa: E402


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    for d in ("proposals/history", "notes", "kb/literature", "state", "inputs", "evals", "logs", "tools", ".claude/skills/x"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "inputs/DEVICE_INFO.md").write_text("x")
    (tmp_path / "notes/journal.md").write_text("x")
    (tmp_path / "evals/E00025_bands_vd1.png").write_bytes(b"x")
    (tmp_path / ".tcadcal_workspace.json").write_text(json.dumps({"text_only_model": False}))
    return tmp_path.resolve()


BLOCK = (G.C.WEB_BLOCK_TERMS[0] if G.C.WEB_BLOCK_TERMS else None)

# (label, Claude Code call, Codex call)
PAIRS = [
    ("tool run", ("Bash", {"command": "python3 tools/state.py show"}), ("Bash", {"command": "python3 tools/state.py show"})),
    ("tool run with redirection", ("Bash", {"command": "python3 tools/state.py show > out.txt"}),
     ("Bash", {"command": "python3 tools/state.py show > out.txt"})),
    ("delete", ("Bash", {"command": "rm -rf state"}), ("Bash", {"command": "rm -rf state"})),
    ("network", ("Bash", {"command": "curl https://example.com"}), ("Bash", {"command": "curl https://example.com"})),
    ("python -c", ("Bash", {"command": "python3 -c 'print(1)'"}), ("Bash", {"command": "python3 -c 'print(1)'"})),
    ("shell write", ("Bash", {"command": "printf 'x' >> notes/journal.md"}), ("Bash", {"command": "printf 'x' >> notes/journal.md"})),
    ("read inside", ("Read", {"file_path": "inputs/DEVICE_INFO.md"}), ("Bash", {"command": "cat inputs/DEVICE_INFO.md"})),
    ("read part", ("Read", {"file_path": "CLAUDE.md"}), ("Bash", {"command": "sed -n '1,40p' CLAUDE.md"})),
    ("read head", ("Read", {"file_path": "notes/journal.md"}), ("Bash", {"command": "head -n 20 notes/journal.md"})),
    ("read outside", ("Read", {"file_path": "/etc/passwd"}), ("Bash", {"command": "cat /etc/passwd"})),
    ("read parent", ("Read", {"file_path": "../secret"}), ("Bash", {"command": "cat ../secret"})),
    ("list", ("Glob", {"pattern": "*", "path": "proposals"}), ("Bash", {"command": "ls proposals"})),
    ("search", ("Grep", {"pattern": "SS", "path": "notes/journal.md"}), ("Bash", {"command": "grep -n SS notes/journal.md"})),
    ("search outside", ("Grep", {"pattern": "x", "path": "/usr"}), ("Bash", {"command": "grep -rn x /usr"})),
    ("search for a path-like pattern", ("Grep", {"pattern": "/usr", "path": "notes/journal.md"}),
     ("Bash", {"command": "grep -n /usr notes/journal.md"})),
    ("skill file", ("Read", {"file_path": ".claude/skills/x/SKILL.md"}), ("Bash", {"command": "cat .claude/skills/x/SKILL.md"})),
    ("write proposal", ("Write", {"file_path": "proposals/P002.json"}), ("mcp__ws_files__write_file", {"path": "proposals/P002.json", "content": "{}"})),
    ("write note (dotted name)", ("Write", {"file_path": "notes/a.md"}), ("ws_files.write_file", {"path": "notes/a.md", "content": "x"})),
    ("edit note", ("Edit", {"file_path": "notes/journal.md"}), ("mcp__ws_files__edit_file", {"path": "notes/journal.md", "old_string": "x", "new_string": "y"})),
    ("write state", ("Write", {"file_path": "state/x.json"}), ("mcp__ws_files__write_file", {"path": "state/x.json", "content": "x"})),
    ("write tools", ("Write", {"file_path": "tools/x.py"}), ("mcp__ws_files__write_file", {"path": "tools/x.py", "content": "x"})),
    ("write validation record", ("Write", {"file_path": "proposals/P002.validated.json"}),
     ("mcp__ws_files__write_file", {"path": "proposals/P002.validated.json", "content": "x"})),
    ("write history", ("Write", {"file_path": "proposals/history/a.json"}), ("mcp__ws_files__write_file", {"path": "proposals/history/a.json", "content": "x"})),
    ("web search", ("mcp__exa__web_search_exa", {"query": "amorphous oxide TFT acceptor-like tail states"}),
     ("mcp__exa__web_search_exa", {"query": "amorphous oxide TFT acceptor-like tail states"})),
    ("web search (dotted name)", ("mcp__exa__web_search_exa", {"query": "IGZO subgap density of states"}),
     ("exa.web_search_exa", {"query": "IGZO subgap density of states"})),
    ("web fetch", ("mcp__exa__web_fetch_exa", {"urls": ["https://www.example.org/paper"]}),
     ("mcp__exa__web_fetch_exa", {"urls": ["https://www.example.org/paper"]})),
    ("web fetch localhost", ("mcp__exa__web_fetch_exa", {"urls": ["http://127.0.0.1:4000/"]}),
     ("mcp__exa__web_fetch_exa", {"urls": ["http://127.0.0.1:4000/"]})),
    ("image", ("Read", {"file_path": "evals/E00025_bands_vd1.png"}), ("view_image", {"path": "evals/E00025_bands_vd1.png"})),
    ("image outside", ("Read", {"file_path": "/tmp/x.png"}), ("view_image", {"path": "/tmp/x.png"})),
    ("plan", ("TodoWrite", {"todos": []}), ("update_plan", {"plan": []})),
    ("sub-agent", ("Agent", {"prompt": "x"}), ("spawn_agent", {"prompt": "x"})),
]
if BLOCK:
    PAIRS.append(("web search blocked term", ("mcp__exa__web_search_exa", {"query": f"{BLOCK} calibration"}),
                  ("mcp__exa__web_search_exa", {"query": f"{BLOCK} calibration"})))

CODEX_ONLY_DENY = [
    ("sed in place", ("Bash", {"command": "sed -i 's/x/y/' notes/journal.md"})),
    ("sed write command", ("Bash", {"command": "sed -n 'w notes/out.md' CLAUDE.md"})),
    ("find exec", ("Bash", {"command": "find . -name '*.md' -exec rm {} ;"})),
    ("find delete", ("Bash", {"command": "find notes -delete"})),
    ("escalation", ("Bash", {"command": "python3 tools/state.py show", "sandbox_permissions": "require_escalated"})),
    ("workdir outside", ("Bash", {"command": "ls", "workdir": "/usr"})),
    ("substitution", ("Bash", {"command": "cat $(echo CLAUDE.md)"})),
    ("heredoc write", ("Bash", {"command": "cat > notes/x.md <<'EOF'\nx\nEOF"})),
    # output-only forms are allowed, but nothing that writes, runs another program or reads outside
    ("pipe into a writer", ("Bash", {"command": "python3 tools/state.py show | tee notes/x.md"})),
    ("pipe into a shell", ("Bash", {"command": "cat CLAUDE.md | sh"})),
    ("pipe into python", ("Bash", {"command": "cat CLAUDE.md | python3"})),
    ("redirect after filter", ("Bash", {"command": "python3 tools/state.py show | head > notes/x.md"})),
    ("list with delete", ("Bash", {"command": "cat CLAUDE.md; rm -rf state"})),
    ("list with outside read", ("Bash", {"command": "cat CLAUDE.md && cat /etc/passwd"})),
    ("filter outside", ("Bash", {"command": "python3 tools/state.py show | grep x /etc/passwd"})),
    ("background", ("Bash", {"command": "python3 tools/state.py show &"})),
    ("stderr to file", ("Bash", {"command": "python3 tools/state.py show 2> notes/err.txt"})),
    ("input from outside", ("Bash", {"command": "grep x < /etc/passwd"})),
    ("subshell", ("Bash", {"command": "(cat CLAUDE.md)"})),
    ("sed write in pipe", ("Bash", {"command": "cat CLAUDE.md | sed -n 'w notes/x.md'"})),
    ("python -c in list", ("Bash", {"command": "python3 tools/state.py show && python3 -c 'print(1)'"})),
    ("find outside in list", ("Bash", {"command": "ls; find /usr -name proposals.py"})),
    # the pattern is skipped, the files are still checked
    ("grep -e outside file", ("Bash", {"command": "grep -e x /etc/passwd"})),
    ("grep pattern file outside", ("Bash", {"command": "grep -f /etc/passwd notes/journal.md"})),
    ("sed script then outside file", ("Bash", {"command": "sed -n '/x/p' /etc/passwd"})),
    ("rg pattern then outside dir", ("Bash", {"command": "rg -n '/x' /usr"})),
]

CODEX_V2_ALLOW = [
    ("tool with 2>&1", "python3 tools/state.py propose P002 2>&1"),
    ("tool piped to head", "python3 tools/state.py propose P002 2>&1 | head -30"),
    ("tool piped to grep", "python3 tools/state.py show | grep -n 'J'"),
    ("tool piped to two filters", "python3 tools/state.py show | grep -E 'a|b' | wc -l"),
    ("read piped to head", "cat CLAUDE.md | head"),
    ("read with stderr discarded", "cat notes/journal.md 2>/dev/null"),
    ("list of reads and echo", "cat notes/journal.md 2>/dev/null; echo '---'; ls proposals"),
    ("and-list of tools", "python3 tools/state.py show && python3 tools/jobs.py list"),
    ("sed -n in pipe", "python3 tools/state.py show | sed -n '1,20p'"),
    ("sed range script starting with /", "sed -n '/\"terms\"/,/}/p' notes/journal.md | head -150"),
    ("grep -e pattern starting with /", "grep -n -e '/usr' notes/journal.md"),
    ("piped grep for a path", "python3 tools/state.py show | grep '/opt/data'"),
]


@pytest.mark.parametrize("label,claude,codex", PAIRS, ids=[p[0] for p in PAIRS])
def test_same_decision(ws: Path, label: str, claude: tuple, codex: tuple) -> None:
    d1, r1 = G.decide({"tool_name": claude[0], "tool_input": claude[1]}, ws)
    d2, r2 = CG.decide({"tool_name": codex[0], "tool_input": codex[1]}, ws)
    assert d1 == d2, (label, d1, r1, d2, r2)


@pytest.mark.parametrize("label,codex", CODEX_ONLY_DENY, ids=[p[0] for p in CODEX_ONLY_DENY])
def test_codex_only_refused(ws: Path, label: str, codex: tuple) -> None:
    d, r = CG.decide({"tool_name": codex[0], "tool_input": codex[1]}, ws)
    assert d == "deny", (label, r)


@pytest.mark.parametrize("label,cmd", CODEX_V2_ALLOW, ids=[p[0] for p in CODEX_V2_ALLOW])
def test_codex_v2_output_forms_allowed(ws: Path, label: str, cmd: str) -> None:
    d, r = CG.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, ws)
    assert d == "allow", (label, r)


def test_outside_read_names_where_the_rules_are(ws: Path) -> None:
    d, r = CG.decide({"tool_name": "Bash", "tool_input": {"command": "cat /opt/other/lib/tcadcal/proposals.py"}}, ws)
    assert d == "deny" and "state.py propose" in r


def test_text_only_image(ws: Path) -> None:
    (ws / ".tcadcal_workspace.json").write_text(json.dumps({"text_only_model": True}))
    d1, _ = G.decide({"tool_name": "Read", "tool_input": {"file_path": "evals/E00025_bands_vd1.png"}}, ws)
    d2, _ = CG.decide({"tool_name": "view_image", "tool_input": {"path": "evals/E00025_bands_vd1.png"}}, ws)
    assert d1 == d2 == "deny"

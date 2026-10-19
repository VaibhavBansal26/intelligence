"""Docs drift checks (run by pytest and CI): links, index, config/env parity, OpenAPI freshness, Mermaid, route coverage."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.config import Settings
from app.tools import docs_config, export_openapi

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MD_FILES = [
    *ROOT.glob("*.md"),
    *DOCS.rglob("*.md"),
    ROOT / "contracts" / "README.md",
    ROOT / ".github" / "pull_request_template.md",
]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    s = re.sub(r"[`*_]", "", heading.strip().lower())
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    out: set[str] = set()
    in_code = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("#"):
            out.add(slug(line.lstrip("#")))
    return out


def test_relative_links_and_anchors_resolve() -> None:
    problems: list[str] = []
    for md in MD_FILES:
        text = re.sub(r"```.*?```", "", md.read_text(), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:", target) or (target.startswith("#") and False):
                continue
            path_part, _, frag = target.partition("#")
            dest = md if not path_part else (md.parent / path_part).resolve()
            if not dest.exists():
                problems.append(f"{md.relative_to(ROOT)}: missing {target}")
            elif frag and dest.suffix == ".md" and frag not in anchors(dest):
                problems.append(f"{md.relative_to(ROOT)}: no anchor #{frag} in {dest.name}")
    assert not problems, "\n".join(problems)


def test_docs_index_lists_every_doc() -> None:
    index = (DOCS / "README.md").read_text()
    missing = []
    for f in sorted(DOCS.rglob("*")):
        if f.is_file() and f.name != "README.md":
            rel = f.relative_to(DOCS).as_posix()
            if rel not in index:
                missing.append(rel)
    assert not missing, f"docs/README.md does not mention: {missing}"


def test_configuration_doc_matches_settings_model() -> None:
    doc = (DOCS / "CONFIGURATION.md").read_text()
    documented = set(re.findall(r"^\| `([A-Z0-9_]+)` \|", doc, flags=re.M))
    defined = {n.upper() for n in Settings.model_fields}
    assert documented == defined, f"only in doc: {documented - defined}; only in code: {defined - documented}"
    assert "UNDOCUMENTED" not in doc
    env_example = {
        line.split("=")[0] for line in (ROOT / ".env.example").read_text().splitlines() if re.match(r"^[A-Z_]+=", line)
    }
    assert env_example <= defined, f".env.example has unknown variables: {env_example - defined}"
    assert docs_config.render() == doc, "CONFIGURATION.md is stale: run `make docs-config`"


def test_openapi_json_is_fresh() -> None:
    assert (DOCS / "openapi.json").read_text() == export_openapi.render(), "openapi.json is stale: run `make docs-api`"


def test_every_route_is_documented_in_api_md() -> None:
    api = (DOCS / "API.md").read_text()
    spec = json.loads((DOCS / "openapi.json").read_text())
    missing = []
    for path, ops in spec["paths"].items():
        for method in ops:
            if f"`{method.upper()} {path}`" not in api:
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"routes missing from docs/API.md: {missing}"


def test_changelog_has_unreleased_section() -> None:
    assert re.search(r"^## \[Unreleased\]", (ROOT / "CHANGELOG.md").read_text(), flags=re.M)


def test_required_docs_exist() -> None:
    for name in (
        "ARCHITECTURE",
        "APPROACH",
        "MODELS",
        "AI",
        "TESTING",
        "API",
        "RUNBOOK",
        "SECURITY",
        "GLOSSARY",
        "CONFIGURATION",
        "PERFORMANCE",
    ):
        assert (DOCS / f"{name}.md").exists(), name
    for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"):
        assert (ROOT / name).exists(), name
    assert len(list((DOCS / "adr").glob("0*.md"))) >= 6
    assert (ROOT / ".github" / "pull_request_template.md").exists()


# --- Mermaid ------------------------------------------------------------------------------------------
KINDS = ("flowchart", "graph", "sequenceDiagram", "erDiagram", "classDiagram", "stateDiagram-v2", "gantt", "pie")


def mermaid_blocks() -> list[tuple[str, str]]:
    out = []
    for md in MD_FILES:
        for i, block in enumerate(re.findall(r"```mermaid\n(.*?)```", md.read_text(), flags=re.S)):
            out.append((f"{md.relative_to(ROOT)}#{i}", block))
    return out


def structural_errors(block: str) -> list[str]:
    errs: list[str] = []
    lines = [ln for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    if not lines or not lines[0].strip().startswith(KINDS):
        return ["unknown or missing diagram type"]
    kind = lines[0].split()[0]
    pairs = (("[", "]"), ("(", ")")) if kind == "erDiagram" else (("[", "]"), ("(", ")"), ("{", "}"))
    for opener, closer in pairs:
        if block.count(opener) != block.count(closer):
            errs.append(f"unbalanced {opener}{closer}")
    if block.count('"') % 2:
        errs.append("unbalanced quotes")
    if kind in ("flowchart", "graph"):
        subs = sum(1 for ln in lines if ln.strip().startswith("subgraph"))
        ends = sum(1 for ln in lines if ln.strip() == "end")
        if subs != ends:
            errs.append("subgraph/end mismatch")
        if not any("-->" in ln or "---" in ln or "-.->" in ln for ln in lines):
            errs.append("no edges")
    if kind == "sequenceDiagram":
        opens = sum(1 for ln in lines if re.match(r"\s*(loop|alt|opt|par)\b", ln))
        ends = sum(1 for ln in lines if ln.strip() == "end")
        if opens != ends:
            errs.append("loop/end mismatch")
        if not any(re.search(r"->>|-->>|->|-->", ln) for ln in lines[1:]):
            errs.append("no messages")
    if kind == "erDiagram":
        depth = 0
        for ln in lines[1:]:
            if ln.rstrip().endswith("{"):
                depth += 1
            elif ln.strip() == "}":
                depth -= 1
            elif depth == 0 and not re.search(r"\|\|--|o\{|\}o|\|o|o\|", ln):
                errs.append(f"bad relationship line: {ln.strip()}")
        if depth != 0:
            errs.append("unclosed entity block")
    return errs


def test_mermaid_diagrams_exist_and_are_structurally_valid() -> None:
    blocks = mermaid_blocks()
    assert len(blocks) >= 5
    bad = {name: structural_errors(b) for name, b in blocks if structural_errors(b)}
    assert not bad, bad


CHECKER = ROOT / "tools" / "mermaid_check"


@pytest.mark.skipif(
    shutil.which("node") is None or not (CHECKER / "node_modules").exists(),
    reason="run `npm ci --prefix tools/mermaid_check` to enable the real Mermaid parser (CI does); structural check still runs",
)
def test_mermaid_diagrams_parse_with_real_mermaid_parser() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(mermaid_blocks(), f)
    r = subprocess.run(["node", str(CHECKER / "parse.mjs"), f.name], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr

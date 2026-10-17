"""The read-only knowledge catalogue must be real, navigable and self-contained."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "offensive"
KNOWLEDGE_ROOT = REPO_ROOT / "knowledge"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch-knowledge.sh"

# name|repository|pinned revision|licence note
CORPORA_ENTRY = re.compile(
    r'^\s*"([A-Za-z0-9-]+)\|(https://[^|"]+)\|([0-9a-f]{40})\|([^"|]+)"\s*$',
    re.MULTILINE,
)

# A manual that cannot say when to use it, what it should establish and what it
# is not allowed to do is a note, not a skill. These sections are the contract.
REQUIRED_SECTIONS = (
    "## 何时使用",
    "## 预期结论",
    "## 侦察与判定信号",
    "## payload 家族与变体",
    "## 预期证据形态",
    "## 操作边界",
    "## 常见坑",
    "## 提示",
)

# Short stubs were the previous state; a real manual carries substance.
MIN_SKILL_LINES = 50

MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)#][^)]*)\)")


def _skill_files() -> list[Path]:
    return sorted(SKILL_ROOT.glob("*/SKILL.md"))


def test_offensive_catalogue_is_present() -> None:
    assert len(_skill_files()) == 7


@pytest.mark.parametrize("path", _skill_files(), ids=lambda path: path.parent.name)
def test_skill_documents_the_required_sections(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    missing = [section for section in REQUIRED_SECTIONS if section not in text]
    assert not missing, f"{path}: missing sections {missing}"
    assert len(text.splitlines()) >= MIN_SKILL_LINES, f"{path}: too thin to be a manual"


def test_knowledge_index_links_resolve() -> None:
    missing: list[str] = []
    for path in sorted(KNOWLEDGE_ROOT.rglob("*.md")):
        for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_file():
                missing.append(f"{path.relative_to(REPO_ROOT)}: {target}")

    assert not missing, "knowledge links do not resolve:\n" + "\n".join(missing)


def test_knowledge_catalogue_stays_offline() -> None:
    """Bulk corpora are fetched explicitly; nothing in the tree depends on them."""
    index = (KNOWLEDGE_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/fetch-knowledge.sh" in index
    for path in sorted(KNOWLEDGE_ROOT.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, path
    # The in-repo catalogue must stand on its own: methodology and categories
    # are real files, not pointers at the fetched cache.
    assert (KNOWLEDGE_ROOT / "methodology" / "recon-to-report.md").is_file()
    assert (KNOWLEDGE_ROOT / "methodology" / "evidence-and-reporting.md").is_file()
    for category in ("web", "network", "cloud", "ad-and-credentials"):
        assert (KNOWLEDGE_ROOT / "categories" / f"{category}.md").is_file()


def test_bulk_corpus_manifest_is_pinned_and_indexed() -> None:
    script = FETCH_SCRIPT.read_text(encoding="utf-8")
    entries = CORPORA_ENTRY.findall(script)

    assert len(entries) >= 5
    names = [name for name, _repo, _revision, _licence in entries]
    assert len(names) == len(set(names))

    index = (KNOWLEDGE_ROOT / "README.md").read_text(encoding="utf-8")
    for name, repo, revision, licence in entries:
        assert repo.startswith("https://"), name
        assert len(revision) == 40, name
        assert licence.strip(), name
        assert name in index, f"{name} is not listed in knowledge/README.md"


def test_fetch_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(FETCH_SCRIPT)], check=True)

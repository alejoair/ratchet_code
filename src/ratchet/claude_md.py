"""Parser for the CLAUDE.md contract.

Extracts the three required sections (file_tree, architecture,
restrictions) from a repository's CLAUDE.md file. The restrictions
section is injected verbatim into each executor's system prompt.
"""

import logging
import re
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_REQUIRED_SECTIONS = ("file_tree", "architecture", "restrictions")

_HEADING_RE = re.compile(r"^##\s+(\S+)", re.MULTILINE)


class ClaudeMd(BaseModel):
    """Parsed CLAUDE.md sections.

    Attributes:
        file_tree: The file_tree section content.
        architecture: The architecture section content.
        restrictions: The restrictions section content.
            Injected verbatim into executor system prompts.
    """

    file_tree: str
    architecture: str
    restrictions: str


def parse_claude_md(repo_path: str) -> ClaudeMd:
    """Parse CLAUDE.md from a repository.

    Reads <repo_path>/CLAUDE.md and extracts the three required
    sections. Raises ValueError if any section is missing.

    Args:
        repo_path: Absolute path to the repository.

    Returns:
        A ClaudeMd with the parsed sections.

    Raises:
        FileNotFoundError: If CLAUDE.md does not exist.
        ValueError: If any required section is missing.
    """
    md_path = Path(repo_path) / "CLAUDE.md"
    content = md_path.read_text(encoding="utf-8")

    sections = _split_sections(content)

    missing = [
        s for s in _REQUIRED_SECTIONS if s not in sections
    ]
    if missing:
        raise ValueError(
            f"CLAUDE.md is missing required sections: "
            f"{', '.join(missing)}"
        )

    return ClaudeMd(
        file_tree=sections["file_tree"],
        architecture=sections["architecture"],
        restrictions=sections["restrictions"],
    )


def _split_sections(content: str) -> dict[str, str]:
    """Split CLAUDE.md content into named sections.

    Sections are delimited by ## heading lines. The content
    of each section runs from after the heading to the next heading
    (or end of file).

    Args:
        content: Full text of CLAUDE.md.

    Returns:
        Dict mapping section names to their content strings.
    """
    matches = list(_HEADING_RE.finditer(content))
    sections: dict[str, str] = {}

    for i, match in enumerate(matches):
        name = match.group(1).lower()
        start = match.end()
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(content)
        )
        sections[name] = content[start:end].strip()

    return sections

"""The research taxonomy papers are tagged into (config/taxonomy.yaml).

A taxonomy is a tree of categories. Papers are tagged only with leaves -- the
most specific categories -- identified by their id path, e.g.
"mitigation/pre-processing/reweighing-and-resampling", which is what the sheet
stores (see pipeline_stages.py and tagger.py). Two more tags are reserved and
are not part of the tree: "other" for papers scoring below
taxonomy.min_score, and "unsorted" for a paper that's in scope but fits no
leaf, a sign the taxonomy needs a new category.

Displays number the outline from the YAML's order (1, A, i, a), so "1.A.i" is
the first leaf of the first branch. Numbers move when a category is inserted;
the id path doesn't, which is why the path is the stored tag.

`tag_schema` builds the structured-output schema for the tagging call (see
llm_tag.py): its category field is an enum of exactly the current leaves plus
"unsorted", so the model can't return a tag that doesn't exist.
"""

from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, create_model

from lit_pipeline.config import PROJECT_ROOT

OTHER = "other"
UNSORTED = "unsorted"
RESERVED = (OTHER, UNSORTED)

# Outline numbering by depth: 1, A, i, a. Four levels deep at most.
_ROMAN = (
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
)
_MAX_SIBLINGS = (99, 26, len(_ROMAN), 26)
_NODE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Category:
    id: str  # the id path, e.g. "mitigation/pre-processing/reweighing-and-resampling"
    number: str  # outline number, e.g. "1.A.i"
    label: str
    description: str
    depth: int
    is_leaf: bool


class Taxonomy:
    def __init__(self, categories: list[Category], rules: tuple[str, ...], source_text: str) -> None:
        self.categories = tuple(categories)  # in outline order
        self.rules = rules
        self.by_id = {c.id: c for c in self.categories}
        self.leaf_ids = tuple(c.id for c in self.categories if c.is_leaf)
        # Changes whenever the file does, so a run can tell its tags came
        # from a different version of the taxonomy.
        self.content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:12]
        self.prompt_text = self._render_prompt()

    def is_tag(self, tag: str) -> bool:
        """Whether a paper can be tagged `tag` with this taxonomy."""
        category = self.by_id.get(tag)
        return tag in RESERVED or (category is not None and category.is_leaf)

    def display(self, tag: str) -> str:
        """'1.A.i Reweighing and resampling' for a leaf, 'Other' or 'Unsorted' for a reserved tag."""
        category = self.by_id.get(tag)
        if category is None:
            return tag.capitalize() if tag in RESERVED else tag
        return f"{category.number} {category.label}"

    def top_level(self, tag: str) -> Category | None:
        """The branch a tag sits under, or None for a reserved or unknown tag."""
        return self.by_id.get(tag.split("/", 1)[0]) if tag not in RESERVED else None

    def context(self, tag: str) -> str:
        """The branches above a category, top down, for showing next to
        display(): 'Bias mitigation methods › In-processing › Regularization
        and constraints' for 1.B.i.a. Empty for a top-level category, a
        reserved tag or an unknown one."""
        if tag not in self.by_id:
            return ""
        parts = tag.split("/")
        return " › ".join(self.by_id["/".join(parts[:i])].label for i in range(1, len(parts)))

    def _render_prompt(self) -> str:
        lines = []
        if self.rules:
            lines.append("Placement rules:")
            lines += [f"{i}. {rule}" for i, rule in enumerate(self.rules, start=1)]
            lines.append("")
        lines.append("Categories. Tag with a leaf's id, shown in brackets; branches without an id are grouping only.")
        for c in self.categories:
            indent = "  " * c.depth
            tag = f" [{c.id}]" if c.is_leaf else ""
            description = f": {c.description}" if c.description else ""
            lines.append(f"{indent}{c.number}{tag} {c.label}{description}")
        return "\n".join(lines)


def load_taxonomy(path: str | Path) -> Taxonomy:
    """Load and validate a taxonomy file. A relative `path` is resolved
    against the project root, the way settings.yaml writes it."""
    resolved = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    return _load(str(resolved.resolve()))


@functools.lru_cache(maxsize=4)
def _load(resolved: str) -> Taxonomy:
    text = Path(resolved).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    nodes = raw.get("nodes") or []
    if not nodes:
        raise ValueError(f"{resolved} has no categories under 'nodes'")

    categories: list[Category] = []

    def walk(items: list[dict], prefix: str, number_prefix: str, depth: int) -> None:
        if depth >= len(_MAX_SIBLINGS):
            raise ValueError(f"{resolved}: categories nest at most {len(_MAX_SIBLINGS)} levels deep")
        if len(items) > _MAX_SIBLINGS[depth]:
            raise ValueError(f"{resolved}: too many categories under {prefix or 'the top level'} to number")
        seen: set[str] = set()
        for index, node in enumerate(items):
            node_id = str(node.get("id", ""))
            path = prefix + node_id
            if not _NODE_ID.match(node_id):
                raise ValueError(f"{resolved}: id {path!r} must be lowercase words joined by hyphens")
            if node_id in seen:
                raise ValueError(f"{resolved}: duplicate id {path!r}")
            if path in RESERVED:
                raise ValueError(f"{resolved}: {path!r} is reserved and can't be a category")
            seen.add(node_id)
            label = str(node.get("label", "")).strip()
            description = str(node.get("description", "")).strip()
            children = node.get("children") or []
            if not label:
                raise ValueError(f"{resolved}: {path!r} needs a label")
            if not children and not description:
                # The description is what the tagging model places papers by.
                raise ValueError(f"{resolved}: leaf {path!r} needs a description")
            number = number_prefix + _outline_token(index, depth)
            categories.append(Category(path, number, label, description, depth, is_leaf=not children))
            if children:
                walk(children, path + "/", number + ".", depth + 1)

    walk(nodes, "", "", 0)
    rules = tuple(str(rule).strip() for rule in raw.get("rules") or [] if str(rule).strip())
    return Taxonomy(categories, rules, text)


def _outline_token(index: int, depth: int) -> str:
    if depth == 0:
        return str(index + 1)
    if depth == 2:
        return _ROMAN[index]
    return chr((ord("A") if depth == 1 else ord("a")) + index)


@functools.lru_cache(maxsize=4)
def tag_schema(taxonomy: Taxonomy) -> type[BaseModel]:
    """The structured-output schema for one tagging call: a single category,
    limited to this taxonomy's leaves plus "unsorted"."""
    choices = Literal[taxonomy.leaf_ids + (UNSORTED,)]  # type: ignore[valid-type]
    return create_model(
        "PaperTag",
        category=(
            choices,
            Field(
                description=(
                    "The id of the single most specific category that matches the paper's main "
                    'contribution, or "unsorted" if the paper is in scope but no category fits.'
                )
            ),
        ),
    )

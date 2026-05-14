"""Shared config loader for lxp_tools/.

Loads `lxp_tools.config.yaml` and resolves relative paths against the
repository root. All three CLI scripts in lxp_tools/ pull defaults through
here.

Config discovery, in order:
  1. Explicit --config PATH (passed to load_config()).
  2. <project_root>/lxp_tools.config.yaml, where <project_root> is the
     nearest ancestor of cwd that contains either an `lxp_tools/` directory
     or an `lxp_tools.config.yaml` file. This lets one shared lxp_tools/
     directory (e.g., installed once at ~/Repos/lxp-tools and symlinked in)
     serve multiple course repos that each carry their own root-level
     config.
  3. The bundled fallback at <lxp_tools/>/lxp_tools.config.yaml — template
     values shipped with the toolkit. Used only when neither (1) nor (2)
     resolves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


TOOLS_DIR = Path(__file__).resolve().parent
BUNDLED_CONFIG_PATH = TOOLS_DIR / "lxp_tools.config.yaml"


@dataclass
class LxpToolsConfig:
    course_title: str
    course_color: str
    repo_id: int
    build_plans_dir: Path
    export_output_dir: Path
    interactives_dirs: list[Path]
    module_folder_pattern: str
    module_name_pattern: str
    manifest_schema_path: Path
    repo_root: Path
    config_path: Path = field(default=BUNDLED_CONFIG_PATH)

    def module_folder(self, n: int) -> Path:
        return self.build_plans_dir / self.module_folder_pattern.format(n=n)

    def module_name(self, n: int) -> str:
        return self.module_name_pattern.format(n=n)


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` looking for a project root.

    A project root is the nearest ancestor that contains either an
    `lxp_tools/` directory or an `lxp_tools.config.yaml` file. Falls back
    to the parent of lxp_tools/ if nothing matches.
    """
    p = start.resolve()
    while p != p.parent:
        if (p / "lxp_tools.config.yaml").is_file() or (p / "lxp_tools").is_dir():
            return p
        p = p.parent
    return TOOLS_DIR.parent


def discover_config_path() -> Path:
    """Resolve the config path when none was passed explicitly.

    Prefers <project_root>/lxp_tools.config.yaml; falls back to the
    bundled template inside lxp_tools/ if no project-level config exists.
    """
    project_root = find_repo_root(Path.cwd())
    project_cfg = project_root / "lxp_tools.config.yaml"
    if project_cfg.is_file():
        return project_cfg
    return BUNDLED_CONFIG_PATH


def load_config(path: Path | None = None, repo_root: Path | None = None) -> LxpToolsConfig:
    cfg_path = Path(path) if path else discover_config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    if repo_root is None:
        repo_root = find_repo_root(cfg_path.parent)
    repo_root = Path(repo_root).resolve()

    def _resolve(p: str) -> Path:
        pth = Path(p)
        return pth if pth.is_absolute() else (repo_root / pth)

    interactives = [_resolve(p) for p in raw.get("interactives_dirs", ["Interactives", "course_outline"])]
    schema_rel = raw.get("manifest_schema_file", "manifest_schema.json")
    schema_path = Path(schema_rel)
    if not schema_path.is_absolute():
        schema_path = TOOLS_DIR / schema_path

    return LxpToolsConfig(
        course_title=raw.get("course_title", "Course"),
        course_color=raw.get("course_color", "#2196F3"),
        repo_id=int(raw.get("repo_id", 9000)),
        build_plans_dir=_resolve(raw.get("build_plans_dir", "lxp_build_plans")),
        export_output_dir=_resolve(raw.get("export_output_dir", "lxp_export")),
        interactives_dirs=interactives,
        module_folder_pattern=raw.get("module_folder_pattern", "Module{n}"),
        module_name_pattern=raw.get("module_name_pattern", "Module {n}"),
        manifest_schema_path=schema_path,
        repo_root=repo_root,
        config_path=cfg_path,
    )


def discover_segment_yamls(cfg: LxpToolsConfig) -> list[tuple[int, str, list[Path]]]:
    """Walk build_plans_dir for Module<N>/segment_*.yaml.

    Returns [(module_ordinal, module_name, [yaml_paths sorted])] in module order.
    Excludes files starting with '_' and ones with .skip suffix.
    """
    out: list[tuple[int, str, list[Path]]] = []
    n = 1
    while True:
        folder = cfg.module_folder(n)
        if not folder.is_dir():
            break
        yamls = sorted(
            p for p in folder.glob("segment_*.yaml")
            if not p.name.startswith("_") and not p.name.endswith(".skip.yaml")
        )
        # Sort welcome (no number) before numbered segments, then by numeric order.
        def sort_key(p: Path) -> tuple[int, str]:
            stem = p.stem  # segment_1.0 or segment_welcome
            rest = stem[len("segment_"):]
            # Welcome / non-numeric first
            try:
                num = float(rest.split("_")[0])
                return (1, f"{num:09.3f}")
            except ValueError:
                return (0, rest)
        yamls.sort(key=sort_key)
        out.append((n, cfg.module_name(n), yamls))
        n += 1
    return out

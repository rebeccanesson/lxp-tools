#!/usr/bin/env python3
"""Convert an LXP export folder into build-plan YAMLs.

Export → YAML. Walks every FOLDER (module) → PAGE (segment) in the
extracted export and emits one YAML per PAGE under
`<output_dir>/Module<N>/`.

For each LXP_ADV_HTML element, attempts to reverse-resolve widget file
paths (script_0 / style_0) via a sha256 index over the configured
interactives_dirs. The instance HTML body is resolved the same way
(matched against the SHA-256 of file contents); if no source file
matches, the body is inlined as `html_inline_body:`.

Filename selection:
  - If the target Module<N>/ subdir already has YAMLs, match each new
    PAGE to an existing file by `page_title` and reuse the filename.
    This preserves the user's chosen names across round-trips.
  - Otherwise, derive a deterministic slug from page_title.

Usage:
  # Round-trip our own export back to YAML for validation
  python3 lxp_tools/export_to_yaml.py lxp_export/cs20_full \\
      --output-dir lxp_build_plans_v2

  # Ingest a tarball the user re-exported from LXP
  python3 lxp_tools/export_to_yaml.py _context/lxp_user_exports/cs20_2026-05-13_user_edits.tgz \\
      --output-dir lxp_build_plans
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import unicodedata
from pathlib import Path

import yaml


def _nfc(s: str) -> str:
    """NFC-normalize a string. macOS filesystems return NFD-decomposed
    filenames; YAMLs need to be portable across filesystems."""
    return unicodedata.normalize("NFC", s)

from _config import load_config, LxpToolsConfig


# ---------------------------------------------------------------------------
# YAML emitters (string-based, to control style — block scalars for HTML)
# ---------------------------------------------------------------------------

def _yaml_str(s: str | None) -> str:
    if s is None:
        return "null"
    if "\x00" in s:
        raise ValueError("null byte in string")
    # PyYAML rejects C0 (except \n \t) and C1 (0x80-0x9F) in scalar context.
    # Switch to double-quoted form whenever any such char appears.
    def is_yaml_unsafe(c: str) -> bool:
        o = ord(c)
        if o < 0x20 and c not in ("\n", "\t"):
            return True
        if o == 0x7F:
            return True
        if 0x80 <= o <= 0x9F:
            return True
        return False
    needs_double = any(is_yaml_unsafe(c) for c in s) and "\n" not in s
    if needs_double:
        out = '"'
        for c in s:
            if c == '"':       out += '\\"'
            elif c == "\\":    out += "\\\\"
            elif c == "\t":    out += "\\t"
            elif is_yaml_unsafe(c):
                o = ord(c)
                if o <= 0xFF:
                    out += f"\\x{o:02X}"
                else:
                    out += f"\\u{o:04X}"
            else:
                out += c
        out += '"'
        return out
    return "'" + s.replace("'", "''") + "'"


def _block_scalar(s: str, indent: str) -> str:
    if not s:
        return '""'
    lines = s.split("\n")
    return "|\n" + "\n".join(indent + line for line in lines)


# ---------------------------------------------------------------------------
# Content-hash index over interactives_dirs
# ---------------------------------------------------------------------------

class HashIndex:
    """sha256(file_bytes) → relative_path (from repo_root).

    Scans the configured interactives_dirs plus an optional snapshot
    fallback (the export's own repository/assets/, useful when source
    files have drifted since the export was built).
    """

    def __init__(self, cfg: LxpToolsConfig, fallback_assets_dir: Path | None = None):
        self.cfg = cfg
        self._index: dict[str, str] = {}
        for root in cfg.interactives_dirs:
            self._scan(root, prefer=True)
        # Snapshot fallback: hash and store, but only if not already mapped
        # to a "better" path under interactives_dirs.
        if fallback_assets_dir and fallback_assets_dir.is_dir():
            self._scan_snapshot(fallback_assets_dir)

    def _scan(self, root: Path, prefer: bool) -> None:
        if not root.is_dir():
            return
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    h = hashlib.sha256(p.read_bytes()).hexdigest()
                except OSError:
                    continue
                rel = _nfc(str(p.relative_to(self.cfg.repo_root)))
                # First write wins for interactives roots; we want stable paths.
                if h not in self._index:
                    self._index[h] = rel

    def _scan_snapshot(self, assets_dir: Path) -> None:
        """Snapshot assets are stored as <hash>___<basename>. Index by the
        prefix hash. Stores relative-to-repo-root when possible, else
        absolute path."""
        for p in assets_dir.iterdir():
            if not p.is_file():
                continue
            m = re.match(r"^([0-9a-f]{64})___(.+)$", p.name)
            if not m:
                continue
            h = m.group(1)
            if h not in self._index:
                try:
                    rel = _nfc(str(p.resolve().relative_to(self.cfg.repo_root)))
                except ValueError:
                    rel = _nfc(str(p.resolve()))
                self._index[h] = rel

    def lookup(self, sha256_hex: str) -> str | None:
        return self._index.get(sha256_hex)

    def lookup_by_content(self, content: str) -> str | None:
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return self._index.get(h)


# ---------------------------------------------------------------------------
# Element emitters
# ---------------------------------------------------------------------------

class Emitter:
    def __init__(self, export_dir: Path, cfg: LxpToolsConfig, hash_index: HashIndex):
        self.export_dir = export_dir
        self.cfg = cfg
        self.hash_index = hash_index
        self.acts = [a for a in json.loads((export_dir / "activities.json").read_text())
                     if not a.get("deleted_at")]
        self.elts = [e for e in json.loads((export_dir / "elements.json").read_text())
                     if not e.get("deleted_at")]
        self.acts_by_id = {a["id"]: a for a in self.acts}
        self.children: dict[int | None, list[dict]] = {}
        for a in self.acts:
            self.children.setdefault(a.get("parent_id"), []).append(a)
        for pid in self.children:
            self.children[pid].sort(key=lambda x: (x.get("position") or 0))
        self.elts_by_act: dict[int, list[dict]] = {}
        for e in self.elts:
            self.elts_by_act.setdefault(e["activity_id"], []).append(e)
        for aid in self.elts_by_act:
            self.elts_by_act[aid].sort(key=lambda x: (x.get("position") or 0))

        # Compute local_ids for reflections that are referenced by responseSeed
        self.local_id_for_eid: dict[int, str] = {}
        for e in self.elts:
            if e["type"] != "HLXP_REFLECTION":
                continue
            seeds = (e.get("refs") or {}).get("responseSeed") or []
            for s in seeds:
                src_id = s.get("id")
                if src_id and src_id not in self.local_id_for_eid:
                    title = next((x["data"].get("title") for x in self.elts if x["id"] == src_id), None)
                    slug = (title or f"src_{src_id}").lower().replace(" ", "_").replace("'", "")
                    slug = "".join(c if c.isalnum() or c == "_" else "_" for c in slug)
                    self.local_id_for_eid[src_id] = slug or f"src_{src_id}"

    # -- element renderers --

    def emit_hlxp_html(self, e, indent: int):
        ind = " " * indent
        body = e["data"]["rte"]["content"]
        out = [f"{ind}- element: HLXP_HTML"]
        title = e["data"].get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")
        out.append(f"{ind}  body: {_block_scalar(body, ind + '    ')}")
        return out

    def emit_hlxp_reflection(self, e, indent: int):
        ind = " " * indent
        d = e["data"]
        out = [f"{ind}- element: HLXP_REFLECTION"]
        local = self.local_id_for_eid.get(e["id"])
        if local:
            out.append(f"{ind}  local_id: {_yaml_str(local)}")
        title = d.get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")
        out.append(f"{ind}  shared: {'true' if d.get('sharedElementType') == 'SHARED' else 'false'}")
        io = d.get("inputOutputType", "INPUT_OUTPUT")
        out.append(f"{ind}  input_output: {io}")
        if d.get("minWordCount"):
            out.append(f"{ind}  min_word_count: {d['minWordCount']}")
        seeds = (e.get("refs") or {}).get("responseSeed") or []
        if seeds:
            src_id = seeds[0]["id"]
            slug = self.local_id_for_eid.get(src_id)
            if slug:
                out.append(f"{ind}  seed_from: {_yaml_str(slug)}")
        prompt = (d.get("prompt") or {}).get("content", "")
        out.append(f"{ind}  prompt: {_block_scalar(prompt, ind + '    ')}")
        return out

    def emit_lxp_file_upload(self, e, indent: int):
        ind = " " * indent
        d = e["data"]
        out = [f"{ind}- element: LXP_FILE_UPLOAD"]
        title = d.get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")
        max_fc = d.get("maxFileCount", 1)
        if max_fc != 1:
            out.append(f"{ind}  max_file_count: {max_fc}")
        io = d.get("inputOutputType", "INPUT_OUTPUT")
        if io != "INPUT_OUTPUT":
            out.append(f"{ind}  input_output: {io}")
        prompt = (d.get("prompt") or {}).get("content", "")
        out.append(f"{ind}  prompt: {_block_scalar(prompt, ind + '    ')}")
        return out

    def emit_hlxp_question(self, e, indent: int):
        ind = " " * indent
        d = e["data"]
        is_multi = e["type"] == "HLXP_MULTIPLE_CHOICE_QUESTION"
        out = [f"{ind}- element: {e['type']}"]
        title = d.get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")
        stem = (d.get("question") or {}).get("content", "")
        out.append(f"{ind}  stem: {_yaml_str(stem)}")
        if not is_multi:
            ftype = d.get("feedbackType", "targeted")
            if ftype != "targeted":
                out.append(f"{ind}  feedback_type: {_yaml_str(ftype)}")
            gf = (d.get("generalFeedback") or {}).get("content", "")
            if gf:
                out.append(f"{ind}  general_feedback: {_yaml_str(gf)}")
        correct = d.get("correct")
        correct_set = set(correct) if isinstance(correct, list) else {correct}
        out.append(f"{ind}  answers:")
        for a in d.get("answers", []):
            mark = "true" if a["id"] in correct_set else "false"
            out.append(f"{ind}    - {{label: {_yaml_str(a['content'])}, correct: {mark}}}")
        return out

    def emit_cda_video(self, e, indent: int):
        ind = " " * indent
        out = [f"{ind}- element: CDA_VIDEO"]
        title = e["data"].get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")
        return out

    def emit_lxp_adv_html(self, e, indent: int):
        ind = " " * indent
        d = e["data"]
        out = [f"{ind}- element: LXP_ADV_HTML"]
        title = d.get("title", "")
        if title:
            out.append(f"{ind}  title: {_yaml_str(title)}")

        # Resolve HTML body: try hash → source path; else inline
        body = (d.get("html") or {}).get("codeContent", "") or ""
        if body:
            html_path = self.hash_index.lookup_by_content(body)
            if html_path:
                out.append(f"{ind}  instance_html: {_yaml_str(html_path)}")
            else:
                out.append(f"{ind}  html_inline_body: {_block_scalar(body, ind + '    ')}")

        for kind, key in (("js", "widget_js"), ("css", "widget_css")):
            files = (d.get(kind) or {}).get("files", {}) or {}
            # Pick the first non-inline file entry
            entry = None
            for fk, fv in files.items():
                if fk in ("script-inline", "style-inline"):
                    continue
                entry = fv
                break
            if not entry:
                continue
            url = entry.get("url", "")
            m = re.match(r"storage://repository/assets/([0-9a-f]{64})___", url)
            if not m:
                continue
            h = m.group(1)
            src_path = self.hash_index.lookup(h)
            if src_path:
                out.append(f"{ind}  {key}: {_yaml_str(src_path)}")
            else:
                # Fallback: emit the export's own assets path so round-trip
                # still works (build_export will re-hash and match the byte
                # content). This requires the export tree to remain on disk.
                snapshot_rel = str((self.export_dir / "repository/assets" / f"{h}___{entry.get('filename', 'asset')}").relative_to(self.cfg.repo_root))
                out.append(f"{ind}  {key}: {_yaml_str(snapshot_rel)}  # NOTE: source not found in interactives_dirs")
        return out

    def emit_image(self, e, indent: int):
        ind = " " * indent
        d = e["data"]
        out = [f"{ind}- element: IMAGE"]
        storage_url = (d.get("assets") or {}).get("url", "")
        m = re.match(r"storage://repository/assets/([0-9a-f]{64})___(.+)", storage_url)
        if m:
            h = m.group(1)
            src_path = self.hash_index.lookup(h)
            if src_path:
                out.append(f"{ind}  asset_path: {_yaml_str(src_path)}")
            else:
                snapshot_rel = str((self.export_dir / "repository/assets" / f"{h}___{m.group(2)}").relative_to(self.cfg.repo_root))
                out.append(f"{ind}  asset_path: {_yaml_str(snapshot_rel)}  # NOTE: source not found")
        meta = d.get("meta") or {}
        out.append(f"{ind}  natural_width: {meta.get('width', 0)}")
        out.append(f"{ind}  natural_height: {meta.get('height', 0)}")
        return out

    def emit_element(self, e, indent: int):
        t = e["type"]
        if t == "HLXP_HTML":           return self.emit_hlxp_html(e, indent)
        if t == "HLXP_REFLECTION":     return self.emit_hlxp_reflection(e, indent)
        if t == "LXP_FILE_UPLOAD":     return self.emit_lxp_file_upload(e, indent)
        if t == "HLXP_SINGLE_CHOICE_QUESTION":   return self.emit_hlxp_question(e, indent)
        if t == "HLXP_MULTIPLE_CHOICE_QUESTION": return self.emit_hlxp_question(e, indent)
        if t == "CDA_VIDEO":           return self.emit_cda_video(e, indent)
        if t == "LXP_ADV_HTML":        return self.emit_lxp_adv_html(e, indent)
        if t == "IMAGE":               return self.emit_image(e, indent)
        raise ValueError(f"Unknown element type: {t}")

    # -- container renderers --

    def emit_container(self, act, indent: int):
        ind = " " * indent
        t = act["type"]
        d = act.get("data") or {}
        if t == "INVISIBLE_CONTAINER":
            out = [f"{ind}- container: invisible"]
            elts = self.elts_by_act.get(act["id"], [])
            if not elts:
                out.append(f"{ind}  # NOTE: invisible container with no element children")
                return out
            e = elts[0]
            child_lines = self.emit_element(e, indent + 2)
            assert child_lines[0].lstrip().startswith("- element:"), child_lines[0]
            out.append(ind + "  " + child_lines[0].lstrip()[2:])
            for line in child_lines[1:]:
                out.append(line[2:] if line.startswith("  ") else line)
            return out

        if t == "EXPAND_CONTAINER":
            out = [f"{ind}- container: expand",
                   f"{ind}  title: {_yaml_str(d.get('title', ''))}",
                   f"{ind}  items:"]
            for e in self.elts_by_act.get(act["id"], []):
                out.extend(self.emit_element(e, indent + 4))
            return out

        if t == "ASSIGNMENT":
            out = [f"{ind}- container: assignment",
                   f"{ind}  title: {_yaml_str(d.get('title', ''))}",
                   f"{ind}  items:"]
            for e in self.elts_by_act.get(act["id"], []):
                out.extend(self.emit_element(e, indent + 4))
            return out

        if t == "CEK_QUESTION_SET":
            out = [f"{ind}- container: question_set",
                   f"{ind}  title: {_yaml_str(d.get('title', ''))}"]
            internal = d.get("internal-title")
            if internal and internal != d.get("title"):
                out.append(f"{ind}  internal_title: {_yaml_str(internal)}")
            if d.get("randomizeOrder", False):
                out.append(f"{ind}  randomize_order: true")
            if d.get("displayQuestions", "all") != "all":
                out.append(f"{ind}  display_questions: {_yaml_str(d['displayQuestions'])}")
            if d.get("numberOfAttempts", -1) != -1:
                out.append(f"{ind}  number_of_attempts: {d['numberOfAttempts']}")
            if d.get("displayCorrectAnswers", "never") != "never":
                out.append(f"{ind}  display_correct_answers: {_yaml_str(d['displayCorrectAnswers'])}")
            out.append(f"{ind}  questions:")
            for e in self.elts_by_act.get(act["id"], []):
                out.extend(self.emit_element(e, indent + 4))
            return out

        raise ValueError(f"Unknown container type: {t}")

    def emit_section(self, sec, indent: int):
        ind = " " * indent
        d = sec.get("data") or {}
        out = [
            f"{ind}- title: {_yaml_str(d.get('title', ''))}",
            f"{ind}  locked: {'true' if d.get('locked', True) else 'false'}",
            f"{ind}  completion_required: {'true' if d.get('completionRequired', True) else 'false'}",
            f"{ind}  items:",
        ]
        for child in self.children.get(sec["id"], []):
            out.extend(self.emit_container(child, indent + 4))
        return out

    def emit_page_yaml(self, page: dict, module_pos: int, page_pos: int,
                       source_outline: str | None = None) -> str:
        sc = next(c for c in self.children.get(page["id"], []) if c["type"] == "SECTION_CONTAINER")
        # Derive segment_id from "Segment N:" prefix in page_title; else use position
        page_title = page["data"]["name"]
        seg_match = re.match(r"^Segment\s+(\d+)\s*:", page_title)
        if seg_match:
            seg_num = seg_match.group(1)
            segment_id = f"{module_pos}.{seg_num}"
        else:
            segment_id = f"{module_pos}.{page_pos}"

        out = [
            f"# Ingested from LXP export by lxp_tools/export_to_yaml.py",
            f'segment_id: "{segment_id}"',
        ]
        if source_outline:
            out.append(f"source_outline: {source_outline}")
        out.extend([
            f"lxp_module: {module_pos}",
            f"lxp_segment_number: {page_pos}",
            f"page_title: {_yaml_str(page_title)}",
            "",
            "sections:",
        ])
        for sec in self.children.get(sc["id"], []):
            out.extend(self.emit_section(sec, 2))
            out.append("")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Filename selection
# ---------------------------------------------------------------------------

def _slug_from_title(title: str) -> str:
    title = title.strip().rstrip(".:?!").lower()
    title = re.sub(r"^segment\s+\d+\s*:\s*", "", title)
    title = re.sub(r"[^a-z0-9]+", "_", title).strip("_")
    return title or "untitled"


def filename_for_page(page_title: str, module_pos: int, page_pos: int,
                      existing: dict[str, str] | None = None) -> str:
    """Decide YAML filename for a page.

    `existing`: optional dict of {page_title: filename} read from the target
    directory's existing YAMLs. If a match exists, reuse it.
    """
    if existing and page_title in existing:
        return existing[page_title]
    # Auto-name
    if page_title.lower().startswith("welcome"):
        return "segment_welcome.yaml"
    seg_match = re.match(r"^Segment\s+(\d+)\s*:\s*(.*)$", page_title)
    if seg_match:
        n = seg_match.group(1)
        rest = seg_match.group(2)
        slug = _slug_from_title(rest)
        if "problem set" in rest.lower():
            return f"segment_{module_pos}.{n}_problem_set.yaml"
        return f"segment_{module_pos}.{n}.yaml"
    # Pure title-based slug
    slug = _slug_from_title(page_title)
    return f"segment_{module_pos}_{page_pos}_{slug}.yaml"


def scan_existing_yamls(folder: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not folder.is_dir():
        return out
    for p in folder.glob("segment_*.yaml"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            title = (data or {}).get("page_title")
            if title:
                out[title] = p.name
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def extract_if_tarball(input_path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Return (export_dir, cleanup) — cleanup is the temp dir to keep alive,
    or None if input was already a directory."""
    if input_path.is_dir():
        return input_path, None
    if input_path.suffix in (".tgz", ".gz") or input_path.name.endswith(".tar.gz"):
        td = tempfile.TemporaryDirectory(prefix="lxp_extract_")
        with tarfile.open(input_path, "r:*") as tf:
            tf.extractall(td.name)
        # If the tarball had a single top-level dir, use it. Else use td root.
        entries = list(Path(td.name).iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0], td
        return Path(td.name), td
    raise ValueError(f"input must be an export directory or .tgz: {input_path}")


def main() -> int:
    p = argparse.ArgumentParser(description="Convert an LXP export to build-plan YAMLs.")
    p.add_argument("input", type=Path,
                   help="Path to an extracted export directory or a .tgz tarball.")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, required=True,
                   help="Where to write Module<N>/ subdirs of YAMLs.")
    p.add_argument("--no-preserve-names", action="store_true",
                   help="Don't reuse existing YAML filenames; auto-generate all names.")
    p.add_argument("--use-snapshot-fallback", action="store_true", default=True,
                   help="Index the export's own repository/assets as fallback for asset lookup.")
    args = p.parse_args()

    cfg = load_config(args.config)
    export_dir, _cleanup = extract_if_tarball(args.input.resolve())

    fallback = (export_dir / "repository/assets") if args.use_snapshot_fallback else None
    hash_index = HashIndex(cfg, fallback_assets_dir=fallback)

    em = Emitter(export_dir, cfg, hash_index)

    out_root = Path(args.output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        [a for a in em.acts if a["type"] == "LONG_HLXP_SCHEMA/FOLDER" and a.get("parent_id") is None],
        key=lambda a: a.get("position") or 0,
    )

    total_pages = 0
    for module_pos, folder in enumerate(folders, start=1):
        mod_folder = out_root / cfg.module_folder_pattern.format(n=module_pos)
        mod_folder.mkdir(parents=True, exist_ok=True)
        existing = {} if args.no_preserve_names else scan_existing_yamls(mod_folder)

        pages = [c for c in em.children.get(folder["id"], []) if c["type"] == "LONG_HLXP_SCHEMA/PAGE"]
        pages.sort(key=lambda a: a.get("position") or 0)
        for page_pos, page in enumerate(pages, start=1):
            title = page["data"]["name"]
            fname = filename_for_page(title, module_pos, page_pos, existing)
            # Try to preserve source_outline from existing YAML if present
            source_outline = None
            if existing and title in existing:
                try:
                    with open(mod_folder / existing[title], "r", encoding="utf-8") as f:
                        prior = yaml.safe_load(f) or {}
                    so = prior.get("source_outline")
                    if so:
                        source_outline = so
                except Exception:
                    pass
            yaml_text = em.emit_page_yaml(page, module_pos, page_pos, source_outline=source_outline)
            (mod_folder / fname).write_text(yaml_text)
            total_pages += 1

        print(f"[Module {module_pos}] {len(pages)} pages → {mod_folder.relative_to(cfg.repo_root) if cfg.repo_root in mod_folder.parents else mod_folder}", file=sys.stderr)

    print(f"\nTotal: {total_pages} pages across {len(folders)} modules", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

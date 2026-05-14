#!/usr/bin/env python3
"""Build an LXP export folder from build-plan YAMLs.

YAML → export. Reads per-segment YAMLs (one per LXP page) and writes a
folder with the LXP export shape:

  <out>/repository.json
  <out>/manifest.json
  <out>/activities.json
  <out>/elements.json
  <out>/media-assets.json     (always [])
  <out>/media-folders.json    (always [])
  <out>/media-references.json (always [])
  <out>/repository/assets/    (content-hashed widget/image files, plus
                               per-LXP_ADV_HTML UUID folders with empty
                               script-inline.js + style-inline.css)

The static manifest schema block is loaded from the vendored
`lxp_tools/manifest_schema.json` (originally extracted from the M1
reference export).

Two invocation styles:

  # Build everything declared in the config (one --module per Module<N>/
  # subdir under build_plans_dir, in numeric order).
  python3 lxp_tools/build_export.py --all --out lxp_export/cs20_full

  # Explicit module list (overrides config discovery).
  python3 lxp_tools/build_export.py --out lxp_export/m1 \\
      --module 'Module 1' lxp_build_plans/Module1/segment_*.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from _config import load_config, discover_segment_yamls, LxpToolsConfig


# ---------------------------------------------------------------------------
# ID/UUID minting
# ---------------------------------------------------------------------------

class IdMinter:
    """Mints monotonically increasing integer ids and fresh UUIDv4s."""

    def __init__(self, start: int = 800000):
        self.next_id = start

    def new_id(self) -> int:
        v = self.next_id
        self.next_id += 1
        return v

    @staticmethod
    def new_uuid() -> str:
        return str(uuid.uuid4())


def content_signature(data: dict) -> str:
    """SHA1 of canonical JSON of an element's data field (matches M1 shape)."""
    return hashlib.sha1(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Asset packaging
# ---------------------------------------------------------------------------

class AssetRegistry:
    """Manages content-hashed files and per-element UUID folders under repository/assets/."""

    def __init__(self, out_assets_dir: Path, repo_root: Path):
        self.out = out_assets_dir
        self.repo_root = repo_root
        self.out.mkdir(parents=True, exist_ok=True)
        self._hash_cache: dict[Path, tuple[str, str]] = {}
        self.referenced_paths: list[str] = []

    def add_content_hashed(self, source: Path) -> tuple[str, str]:
        source = Path(source)
        if not source.is_absolute():
            source = self.repo_root / source
        if not source.exists():
            raise FileNotFoundError(f"Asset source not found: {source}")
        if source in self._hash_cache:
            return self._hash_cache[source]
        h = hashlib.sha256(source.read_bytes()).hexdigest()
        basename = source.name
        m = re.match(r"^[0-9a-f]{64}___(.+)$", basename)
        if m:
            basename = m.group(1)
        dest_name = f"{h}___{basename}"
        dest = self.out / dest_name
        if not dest.exists():
            shutil.copy2(source, dest)
        self._hash_cache[source] = (h, dest_name)
        self.referenced_paths.append(f"repository/assets/{dest_name}")
        return h, dest_name

    def mint_uuid_folder_with_inline_files(self) -> str:
        folder_uuid = str(uuid.uuid4())
        folder = self.out / folder_uuid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "script-inline.js").write_text("")
        (folder / "style-inline.css").write_text("")
        self.referenced_paths.append(f"repository/assets/{folder_uuid}/script-inline.js")
        self.referenced_paths.append(f"repository/assets/{folder_uuid}/style-inline.css")
        return folder_uuid


# ---------------------------------------------------------------------------
# Element emitters
# ---------------------------------------------------------------------------

DEFAULT_TIMESTAMP = "2026-05-10T00:00:00.000Z"


def _common_element_fields(*, eid, repo_id, repo_uid, activity_id, activity_uid,
                           etype, position, data, te_version, detached=False):
    return {
        "id": eid,
        "repository_id": repo_id,
        "activity_id": activity_id,
        "uid": IdMinter.new_uuid(),
        "type": etype,
        "position": position,
        "content_id": IdMinter.new_uuid(),
        "content_signature": content_signature(data),
        "data": data,
        "refs": {},
        "linked": False,
        "detached": detached,
        "created_at": DEFAULT_TIMESTAMP,
        "updated_at": DEFAULT_TIMESTAMP,
        "deleted_at": None,
        "meta": {"teVersion": te_version},
        "repository_uid": repo_uid,
        "activity_uid": activity_uid,
    }


def emit_hlxp_html(*, body: str, title: str = "", **common):
    data = {
        "rte": {"assets": {}, "content": body},
        "title": title,
        "width": 12,
        "assets": {},
        "description": "",
    }
    return _common_element_fields(etype="HLXP_HTML", data=data, te_version="1.1.0", **common)


def emit_hlxp_reflection(*, prompt: str, title: str = "", shared: bool = False,
                         input_output: str = "INPUT_OUTPUT",
                         min_word_count: int | None = None,
                         seed_from_reference: bool = False, **common):
    data = {
        "title": title,
        "width": 12,
        "prompt": {"content": prompt},
        "description": "",
        "hasTimeLimit": False,
        "inputOutputType": input_output,
        "timeLimitSeconds": 120,
        "sharedElementType": "SHARED" if shared else "PRIVATE",
    }
    if min_word_count is not None:
        data["minWordCount"] = min_word_count
    if seed_from_reference:
        data["seedFromReference"] = True
    return _common_element_fields(etype="HLXP_REFLECTION", data=data, te_version="1.8.3", **common)


def emit_lxp_file_upload(*, prompt: str, title: str = "", max_file_count: int = 1,
                         input_output: str = "INPUT_OUTPUT", description: str = "",
                         **common):
    data = {
        "title": title,
        "width": 12,
        "prompt": {"content": prompt},
        "description": description,
        "maxFileCount": max_file_count,
        "inputOutputType": input_output,
    }
    return _common_element_fields(etype="LXP_FILE_UPLOAD", data=data, te_version="1.2.3", **common)


def emit_hlxp_question(*, etype: str, stem: str, answers: list[dict], title: str = "",
                       feedback_type: str | None = None, general_feedback: str = "",
                       **common):
    """Emit a SCQ or MCQ. answers: [{label, correct}]."""
    answer_objs = []
    correct_ids = []
    for a in answers:
        aid = str(uuid.uuid4())
        answer_objs.append({"id": aid, "content": a["label"]})
        if a.get("correct"):
            correct_ids.append(aid)

    is_multi = etype == "HLXP_MULTIPLE_CHOICE_QUESTION"
    if is_multi:
        data = {
            "title": title,
            "width": 12,
            "answers": answer_objs,
            "correct": correct_ids,
            "feedback": {"assets": {}, "content": ""},
            "question": {"assets": {}, "content": stem},
            "description": "",
            "feedbackType": "general",
            "targetedFeedback": {},
        }
        te = "1.1.6"
    else:
        if not correct_ids:
            raise ValueError(f"SCQ requires exactly one correct answer; got 0 (title={title!r})")
        if len(correct_ids) > 1:
            raise ValueError(f"SCQ requires exactly one correct answer; got {len(correct_ids)} (title={title!r})")
        ftype = feedback_type or "targeted"
        data = {
            "title": title,
            "width": 12,
            "answers": answer_objs,
            "correct": correct_ids[0],
            "feedback": {a["id"]: {"assets": {}, "content": ""} for a in answer_objs},
            "question": {"assets": {}, "content": stem},
            "description": "",
            "feedbackType": ftype,
            "generalFeedback": {"assets": {}, "content": general_feedback},
        }
        te = "1.2.2"
    return _common_element_fields(etype=etype, data=data, te_version=te, **common)


def emit_cda_video(*, title: str = "", **common):
    data = {
        "title": title,
        "width": 12,
        "assets": {},
        "upload": {
            "id": "", "url": "", "status": "waiting",
            "timeout": 259200,
            "validUntil": int(time.time() * 1000) + 259200000,
        },
        "assetId": "",
        "captions": {"key": "", "url": "", "name": "", "size": 0, "publicUrl": ""},
        "duration": 0,
        "playback": {"id": "", "url": "", "token": "", "audioUrl": ""},
        "thumbnail": {"key": "", "url": "", "name": "", "size": 0, "publicUrl": ""},
        "playbackId": "",
        "audioOnly": False,
        "transcript": {"key": "", "url": "", "name": "", "size": 0, "publicUrl": ""},
        "aspectRatio": "",
        "eadCaptions": {"key": "", "url": "", "name": "", "size": 0, "publicUrl": ""},
        "uploadAudio": {
            "id": "", "url": "", "status": "waiting",
            "timeout": 259200,
            "validUntil": int(time.time() * 1000) + 259200000,
        },
        "assetFilename": "",
        "audiodescription": {"key": "", "url": "", "name": "", "size": 0, "publicUrl": ""},
        "defaultThumbnail": "",
        "disableScrubbing": False,
        "eadCaptionsComparator": {
            "eadExists": False, "isValidPair": False, "originalExists": False
        },
        "audiodescriptionAssetId": "",
        "audiodescriptionDuration": 0,
        "audiodescriptionFilename": "",
        "audiodescriptionPlayback": {"id": "", "url": "", "token": ""},
        "audiodescriptionAudioOnly": True,
        "audiodescriptionPlaybackId": "",
        "audiodescriptionAspectRatio": "",
        "audiodescriptionUploadStatus": "",
    }
    return _common_element_fields(etype="CDA_VIDEO", data=data, te_version="2.6.4", **common)


def _adv_html_file_entry(*, id_str: str, local_key: str, asset_key: str,
                          filename: str, url: str, size: int | None) -> dict:
    entry = {
        "id": id_str, "url": url, "dedupe": False,
        "assetKey": asset_key, "filename": filename,
        "localKey": local_key, "publicUrl": "",
    }
    if size is not None:
        entry["size"] = size
        entry["sizeDisplay"] = _size_display(size)
    else:
        entry["sizeDisplay"] = "0"
    return entry


def _size_display(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.2f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


def emit_lxp_adv_html(*, html_inline_body: str = "", instance_html_path: str | None = None,
                      widget_js: str | None = None, widget_css: str | None = None,
                      title: str = "", assets: AssetRegistry, repo_root: Path, **common):
    if instance_html_path and not html_inline_body:
        path = Path(instance_html_path)
        if not path.is_absolute():
            path = repo_root / path
        html_inline_body = path.read_text(encoding="utf-8")

    css_files: dict[str, dict] = {}
    js_files: dict[str, dict] = {}

    if widget_css:
        h, fname = assets.add_content_hashed(Path(widget_css))
        src = Path(widget_css) if Path(widget_css).is_absolute() else repo_root / widget_css
        size = src.stat().st_size
        basename = re.sub(r"^[0-9a-f]{64}___", "", src.name)
        css_files["style_0"] = _adv_html_file_entry(
            id_str="style_0", local_key="style_0",
            asset_key="css.files.style_0.publicUrl",
            filename=basename, url=f"storage://repository/assets/{fname}", size=size,
        )

    if widget_js:
        h, fname = assets.add_content_hashed(Path(widget_js))
        src = Path(widget_js) if Path(widget_js).is_absolute() else repo_root / widget_js
        size = src.stat().st_size
        basename = re.sub(r"^[0-9a-f]{64}___", "", src.name)
        js_files["script_0"] = _adv_html_file_entry(
            id_str="script_0", local_key="script_0",
            asset_key="js.files.script_0.publicUrl",
            filename=basename, url=f"storage://repository/assets/{fname}", size=size,
        )

    assets_mirror = {}
    for v in css_files.values():
        assets_mirror[v["assetKey"]] = v["url"]
    for v in js_files.values():
        assets_mirror[v["assetKey"]] = v["url"]

    data = {
        "js": {"files": js_files, "codeContent": ""},
        "css": {"files": css_files, "codeContent": ""},
        "html": {"files": {}, "codeContent": html_inline_body},
        "media": {"files": {}},
        "title": title,
        "width": 12,
        "assets": assets_mirror,
        "description": "",
    }
    return _common_element_fields(etype="LXP_ADV_HTML", data=data, te_version="1.3.0", detached=False, **common)


def emit_image(*, asset_path: str, natural_width: int, natural_height: int,
               assets: AssetRegistry, **common):
    h, fname = assets.add_content_hashed(Path(asset_path))
    data = {
        "alt": "",
        "url": "",
        "meta": {"width": natural_width, "height": natural_height},
        "width": 12,
        "assets": {"url": f"storage://repository/assets/{fname}"},
        "caption": "",
    }
    return _common_element_fields(etype="IMAGE", data=data, te_version="1.0.10", detached=False, **common)


# ---------------------------------------------------------------------------
# Activity emitters
# ---------------------------------------------------------------------------

def _common_activity_fields(*, ids: IdMinter, atype: str, parent_id: int | None,
                            parent_uid: str | None, position) -> dict:
    return {
        "id": ids.new_id(),
        "uid": IdMinter.new_uuid(),
        "type": atype,
        "parent_id": parent_id,
        "parent_uid": parent_uid,
        "position": position,
        "data": {},
        "refs": {},
        "detached": False,
        "deleted_at": None,
        "created_at": DEFAULT_TIMESTAMP,
        "updated_at": DEFAULT_TIMESTAMP,
        "modified_at": DEFAULT_TIMESTAMP,
        "published_at": DEFAULT_TIMESTAMP,
    }


def emit_section(*, ids, parent_id, parent_uid, position, title,
                 locked: bool = True, completion_required: bool = True) -> dict:
    a = _common_activity_fields(ids=ids, atype="SECTION",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"title": title, "locked": locked, "completionRequired": completion_required}
    return a


def emit_invisible(*, ids, parent_id, parent_uid, position) -> dict:
    a = _common_activity_fields(ids=ids, atype="INVISIBLE_CONTAINER",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"width": 12}
    return a


def emit_expand(*, ids, parent_id, parent_uid, position, title) -> dict:
    a = _common_activity_fields(ids=ids, atype="EXPAND_CONTAINER",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"title": title}
    return a


def emit_assignment(*, ids, parent_id, parent_uid, position, title) -> dict:
    a = _common_activity_fields(ids=ids, atype="ASSIGNMENT",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"title": title}
    return a


def emit_question_set(*, ids, parent_id, parent_uid, position,
                      title, internal_title=None,
                      randomize_order=False, display_questions="all",
                      number_of_attempts=-1, display_correct_answers="never") -> dict:
    a = _common_activity_fields(ids=ids, atype="CEK_QUESTION_SET",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {
        "title": title,
        "internal-title": internal_title or title,
        "randomizeOrder": randomize_order,
        "displayQuestions": display_questions,
        "numberOfAttempts": number_of_attempts,
        "displayCorrectAnswers": display_correct_answers,
    }
    return a


def emit_section_container(*, ids, parent_id, parent_uid, position=1) -> dict:
    return _common_activity_fields(ids=ids, atype="SECTION_CONTAINER",
                                   parent_id=parent_id, parent_uid=parent_uid, position=position)


def emit_page(*, ids, parent_id, parent_uid, position, name) -> dict:
    a = _common_activity_fields(ids=ids, atype="LONG_HLXP_SCHEMA/PAGE",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"name": name}
    return a


def emit_folder(*, ids, parent_id, parent_uid, position, name) -> dict:
    a = _common_activity_fields(ids=ids, atype="LONG_HLXP_SCHEMA/FOLDER",
                                parent_id=parent_id, parent_uid=parent_uid, position=position)
    a["data"] = {"name": name}
    return a


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

class CourseBuilder:
    def __init__(self, *, course_name: str, course_color: str, repo_id: int,
                 repo_uid: str, out_dir: Path, repo_root: Path,
                 manifest_schema: dict):
        self.course_name = course_name
        self.course_color = course_color
        self.repo_id = repo_id
        self.repo_uid = repo_uid
        self.out_dir = out_dir
        self.repo_root = repo_root
        self.manifest_schema = manifest_schema
        self.ids = IdMinter()
        self.activities: list[dict] = []
        self.elements: list[dict] = []
        self.assets = AssetRegistry(out_dir / "repository/assets", repo_root=repo_root)
        self.local_id_registry: dict[str, dict] = {}
        self.deferred_seed_refs: list[tuple[dict, str]] = []
        self._current_page_id: int | None = None
        self._current_page_uid: str | None = None

    def _wrap_with_repo(self, act: dict) -> dict:
        act["repository_id"] = self.repo_id
        act["repository_uid"] = self.repo_uid
        return act

    def _attach_element(self, *, element_emitter, activity_id: int, activity_uid: str,
                        position: int, **kwargs) -> dict:
        eid = self.ids.new_id()
        el = element_emitter(
            eid=eid, repo_id=self.repo_id, repo_uid=self.repo_uid,
            activity_id=activity_id, activity_uid=activity_uid,
            position=position, **kwargs,
        )
        self.elements.append(el)
        return el

    def add_module(self, *, module_position: int, module_name: str,
                   segment_yamls: list[Path]) -> None:
        folder = self._wrap_with_repo(emit_folder(
            ids=self.ids, parent_id=None, parent_uid=None,
            position=module_position, name=module_name
        ))
        self.activities.append(folder)
        for i, segyaml in enumerate(segment_yamls, start=1):
            self._add_segment_page(folder=folder, position=i, segyaml=segyaml)

    def _add_segment_page(self, *, folder: dict, position: int, segyaml: Path) -> None:
        with open(segyaml, "r", encoding="utf-8") as f:
            plan = yaml.safe_load(f)
        page = self._wrap_with_repo(emit_page(
            ids=self.ids, parent_id=folder["id"], parent_uid=folder["uid"],
            position=position, name=plan["page_title"],
        ))
        self.activities.append(page)
        prev_page_id, prev_page_uid = self._current_page_id, self._current_page_uid
        self._current_page_id, self._current_page_uid = page["id"], page["uid"]
        try:
            sc = self._wrap_with_repo(emit_section_container(
                ids=self.ids, parent_id=page["id"], parent_uid=page["uid"], position=1,
            ))
            self.activities.append(sc)
            for sec_pos, sec in enumerate(plan["sections"], start=1):
                self._add_section(parent_act=sc, position=sec_pos, sec=sec)
        finally:
            self._current_page_id, self._current_page_uid = prev_page_id, prev_page_uid

    def _add_section(self, *, parent_act: dict, position: int, sec: dict) -> None:
        section = self._wrap_with_repo(emit_section(
            ids=self.ids, parent_id=parent_act["id"], parent_uid=parent_act["uid"],
            position=position, title=sec["title"],
            locked=sec.get("locked", True),
            completion_required=sec.get("completion_required", True),
        ))
        self.activities.append(section)
        for item_pos, item in enumerate(sec.get("items", []), start=1):
            self._add_item(parent_act=section, position=item_pos, item=item)

    def _add_item(self, *, parent_act: dict, position: int, item: dict) -> None:
        ctype = item["container"]
        if ctype == "invisible":
            inv = self._wrap_with_repo(emit_invisible(
                ids=self.ids, parent_id=parent_act["id"], parent_uid=parent_act["uid"],
                position=position,
            ))
            self.activities.append(inv)
            self._add_element(parent_act=inv, position=0, item=item)
        elif ctype == "expand":
            exp = self._wrap_with_repo(emit_expand(
                ids=self.ids, parent_id=parent_act["id"], parent_uid=parent_act["uid"],
                position=position, title=item["title"],
            ))
            self.activities.append(exp)
            for sub_pos, sub in enumerate(item.get("items", []), start=1):
                self._add_element(parent_act=exp, position=sub_pos, item=sub)
        elif ctype == "assignment":
            asn = self._wrap_with_repo(emit_assignment(
                ids=self.ids, parent_id=parent_act["id"], parent_uid=parent_act["uid"],
                position=position, title=item["title"],
            ))
            self.activities.append(asn)
            for sub_pos, sub in enumerate(item.get("items", []), start=1):
                self._add_element(parent_act=asn, position=sub_pos, item=sub)
        elif ctype == "question_set":
            qs = self._wrap_with_repo(emit_question_set(
                ids=self.ids, parent_id=parent_act["id"], parent_uid=parent_act["uid"],
                position=position, title=item["title"],
                internal_title=item.get("internal_title"),
                randomize_order=item.get("randomize_order", False),
                display_questions=item.get("display_questions", "all"),
                number_of_attempts=item.get("number_of_attempts", -1),
                display_correct_answers=item.get("display_correct_answers", "never"),
            ))
            self.activities.append(qs)
            for sub_pos, sub in enumerate(item.get("questions", []), start=1):
                self._add_element(parent_act=qs, position=sub_pos, item=sub)
        else:
            raise ValueError(f"Unknown container type: {ctype}")

    def _add_element(self, *, parent_act: dict, position: int, item: dict) -> None:
        etype = item["element"]
        common = dict(
            activity_id=parent_act["id"], activity_uid=parent_act["uid"], position=position,
        )
        if etype == "HLXP_HTML":
            self._attach_element(
                element_emitter=emit_hlxp_html,
                body=item.get("body", ""),
                title=item.get("title", ""),
                **common,
            )
        elif etype == "HLXP_REFLECTION":
            el = self._attach_element(
                element_emitter=emit_hlxp_reflection,
                prompt=item["prompt"],
                title=item.get("title", ""),
                shared=item.get("shared", False),
                input_output=item.get("input_output", "INPUT_OUTPUT"),
                min_word_count=item.get("min_word_count"),
                seed_from_reference=item.get("seed_from_reference", False) or bool(item.get("seed_from")),
                **common,
            )
            local_id = item.get("local_id")
            if local_id:
                self.local_id_registry[local_id] = {
                    "element_id": el["id"],
                    "element_uid": el["uid"],
                    "container_id": parent_act["id"],
                    "container_uid": parent_act["uid"],
                    "page_id": self._current_page_id,
                    "page_uid": self._current_page_uid,
                }
            seed_from = item.get("seed_from")
            if seed_from:
                self.deferred_seed_refs.append((el, seed_from))
        elif etype in ("HLXP_SINGLE_CHOICE_QUESTION", "HLXP_MULTIPLE_CHOICE_QUESTION"):
            self._attach_element(
                element_emitter=emit_hlxp_question,
                etype=etype,
                stem=item["stem"],
                answers=item["answers"],
                title=item.get("title", ""),
                feedback_type=item.get("feedback_type"),
                general_feedback=item.get("general_feedback", ""),
                **common,
            )
        elif etype == "LXP_FILE_UPLOAD":
            self._attach_element(
                element_emitter=emit_lxp_file_upload,
                prompt=item["prompt"],
                title=item.get("title", ""),
                max_file_count=item.get("max_file_count", 1),
                input_output=item.get("input_output", "INPUT_OUTPUT"),
                description=item.get("description", ""),
                **common,
            )
        elif etype == "CDA_VIDEO":
            self._attach_element(
                element_emitter=emit_cda_video,
                title=item.get("title", ""),
                **common,
            )
        elif etype == "LXP_ADV_HTML":
            self._attach_element(
                element_emitter=emit_lxp_adv_html,
                html_inline_body=item.get("html_inline_body", ""),
                instance_html_path=item.get("instance_html"),
                widget_js=item.get("widget_js"),
                widget_css=item.get("widget_css"),
                title=item.get("title", ""),
                assets=self.assets,
                repo_root=self.repo_root,
                **common,
            )
        elif etype == "IMAGE":
            self._attach_element(
                element_emitter=emit_image,
                asset_path=item["asset_path"],
                natural_width=item["natural_width"],
                natural_height=item["natural_height"],
                assets=self.assets,
                **common,
            )
        else:
            raise ValueError(f"Unknown element type: {etype}")

    def finalize(self) -> None:
        for elt, source_local_id in self.deferred_seed_refs:
            target = self.local_id_registry.get(source_local_id)
            if target is None:
                raise ValueError(
                    f"Element with local_id={source_local_id!r} not found "
                    f"(referenced via seed_from on element id={elt['id']})"
                )
            elt["refs"]["responseSeed"] = [{
                "id": target["element_id"],
                "uid": target["element_uid"],
                "outlineId": target["page_id"],
                "outlineUid": target["page_uid"],
                "containerId": target["container_id"],
                "containerUid": target["container_uid"],
            }]

    def write(self) -> None:
        self.finalize()
        (self.out_dir / "activities.json").write_text(
            json.dumps(self.activities, ensure_ascii=False)
        )
        (self.out_dir / "elements.json").write_text(
            json.dumps(self.elements, ensure_ascii=False)
        )
        repo_data = {
            "id": self.repo_id,
            "uid": self.repo_uid,
            "schema": "LONG_HLXP_SCHEMA",
            "name": self.course_name,
            "description": self.course_name,
            "data": {
                "color": self.course_color,
                "defaultLock": True,
                "defaultRequireContent": False,
            },
            "created_at": "2026-05-10T00:00:00.000Z",
            "updated_at": "2026-05-10T00:00:00.000Z",
            "deleted_at": None,
            "has_unpublished_changes": False,
        }
        (self.out_dir / "repository.json").write_text(json.dumps(repo_data, ensure_ascii=False))
        manifest = {
            "assets": self.assets.referenced_paths,
            "schema": self.manifest_schema,
            "date": "2026-05-10T00:00:00.000Z",
        }
        (self.out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
        for n in ("media-assets.json", "media-folders.json", "media-references.json"):
            (self.out_dir / n).write_text("[]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Build an LXP export folder from build-plan YAMLs.")
    p.add_argument("--config", type=Path, default=None,
                   help="Path to lxp_tools.config.yaml (default: <project_root>/lxp_tools.config.yaml, then lxp_tools/lxp_tools.config.yaml)")
    p.add_argument("--course-name", default=None,
                   help="Override course_title from config.")
    p.add_argument("--course-color", default=None,
                   help="Override course_color from config.")
    p.add_argument("--repo-id", type=int, default=None)
    p.add_argument("--repo-uid", default=None,
                   help="UUID for repository.uid; default minted fresh")
    p.add_argument("--out", required=True, type=Path,
                   help="Output directory (will be removed if it exists).")
    p.add_argument("--all", action="store_true",
                   help="Build every Module<N>/ subdir under build_plans_dir, in order.")
    p.add_argument("--module", action="append", default=None, nargs="+",
                   metavar=("NAME", "SEGMENT_YAML"),
                   help="--module 'Module 1' seg1.yaml seg2.yaml ... (repeat). Overrides --all.")
    args = p.parse_args()

    cfg = load_config(args.config)
    course_name = args.course_name or cfg.course_title
    course_color = args.course_color or cfg.course_color
    repo_id = args.repo_id if args.repo_id is not None else cfg.repo_id

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    manifest_schema = json.loads(cfg.manifest_schema_path.read_text())
    repo_uid = args.repo_uid or str(uuid.uuid4())
    cb = CourseBuilder(
        course_name=course_name, course_color=course_color,
        repo_id=repo_id, repo_uid=repo_uid,
        out_dir=out, repo_root=cfg.repo_root,
        manifest_schema=manifest_schema,
    )

    if args.module:
        for i, mod_args in enumerate(args.module, start=1):
            mod_name = mod_args[0]
            seg_paths = [Path(p) for p in mod_args[1:]]
            if not seg_paths:
                print(f"warning: module {mod_name!r} has no segments", file=sys.stderr)
            cb.add_module(module_position=i, module_name=mod_name, segment_yamls=seg_paths)
    elif args.all:
        modules = discover_segment_yamls(cfg)
        if not modules:
            print(f"error: no Module<N>/ subdirs found under {cfg.build_plans_dir}", file=sys.stderr)
            return 1
        for n, mod_name, seg_paths in modules:
            if not seg_paths:
                print(f"warning: {mod_name} has no segment YAMLs", file=sys.stderr)
            cb.add_module(module_position=n, module_name=mod_name, segment_yamls=seg_paths)
    else:
        print("error: must pass --all or one or more --module", file=sys.stderr)
        return 2

    cb.write()
    print(f"Wrote LXP export to {out}", file=sys.stderr)
    print(f"  activities: {len(cb.activities)}", file=sys.stderr)
    print(f"  elements:   {len(cb.elements)}", file=sys.stderr)
    print(f"  assets:     {len(cb.assets.referenced_paths)} references", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

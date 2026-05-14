#!/usr/bin/env python3
"""Normalized round-trip diff between two LXP export folders.

Walks the activity tree on both sides, matches activities position-by-position
under each parent, then compares activity `data` and attached elements.

Fields ignored on both sides (always differ legitimately):
  - id (int), uid (UUID), repository_uid, content_id, activity_uid
  - content_signature (sha1 over data)
  - created_at, updated_at, deleted_at (we filter deleted_at != null up front)
  - position fractions (we compare rank-among-siblings instead)
  - detached
  - linked, refs (always {} / False on emit)
  - CDA_VIDEO upload, uploadAudio, assetId, playbackId, duration, aspectRatio,
    captions, thumbnail, transcript, eadCaptions, eadCaptionsComparator,
    audiodescription* (Mux placeholders are always different)
  - LXP_ADV_HTML file entries: publicUrl (presigned S3, time-bounded),
    size/sizeDisplay (rebased)
  - storage:// URLs: the `<sha256>___` filename prefix is normalized off
    (filename hashes diverge when source bytes change between builds)

Usage:
  python3 lxp_tools/diff_export.py <generated_dir> <reference_dir>
  python3 lxp_tools/diff_export.py <a> <b> --limit-to-folder 'Module 1'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

ID_FIELDS = {"id", "uid", "repository_uid", "repository_id",
             "content_id", "activity_uid", "activity_id",
             "content_signature", "created_at", "updated_at", "deleted_at",
             "detached", "linked", "refs", "position",
             "uploaded_at"}

CDA_VIDEO_NORMALIZE = {
    "upload", "uploadAudio", "assetId", "playbackId", "duration", "aspectRatio",
    "captions", "thumbnail", "transcript", "eadCaptions", "eadCaptionsComparator",
    "audiodescription", "audiodescriptionAssetId", "audiodescriptionDuration",
    "audiodescriptionFilename", "audiodescriptionPlayback",
    "audiodescriptionAudioOnly", "audiodescriptionPlaybackId",
    "audiodescriptionAspectRatio", "audiodescriptionUploadStatus",
    "assetFilename", "audioOnly", "defaultThumbnail", "disableScrubbing",
}

STORAGE_HASH_RE = re.compile(
    r"(storage://repository/assets/)([0-9a-f]{64})___([^\"]+)"
)
STORAGE_UUID_RE = re.compile(
    r"(storage://repository/assets/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
)
PRESIGNED_S3_RE = re.compile(
    r"https://lxp-prod-tailor-[^?]+\?[^\"]*"
)


def normalize_storage_urls(s: str) -> str:
    s = STORAGE_HASH_RE.sub(r"\1__HASH__/\3", s)
    s = STORAGE_UUID_RE.sub(r"\1__UUID__/", s)
    s = PRESIGNED_S3_RE.sub("__PRESIGNED_S3__", s)
    return s


def normalize_html(s: str) -> str:
    """Collapse whitespace between tags and trim trailing whitespace."""
    if not isinstance(s, str) or "<" not in s:
        return s
    s = re.sub(r">\s+<", "><", s)
    s = s.rstrip("\n")
    return s


OPTIONAL_DEFAULTS = {
    "HLXP_REFLECTION": {
        "hasTimeLimit": False,
        "timeLimitSeconds": 120,
        "isSharedElement": False,
        "seedFromReference": False,
        "minWordCount": 0,
    },
    "HLXP_SINGLE_CHOICE_QUESTION": {},
    "HLXP_MULTIPLE_CHOICE_QUESTION": {},
}


def normalize_value(v):
    if isinstance(v, str):
        return unicodedata.normalize("NFC", normalize_html(normalize_storage_urls(v)))
    if isinstance(v, list):
        return [normalize_value(x) for x in v]
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if k in ID_FIELDS:
                continue
            out[k] = normalize_value(val)
        return out
    return v


def apply_optional_defaults(etype: str, ga: dict, ra: dict) -> tuple[dict, dict]:
    ga = json.loads(json.dumps(ga))
    ra = json.loads(json.dumps(ra))
    defaults = OPTIONAL_DEFAULTS.get(etype, {})
    for k, dflt in defaults.items():
        if k in ga and k not in ra and ga[k] == dflt:
            del ga[k]
        elif k in ra and k not in ga and ra[k] == dflt:
            del ra[k]
    return ga, ra


def normalize_element_data(etype: str, data: dict) -> dict:
    data = json.loads(json.dumps(data))

    if etype == "CDA_VIDEO":
        for k in CDA_VIDEO_NORMALIZE:
            data.pop(k, None)
    if etype == "IMAGE":
        data.pop("url", None)
    if etype == "LXP_ADV_HTML":
        for kind in ("html", "css", "js"):
            files = (data.get(kind, {}).get("files", {}) or {})
            files.pop("style-inline", None)
            files.pop("script-inline", None)
            for fk, fv in files.items():
                if isinstance(fv, dict):
                    fv.pop("publicUrl", None)
                    fv.pop("size", None)
                    fv.pop("sizeDisplay", None)
                    if isinstance(fv.get("filename"), str):
                        fv["filename"] = re.sub(r"^[0-9a-f]{64}___", "", fv["filename"])
        assets = data.get("assets", {})
        if isinstance(assets, dict):
            for k in list(assets):
                if k.endswith(".style-inline.publicUrl") or k.endswith(".script-inline.publicUrl"):
                    assets.pop(k)
    if etype in ("HLXP_SINGLE_CHOICE_QUESTION", "HLXP_MULTIPLE_CHOICE_QUESTION"):
        answers = data.get("answers", [])
        id_map = {a["id"]: f"__A{i}" for i, a in enumerate(answers)}
        for a in answers:
            a["id"] = id_map[a["id"]]
        if isinstance(data.get("correct"), str):
            data["correct"] = id_map.get(data["correct"], data["correct"])
        elif isinstance(data.get("correct"), list):
            data["correct"] = [id_map.get(x, x) for x in data["correct"]]
        for fk in ("feedback", "targetedFeedback"):
            if fk in data and isinstance(data[fk], dict):
                new = {}
                for k, v in data[fk].items():
                    mapped = id_map.get(k)
                    if mapped is not None:
                        new[mapped] = v
                    elif k in ("assets", "content"):
                        new[k] = v
                if all(isinstance(v, dict) and v.get("content", "") == ""
                       for v in new.values() if isinstance(v, dict) and "content" in v):
                    new = {}
                data[fk] = new

    return normalize_value(data)


def load_export(d: Path) -> tuple[list, list]:
    acts = json.loads((d / "activities.json").read_text())
    elts = json.loads((d / "elements.json").read_text())
    acts = [a for a in acts if not a.get("deleted_at")]
    elts = [e for e in elts if not e.get("deleted_at")]
    return acts, elts


def build_index(acts: list, elts: list):
    children: dict = {}
    for a in acts:
        children.setdefault(a.get("parent_id"), []).append(a)
    for pid in children:
        children[pid].sort(key=lambda x: x.get("position") or 0)
    elts_by_act: dict = {}
    for e in elts:
        elts_by_act.setdefault(e["activity_id"], []).append(e)
    for aid in elts_by_act:
        elts_by_act[aid].sort(key=lambda x: x.get("position") or 0)
    return children, elts_by_act


def diff_dict(path: str, a: dict, b: dict, diffs: list[str]):
    keys = sorted(set(a) | set(b))
    for k in keys:
        if k not in a:
            diffs.append(f"{path}.{k}: only in REFERENCE = {b[k]!r}")
            continue
        if k not in b:
            diffs.append(f"{path}.{k}: only in GENERATED = {a[k]!r}")
            continue
        diff_value(f"{path}.{k}", a[k], b[k], diffs)


def diff_value(path: str, a, b, diffs: list[str]):
    if isinstance(a, dict) and isinstance(b, dict):
        diff_dict(path, a, b, diffs)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: list length differs gen={len(a)} ref={len(b)}")
            return
        for i, (ai, bi) in enumerate(zip(a, b)):
            diff_value(f"{path}[{i}]", ai, bi, diffs)
        return
    if a != b:
        sa = repr(a)[:200] + ("…" if len(repr(a)) > 200 else "")
        sb = repr(b)[:200] + ("…" if len(repr(b)) > 200 else "")
        diffs.append(f"{path}: {sa}  !=  {sb}")


def walk_diff(gen_root_ids: list[int], ref_root_ids: list[int],
              gen_index, ref_index, gen_acts_all: list[dict], ref_acts_all: list[dict],
              diffs: list[str], path: str = ""):
    gen_children_map, gen_elts = gen_index
    ref_children_map, ref_elts = ref_index

    if len(gen_root_ids) != len(ref_root_ids):
        diffs.append(f"{path}: child count differs gen={len(gen_root_ids)} ref={len(ref_root_ids)}")
        return

    for i, (gid, rid) in enumerate(zip(gen_root_ids, ref_root_ids)):
        ga = next(a for a in gen_acts_all if a["id"] == gid)
        ra = next(a for a in ref_acts_all if a["id"] == rid)
        sub_path = f"{path}/{ga['type']}#{i}"
        if ga["type"] != ra["type"]:
            diffs.append(f"{sub_path}: type differs gen={ga['type']} ref={ra['type']}")
            continue
        gdata = normalize_value(ga.get("data") or {})
        rdata = normalize_value(ra.get("data") or {})
        if gdata != rdata:
            diff_dict(sub_path + ".data", gdata, rdata, diffs)
        ge = gen_elts.get(gid, [])
        re_ = ref_elts.get(rid, [])
        if len(ge) != len(re_):
            diffs.append(f"{sub_path}: element count differs gen={len(ge)} ref={len(re_)}")
        else:
            for j, (gel, rel) in enumerate(zip(ge, re_)):
                ep = f"{sub_path}/elt#{j}"
                if gel["type"] != rel["type"]:
                    diffs.append(f"{ep}: element type differs gen={gel['type']} ref={rel['type']}")
                    continue
                gd = normalize_element_data(gel["type"], gel["data"])
                rd = normalize_element_data(rel["type"], rel["data"])
                gd, rd = apply_optional_defaults(gel["type"], gd, rd)
                if gd != rd:
                    diff_dict(ep + ".data", gd, rd, diffs)
        gc = [c["id"] for c in gen_children_map.get(gid, [])]
        rc = [c["id"] for c in ref_children_map.get(rid, [])]
        walk_diff(gc, rc, gen_index, ref_index, gen_acts_all, ref_acts_all, diffs, sub_path)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("generated", type=Path)
    p.add_argument("reference", type=Path)
    p.add_argument("--limit-to-folder", type=str, default=None,
                   help="Reference folder name to restrict comparison to (e.g. 'Module 1')")
    p.add_argument("--max-diffs", type=int, default=0,
                   help="If >0, stop printing after this many diffs (still counts total).")
    args = p.parse_args()

    gen_acts, gen_elts = load_export(args.generated)
    ref_acts, ref_elts = load_export(args.reference)
    gen_idx = build_index(gen_acts, gen_elts)
    ref_idx = build_index(ref_acts, ref_elts)

    gen_roots = [a for a in gen_acts if a.get("parent_id") is None
                 and a["type"] == "LONG_HLXP_SCHEMA/FOLDER"]
    ref_roots = [a for a in ref_acts if a.get("parent_id") is None
                 and a["type"] == "LONG_HLXP_SCHEMA/FOLDER"]
    # LXP exports don't guarantee insertion order in activities.json — sort by
    # position so we pair folders consistently between sides.
    gen_roots.sort(key=lambda a: a.get("position") or 0)
    ref_roots.sort(key=lambda a: a.get("position") or 0)
    if args.limit_to_folder:
        gen_roots = [a for a in gen_roots if (a.get("data") or {}).get("name") == args.limit_to_folder]
        ref_roots = [a for a in ref_roots if (a.get("data") or {}).get("name") == args.limit_to_folder]

    diffs: list[str] = []
    if len(gen_roots) != len(ref_roots):
        diffs.append(f"/: root FOLDER count differs gen={len(gen_roots)} ref={len(ref_roots)}")
        for d in diffs:
            print(d)
        return 1

    for gf, rf in zip(gen_roots, ref_roots):
        gen_pages = gen_idx[0].get(gf["id"], [])
        ref_pages = ref_idx[0].get(rf["id"], [])
        gen_page_names = {(p.get("data") or {}).get("name", "") for p in gen_pages}
        ref_pages_filtered = [p for p in ref_pages if (p.get("data") or {}).get("name", "") in gen_page_names]
        if len(gen_pages) != len(ref_pages_filtered):
            diffs.append(f"FOLDER '{(gf['data'] or {}).get('name')}': page name mismatch "
                         f"gen={[p['data'].get('name') for p in gen_pages]} "
                         f"ref={[p['data'].get('name') for p in ref_pages_filtered]}")
            continue
        for gp in gen_pages:
            name = (gp.get("data") or {}).get("name", "")
            rp = next(p for p in ref_pages_filtered if (p.get("data") or {}).get("name", "") == name)
            sub_path = f"/{gf['type']}/PAGE['{name}']"
            diff_dict(sub_path + ".data",
                      normalize_value(gp.get("data") or {}),
                      normalize_value(rp.get("data") or {}),
                      diffs)
            gc = [c["id"] for c in gen_idx[0].get(gp["id"], [])]
            rc = [c["id"] for c in ref_idx[0].get(rp["id"], [])]
            walk_diff(gc, rc, gen_idx, ref_idx, gen_acts, ref_acts, diffs, sub_path)

    if not diffs:
        print("Diff is clean (modulo normalized fields).")
        return 0
    print(f"Found {len(diffs)} differences:\n")
    limit = args.max_diffs if args.max_diffs > 0 else len(diffs)
    for d in diffs[:limit]:
        print(d)
    if len(diffs) > limit:
        print(f"\n... ({len(diffs) - limit} more not shown; pass --max-diffs 0 for all)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

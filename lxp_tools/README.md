# lxp_tools

A round-trip toolkit for Harvard LXP course exports.

The toolkit converts between two representations:

- **LXP export** — the tarball you download from "Export Course" in LXP, or upload via "Import Course".
- **Build-plan YAMLs** — one YAML per page (segment), checked into the repo at `<build_plans_dir>/Module<N>/segment_*.yaml`. Human-readable, diffable, the source of truth.

The pipeline has three CLI scripts:

```
LXP export tarball ──[export_to_yaml.py]──► build-plan YAMLs
                                                  │
                                                  ▼ edits in YAML
                                                  │
   LXP import ◄──[build_export.py]── tarball ◄── YAMLs
                          ▲
                          │  validates round-trip
                [diff_export.py]
```

**Round-trip invariant:** `export_to_yaml(export) → build_export(yaml) → export'` is semantically equivalent to the original `export`. UIDs and timestamps differ; content, structure, and lock/completion flags must match. Use `diff_export.py` to verify.

There's a companion Claude skill at `.claude/skills/lxp-roundtrip/SKILL.md` that drives the workflow interactively. Type `/lxp-roundtrip` (or describe the task and let Claude pick the skill) to step through ingest → edit → build → validate.

## Installation

This toolkit is dropped into a course repo as a single directory:

```
<repo>/
├── lxp_tools/
│   ├── README.md                  ← this file
│   ├── lxp_tools.config.yaml      ← per-course settings
│   ├── manifest_schema.json       ← static LXP manifest schema (vendored)
│   ├── _config.py                 ← shared config loader
│   ├── export_to_yaml.py          ← LXP export → YAMLs
│   ├── build_export.py            ← YAMLs → LXP export
│   └── diff_export.py             ← compare two exports
├── lxp_build_plans/
│   ├── Module1/segment_*.yaml
│   ├── Module2/segment_*.yaml
│   └── ...
├── Interactives/                  ← widget JS/CSS sources
└── ...
```

Dependencies: Python 3.10+, `PyYAML`. No other packages.

## Per-course configuration

Edit `lxp_tools.config.yaml`. All paths are relative to the repo root (the directory containing `lxp_tools/`).

| Key | Default | What it does |
|---|---|---|
| `course_title` | `"Course"` | Repository name in the export. |
| `course_color` | `"#2196F3"` | Repository color. |
| `repo_id` | `9000` | LXP `repository.id`; stable across builds for friendly diffs. |
| `build_plans_dir` | `"lxp_build_plans"` | Where Module<N>/ subdirs of YAML live. |
| `export_output_dir` | `"lxp_export"` | Where built tarballs go. |
| `interactives_dirs` | `["Interactives", "course_outline"]` | Roots scanned to reverse-resolve widget asset hashes. Order matters: earlier wins on collisions. |
| `module_folder_pattern` | `"Module{n}"` | Subdir name pattern under `build_plans_dir`. `{n}` is the module ordinal. |
| `module_name_pattern` | `"Module {n}"` | FOLDER name in the export. |
| `manifest_schema_file` | `"manifest_schema.json"` | Path to the vendored LXP manifest schema (relative to `lxp_tools/`). Almost no course needs to change this. |

## CLI reference

### `export_to_yaml.py` — ingest an LXP export

```bash
python3 lxp_tools/export_to_yaml.py <input> --output-dir <dir> [options]
```

`<input>` is either an extracted export directory or a `.tgz` tarball (auto-extracted).

| Flag | Default | What it does |
|---|---|---|
| `--config PATH` | `lxp_tools/lxp_tools.config.yaml` | Override config path. |
| `--output-dir PATH` | required | Where to write Module<N>/ subdirs of YAMLs. |
| `--no-preserve-names` | off | Don't reuse existing YAML filenames; auto-generate from `page_title`. By default the script reads existing YAMLs and matches new pages by `page_title` so renumbered/renamed segments keep canonical filenames. |
| `--use-snapshot-fallback` | on | Index the export's own `repository/assets/` as fallback when an asset hash isn't found under `interactives_dirs`. Disable only if you want hard failures. |

### `build_export.py` — build an LXP export from YAMLs

```bash
# Full course
python3 lxp_tools/build_export.py --all --out lxp_export/cs20_full

# Subset of modules
python3 lxp_tools/build_export.py --out lxp_export/m1 \
    --module 'Module 1' lxp_build_plans/Module1/segment_*.yaml
```

| Flag | Default | What it does |
|---|---|---|
| `--config PATH` | default config | Override config path. |
| `--out PATH` | required | Output directory (removed if it exists). |
| `--all` | off | Build every Module<N>/ subdir under `build_plans_dir` in numeric order. |
| `--module NAME YAML…` | (none) | Explicit module list. Repeat for multiple modules. Overrides `--all`. |
| `--course-name NAME` | from config | Override `course_title`. |
| `--course-color #RGB` | from config | Override `course_color`. |
| `--repo-id INT` | from config | Override `repo_id`. |
| `--repo-uid UUID` | minted | Override `repository.uid`. |

After building, tar it up for upload:
```bash
tar -C lxp_export/cs20_full -czf lxp_export/cs20_full.tgz .
```

### `diff_export.py` — compare two exports

```bash
python3 lxp_tools/diff_export.py <generated> <reference>
```

| Flag | What it does |
|---|---|
| `--limit-to-folder NAME` | Restrict to one module by FOLDER name. |
| `--max-diffs N` | Stop printing after N diffs (still counts total). 0 = unlimited. |

Exit code: `0` = clean, `1` = diffs found.

The script normalizes fields that legitimately differ between builds (UUIDs, timestamps, asset content-hashes, presigned S3 URLs, CDA_VIDEO Mux placeholders, HTML whitespace between tags). Differences in any other field are reported.

## Build-plan YAML schema

One YAML per LXP PAGE (segment). Top-level keys:

```yaml
segment_id: "1.0"                       # logical identifier (your choice)
source_outline: course_outline/...      # optional: pointer to source markdown
lxp_module: 1                           # 1-based module ordinal
lxp_segment_number: 1                   # 1-based PAGE position within the module
page_title: "Segment 1: What is a Proof?"

sections:
  - title: "..."                        # SECTION title (visible to learner)
    locked: true                        # unlocks after prior section completes
    completion_required: true           # required for module completion
    items:
      - container: invisible            # see "Containers" below
        element: HLXP_HTML
        body: |
          <h2>...</h2>
          <p>...</p>
```

### Containers

| YAML `container:` value | LXP type | Children key | When to use |
|---|---|---|---|
| `invisible` | `INVISIBLE_CONTAINER` | (single inline element) | Default wrapper for one element. The child element's keys appear at the same indent level as `container:`. |
| `expand` | `EXPAND_CONTAINER` | `items:` | Expandable accordion (e.g., "Show solution"). |
| `assignment` | `ASSIGNMENT` | `items:` | Gradeable assignment grouping. |
| `question_set` | `CEK_QUESTION_SET` | `questions:` | Quiz / question bank. Extra keys: `internal_title`, `randomize_order`, `display_questions`, `number_of_attempts`, `display_correct_answers`. |

### Element types

| YAML `element:` value | LXP type | Required keys | Optional keys |
|---|---|---|---|
| `HLXP_HTML` | `HLXP_HTML` | `body` (block scalar HTML) | `title` |
| `HLXP_REFLECTION` | `HLXP_REFLECTION` | `prompt` | `title`, `shared` (bool), `input_output` (`INPUT_OUTPUT`/`INPUT_ONLY`/`OUTPUT_ONLY`), `min_word_count`, `local_id` (string, target for `seed_from`), `seed_from` (other reflection's `local_id`) |
| `HLXP_SINGLE_CHOICE_QUESTION` | `HLXP_SINGLE_CHOICE_QUESTION` | `stem`, `answers: [{label, correct}]` (exactly one `correct: true`) | `title`, `feedback_type` (`targeted`/`general`), `general_feedback` |
| `HLXP_MULTIPLE_CHOICE_QUESTION` | `HLXP_MULTIPLE_CHOICE_QUESTION` | `stem`, `answers` (one or more `correct: true`) | `title` |
| `CDA_VIDEO` | `CDA_VIDEO` | (none) | `title` (Mux upload IDs are filled in post-import) |
| `LXP_ADV_HTML` | `LXP_ADV_HTML` | `widget_js`, `widget_css`, and one of (`instance_html` path or `html_inline_body` block) | `title` |
| `IMAGE` | `IMAGE` | `asset_path`, `natural_width`, `natural_height` | (none) |

### Widget asset resolution

For `LXP_ADV_HTML`:
- `widget_js` / `widget_css` are paths to source files. The builder content-hashes them and stores under `repository/assets/<sha256>___<basename>`.
- `instance_html` is a path to the per-instance HTML body. Read into `data.html.codeContent`.
- The converter reverses this: hashes every file under `interactives_dirs`, then maps each export asset back to its source path. If a hash isn't found, it falls back to the export's own `repository/assets/` snapshot path.

## Known LXP UI behaviors

These behaviors are not visible from the LXP API or schema; they were discovered by inspecting exports produced after editing content in the LXP UI.

### MathML stripping on save

The LXP rich-text editor converts `<math>...</math>` → Unicode + `<sup>`/`<sub>` whenever a section is saved through the UI:

- `HLXP_HTML` element bodies: MathML survives if untouched. The moment the user opens and saves the section in the UI, MathML becomes Unicode + `<sup>`/`<sub>`.
- `HLXP_REFLECTION` prompts: **NOT** stripped — these are pop-up elements the editor handles differently.

**Implication:** for sections you expect the user to edit in LXP, author math as Unicode + `<sup>`/`<sub>` from the start. Don't write MathML you'll just have to lose to UI saves.

### Full-course-only upload

LXP's "Import Course" feature accepts only a full-course tarball — there is no per-module import. Re-export is therefore all-or-nothing; even a one-segment edit ships as a full-course rebuild. `build_export.py --all` exists to make this easy.

### Asset filename mojibake

Filenames containing `U+202F` (NARROW NO-BREAK SPACE) — common in macOS screenshot names like `Screenshot ... 3.57.37 PM.png` — appear in some exports as Latin-1-decoded UTF-8 (the U+0080 C1 control char shows up in the storage URL). The toolkit handles these correctly: the converter escapes them in YAML and the builder round-trips the bytes verbatim.

## Porting to another course

1. Copy `lxp_tools/` and `.claude/skills/lxp-roundtrip/` into the new repo.
2. Edit `lxp_tools/lxp_tools.config.yaml` for the new course's title, color, and any non-default paths.
3. Run a dry-run round-trip on the course's current export to confirm the toolkit is wired correctly:
   ```bash
   python3 lxp_tools/export_to_yaml.py <current_export.tgz> --output-dir /tmp/dry_yaml --no-preserve-names
   # Then build it back and diff.
   ```
   A clean diff means you're ready to ingest user-edited tarballs.
4. Update the CS20Async-specific edit principles in the skill body (`.claude/skills/lxp-roundtrip/SKILL.md`) to your course's editorial conventions.

## Architecture notes

- **The toolkit doesn't talk to LXP.** It only reads/writes the file format LXP accepts via Import/Export. Authentication, upload, and the course UI are out of scope.
- **No state outside the input/output folders.** The scripts are pure functions of their inputs plus the config; rerunning is idempotent.
- **Element schema is fixed.** Adding a new element type means adding `emit_*` functions in both `build_export.py` and `export_to_yaml.py`, plus normalization rules in `diff_export.py`.
- **The vendored manifest schema** (`manifest_schema.json`) was extracted from a working LXP export. If LXP releases a new schema version, replace this file with one extracted from a current export.

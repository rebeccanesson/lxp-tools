---
name: lxp-roundtrip
description: Round-trip editing between an LXP export tarball and the project's YAML build plans. Walks the user through ingest → diff review → edit-in-YAML → rebuild → validate → hand back. Invoke when the user says "ingest an LXP export", "let's update from LXP", "round-trip the export", or hands over a tarball from LXP.
---

# LXP round-trip skill

This skill drives the **ingest → edit → re-export** cycle between an LXP course export and the project's `lxp_build_plans/Module*/*.yaml` source of truth. The toolkit lives in `lxp_tools/`; this skill is the human-readable workflow that wraps it.

## When to invoke

The user says something like:

- "I have a fresh LXP export tarball, let's ingest it"
- "Round-trip the export through the YAML"
- "Update the build plans from this LXP tarball"
- Hands over a `.tgz` file that came from the LXP "export course" feature
- "Apply our edit principles to <segment kind>" (a follow-on after ingest)

The skill assumes the project has the `lxp_tools/` directory in place (or symlinked from a shared installation) and a working `lxp_tools.config.yaml` at the project root.

## Pipeline overview

```
LXP export tarball ──[lxp_tools/export_to_yaml.py]──► lxp_build_plans/Module*/*.yaml
                                                              │
                                                              ▼ edits in YAML
                                                              │
   LXP import ◄──[lxp_tools/build_export.py]── tarball ◄──── YAML
                            ▲
                            │  validates round-trip
                  [lxp_tools/diff_export.py]
```

**Round-trip invariant:** ingesting an export and rebuilding from the resulting YAMLs must produce a semantically equivalent export — UIDs and timestamps differ, but content/structure/lock-and-completion flags must match. Validated by `diff_export.py`.

## The cycle

### Step 1 — Ingest the user's tarball

```bash
python3 lxp_tools/export_to_yaml.py <path_to_tarball.tgz> --output-dir lxp_build_plans
```

This walks every FOLDER (module) → PAGE (segment) in the export and emits one YAML per page into `lxp_build_plans/Module<N>/`. By default it preserves existing YAML filenames by matching on `page_title`, so renumbered psets etc. keep their canonical filenames.

After running, **commit the diff as a single "ingest user edits" commit**. The diff against the previous commit shows exactly what the user changed in LXP — review it before applying further edits. Surface the most consequential changes to the user (don't dump the full diff).

### Step 2 — Review the ingest diff with the user

Before editing, walk through the diff so the user can confirm intent. Categorize changes:

- **Structural edits** (renumbered problems, new sections, removed items) — likely intentional, but flag duplicates ("two `Problem 4`s" usually means the user was mid-renumber and didn't finish).
- **MathML stripping** — automatic; not an edit the user made deliberately, but it sticks (see "LXP UI behaviors" below).
- **Content rewrites** — the user re-worded an intro, comp-check prompt, or solution.

Don't auto-apply additional changes at this step. Just report what landed.

### Step 3 — Apply edit principles in YAML

This is the substantive work. **The course's editorial conventions live in project memory, not in this skill** — consult `MEMORY.md` for feedback/project entries describing the course's pset structure, intro phrasing, solution-style preferences, recommended-reading sources, etc. The skill itself only covers the LXP-platform-level behaviors that are common to every course (next section).

If the project has no editorial memory yet, ask the user before making non-obvious editorial choices. Capture confirmed conventions as feedback memories so they don't have to be re-explained next time.

### Step 4 — Build the export

```bash
# Full course (the only artifact the user can upload to LXP — see "Full-course upload only" below)
python3 lxp_tools/build_export.py --all --out lxp_export/<course>_full

# Optional: per-module builds for inspection
python3 lxp_tools/build_export.py --out lxp_export/m1_full \
    --module 'Module 1' lxp_build_plans/Module1/segment_*.yaml
```

Then tarball the full course:

```bash
tar -C lxp_export/<course>_full -czf lxp_export/<course>_full.tgz .
```

### Step 5 — Validate the round-trip

Before handing back, prove the new build still matches structure/content semantics:

```bash
python3 lxp_tools/diff_export.py lxp_export/<course>_full <previous_export_dir>
```

If the user's intent was *just* to apply edits and not change structure, a clean diff (modulo your intentional edits) is the green light. If the diff surfaces unexpected differences, investigate before shipping.

### Step 6 — Hand back

The deliverable is `lxp_export/<course>_full.tgz`. Tell the user:

- Where the tarball is.
- That LXP only accepts the **full-course tarball** for upload — there is no per-module upload path.
- What's in this build (a 1-sentence summary of edits applied since the last delivery).

## LXP platform behaviors a teammate's Claude needs to know

These behaviors are not visible from reading the code or the LXP API; they were discovered by inspecting exports produced after editing content in the LXP UI. They apply to every LXP course, not just one.

### MathML stripping on UI save

The LXP rich-text editor converts `<math>...</math>` → Unicode plus `<sup>`/`<sub>` tags whenever a section is saved through the UI. Specifically:

- `HLXP_HTML` element bodies: MathML survives if the section was never touched in the UI; gets stripped to Unicode the moment a user edits and saves.
- `HLXP_REFLECTION` prompts: **NOT** stripped (these are pop-up elements the editor treats differently).

**Implication for editing in YAML:** for sections you expect the user to edit in LXP, author math as Unicode + `<sup>`/`<sub>` from the start. Don't write MathML you'll just have to lose. For sections that are stable (intros, definitions), MathML is fine.

Companion preference: when you encounter ASCII caret superscript notation (`x^2`, `{0,1}^ω`) in HLXP_HTML bodies, upgrade it to `<sup>...</sup>` even if the caret would render readably. The user's hand-edits have consistently shown this upgrade; it's the canonical editorial form.

### Conservative-only LaTeX → MathML conversion

If the project includes scripts under `scripts/` for LaTeX-to-MathML conversion, the *settled policy* is to run only the conservative `$...$` / `$$...$$` / `\(...\)` / `\[...\]` pass. Don't attempt per-candidate review of Unicode/HTML math (e.g., `K<sub>n</sub>`, `λ(G)`, `≤`, `∈`) — that path was attempted and abandoned because the review volume is hours per module and the rendered improvement in LXP is marginal. Skip the per-candidate toolchain unless the user specifically asks for it (e.g., a future LXP renderer that uses MathML semantics meaningfully).

### SECTION titles must have a leading `<h2>` to be visible to learners

The SECTION's `data.title` field is editorial-only and **does not render in the LXP learner UI**. For the section title to appear to the learner, it must be present as an `<h2>` heading at the top of an `HLXP_HTML` element inside that section.

**How to apply when authoring:**
- If the SECTION already has an HLXP_HTML element (a section-level INVISIBLE wrapper, or an ASSIGNMENT/CEK lead-in), prepend `<h2>{title}</h2>` to the *first* HTML body in document order.
- If the SECTION has no HLXP_HTML at all (e.g., only a CDA_VIDEO, LXP_ADV_HTML, standalone HLXP_REFLECTION, or a CEK/ASSIGNMENT with no lead-in), add a leading section-level INVISIBLE_CONTAINER → HLXP_HTML whose body is just the `<h2>`. A "title-only HTML element" in front of a video or interactive is the right pattern, not a workaround.
- The H2 text doesn't have to verbatim match `data.title` — title is the editorial nav handle, H2 is the learner-facing heading.
- Reserve `<h2>` for section titles; internal sub-headings use `<h3>+`. If an existing internal `<h3>` duplicates the new H2 verbatim, drop the `<h3>`.

### Prefer more, shorter SECTIONs over fewer larger ones

Default flags on every SECTION should be `locked: true, completion_required: true`. The lock + required-completion combo is what enforces pacing — if multiple activities share a SECTION, the student can ignore later ones inside it once they've satisfied the completion rule, and the next SECTION unlocks regardless. Shorter SECTIONs give finer-grained gating and clearer pacing.

When in doubt about whether two related items (e.g., a video and a follow-on interactive, a setup HTML and a problem, a problem and its solution) should share a SECTION or be split, **split**. The narrow exception is a sentence-or-two HTML set-up that has no value on its own and only frames the immediate-next reflection/interactive. Solutions to problems should lean toward their own SECTION (rather than collapsing to an EXPAND_CONTAINER) when the solution is substantive enough to be paced through.

### Full-course upload only

LXP's "Import Course" feature accepts a single full-course tarball. There is no per-module upload. This means re-export is all-or-nothing: even a one-segment edit ships as a full-course rebuild. The build script supports `--all` for this reason.

## Verifying interactive widgets

LXP_ADV_HTML widgets are self-contained HTML/CSS/JS with no fetch/AJAX, no module imports, and no CORS-restricted assets. **You don't need to spin up an HTTP server to verify them visually** — just open the test page directly from the filesystem (`file://...`). Skip `python3 -m http.server` unless a widget actually needs a feature that requires HTTP.

## Workflow patterns

### Parallel segment drafting via subagents

When the user asks you to draft multiple segments (e.g., "draft Module 4"), launch one subagent per segment in **parallel** rather than drafting them sequentially in the main thread. Segments are independent — each reads its own source outline, references the project's editorial patterns, identifies its own interactives, and writes one YAML file. Parallel subagents run concurrently and keep the main context clean of bulk transcription work.

After all subagents return, the main session does a quick cross-segment consistency pass: recurring widget patterns, consistent module-end "Conclusion" phrasing, voice consistency. Then build the module tarball and commit.

What NOT to parallelize: round-trip validation (it needs iterative diff-and-fix on a single source of truth) and any debugging that requires the LXP-upload feedback loop (one issue at a time). Pure drafting from a source outline is the parallelizable case.

## Failure modes and recovery

### `export_to_yaml.py` fails on unknown element type

The converter knows: `HLXP_HTML`, `HLXP_REFLECTION`, `LXP_FILE_UPLOAD`, `HLXP_SINGLE_CHOICE_QUESTION`, `HLXP_MULTIPLE_CHOICE_QUESTION`, `CDA_VIDEO`, `LXP_ADV_HTML`, `IMAGE`. If LXP added a new element type, the converter raises `ValueError: Unknown element type: ...`. Surface that to the user and add a corresponding `emit_*` method (mirroring the existing ones) before re-running.

### `build_export.py` fails on `Asset source not found`

A YAML references a file under one of the `interactives_dirs` (default `Interactives/`, `course_outline/`) that no longer exists. Either the path is stale in the YAML or the file was moved. Re-run the converter against a tarball that contains the asset (it will fall back to the snapshot path under the export's own `repository/assets/`).

### Diff isn't clean after a build that should be a no-op

First check that no YAMLs were inadvertently changed. If they weren't, the converter or builder probably needs a fix; treat as a bug in `lxp_tools/`.

## Decision points to surface during editing

These are the recurring "small choices" that come up. Batch them in one message to the user rather than asking one at a time.

- **Editorial conventions you're not sure about.** Don't guess at a project's house style; ask, then save the answer as a feedback memory so the next session knows.
- **Whether a section should be edited in YAML or punted to the LXP UI.** If the edit is heavy text and the section is MathML-only, doing it in YAML is fine. If it's a small tweak the user could do in UI, ask.
- **Multi-version solutions or alternate answers.** When source material offers more than one solution to a problem, default to including one and surface the choice to the user.

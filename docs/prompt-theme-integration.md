# FLUX prompt-theme editor

## Palette-based verification gate

The `object-profiles-v1` (`POST /runs`) and `town-plan-v1`/Crypt plan (`POST /integration/runs`) decoration paths use the shared palette gate. For each request, explicit HEX/RGB colors take priority, then reference-image colors, then a real VLM proposal. The plan retains the original palette separately, records color sources and input hashes, and is frozen across retries. A reference image without a selected region uses the full image with a warning. Each run stores `palette-plan.json` and `palette-swatch.png`.

The extractor aligns the generated sprite, uses BOX when shrinking high-resolution output, compares it with the source using ΔE00, then intersects changed pixels with allowed and unprotected regions. Three steps keep FLUX color drift, blur and sub-pixel shift out of the change mask (`extraction` in `gate_config.json`; the settings are uncalibrated):

1. White balance (von Kries): per-channel gains are the median generated/source ratio over opaque source pixels. Only change detection uses the balanced colors; overlay colors stay generated. The correction is skipped when a gain moves more than `white_balance_max_gain_change`, because then decorations more likely cover most of the sprite than a color cast.
2. Hysteresis (as in Canny): a change above `change_delta_e_initial` counts only when 8-connected to a change above `change_delta_e_strong`.
3. Local source range: a change whose balanced color stays inside the 3x3 source color range (± `source_explained_tolerance_rgb`) is a drifted source pixel and is dropped.

`gate.json` records `white_balance_gains`, `dropped_isolated_px` and `dropped_source_explained_px`. A decoration whose color is close to the material under it (for example light snow on light stone) can also be dropped by step 3. Palette distance is diagnostic; it does not remove pixels. Candidate and clipped masks, change and palette-distance maps, pre-snap metrics, corrected overlay and final composite are saved per attempt. Color snapping and final geometry enforcement occur after measurement.

`shadow`, `demo` and `enforce` are separate modes. The default `demo` uses tentative SC/PQ retry levels, makes at most two retries, never retries an unscored result, and selects by O with the earliest candidate winning ties. A low-scoring or unscored but structurally valid result can be applied with an explicit warning. The score estimates are not calibrated against human ratings. `enforce` is rejected until calibrated adopted metrics and thresholds exist; missing required measurements are held as unscored. Per-object failures do not discard other valid objects. A run with zero applied objects stays ready for inspection but cannot be approved.

Configure the actual OpenAI-compatible vision service with `THEME_VLM_URL` and `THEME_VLM_MODEL`. It must support image inputs, `/v1/chat/completions`, top-token log probabilities and the prefilled one-token scoring request. Without this service, text-only palette planning reports `palette_unavailable`; explicit/reference palettes can still be used, and missing visual scores remain null/unscored. The prototype does not substitute prepared model output. Verify model IDs and endpoint features against the running service before a live generation. Tests mock API responses and do not call FLUX or a VLM.

Install `scikit-image` in the server environment for CIEDE2000. Do not infer new decoration support from palette availability: current profile support checks still reject unsupported decorations such as pumpkins or leaves.

### Problem heatmap (RichHF-style, diagnostic)

`problem_heatmap.py` adds a RichHF-shaped output to every non-empty gate candidate: a per-pixel problem map and a list of missing requested decorations. It is computed from the gate's own source, generated image, final overlay and clipped mask. No model is called. It never changes a decision, retry or selection, and its thresholds (`problem_heatmap` in `gate_config.json`) are uncalibrated.

| Class | Meaning |
| --- | --- |
| `cutout_leak` | An overlay pixel inside the source silhouette whose generated color stays inside the local 3x3 source color range (± tolerance). It is a resampled or drifted source pixel, not new decoration material. |
| `background_fringe` | The same test outside the silhouette: gray background mixed into the decoration edge. |
| `cut_edge` | An overlay pixel next to a changed pixel removed by the allowed/protected masks. |
| `fragment` | An overlay component smaller than `fragment_px`. |
| `small_support` | Decoration attached to a source part thinner than about 3 px, or to an object with fewer than `small_object_px` opaque pixels. |
| `crop_seam` | Decoration within `crop_seam_px` of a crop edge where the source continues (a TSX fragment cut from a larger sprite). |

Missing decorations come from two sources: `palette_class` (fewer than `min_decoration_px` non-leak overlay pixels near that decoration's own palette colors) and `vlm_sc` (a scored per-decoration SC below the demo retry level).

Each attempt saves `problem-heatmap.png` (view with legend), `problem-classes.png` (class colors at native size), `problem-map.png` (soft 0-255 map) and `problems.json`. The selected attempt's files are copied to the object folder, and `gate.json` holds the summary under `problems`. Town-plan and Crypt finalization paint the placed objects' classes on a dimmed preview as `problem-heatmap-map.png` (marks widened by one pixel for visibility) with `problems-map.json`, and the result gallery shows both. Re-analyze a saved run without regenerating: `python experiments/prompt-theme-server/problem_heatmap.py RUN_ID_OR_DIR`.

The extractor now drops most of these pixels itself (see the three steps above), so `cutout_leak` mainly reports leaks that survive them. Limitation: the leak test catches blur, shift and color drift of existing source colors. A generated texture that is not a blend of nearby source colors is not flagged. On simulated FLUX-like drift over eight town groups, `cutout_leak` + `background_fringe` reached about 0.98 precision and 0.88-0.92 recall against known leak pixels. This is a simulation result, not a measurement on real FLUX output.

`rahf_probe.py` optionally runs the community RichHF/RAHF reimplementation (github.com/youweiliang/RichHF; non-commercial weights) on saved composites for comparison. In a zero-shot probe on the same simulated leaks, its implausibility map separated leak pixels from other object pixels with only 0.58-0.87 AUC, mostly highlighted correctly added bulbs, and its misaligned-word output marked "pixel art" instead of missing decorations. Use it only as a comparison aid until it is fine-tuned on our own labeled sprites.

Game editor chrome uses the same charcoal/blue palette through scoped `editorWorkspace.css`. Header, asset sidebar, inputs and preview toolbar are restyled without changing the game iframe, source assets, sidebar hover behavior or generation worker.

The style workspace now uses a charcoal/blue review layout, a bounded object selector with select-all/clear controls, a generation progress bar and collapsed advanced DSL controls. Opening the Crypt workspace resumes displaying an existing running batch before the applied result. This only polls status: it does not resume, cancel or restart the worker. The game editor sidebar remains unchanged.

The editor's **스타일 변환** link opens `/editor.html?workspace=style`. The original compact asset sidebar is restored; generation lives on a separate page, not in the sidebar.

The sidebar also retains the original hover previews: expand a kind and hover an asset row to see its enlarged sprite or TMX-composed object beside the list. Leaving the row hides the preview. This reuses `createHoverPreview` and the original tileset/cell lookup rather than a generated thumbnail.

## Start

For a fresh checkout, run `git lfs pull` and `python scripts/restore-crypt-ruins.py --apply` to restore the bundled first-floor baseline before opening the integration workspace. Omit `--apply` for artifact-only restoration. The command validates sources and overlays, tolerates only XML line-ending differences, refuses conflicting experiment files, backs up the first-floor selection and leaves floor zero untouched. It does not run FLUX. Test with `python -m unittest discover -s scripts -p test_restore_crypt_ruins.py`.

Install FastAPI, uvicorn, Pillow, NumPy and requests in your Python environment. Start the existing FLUX `/style-transfer` service separately; no Qwen or image-generation fallback is used.

```powershell
# Optional: point at an existing FLUX service or SSH-forwarded local port.
$env:THEME_FLUX_URL = 'http://127.0.0.1:18765'
npm run theme:dev
```

In a second terminal, run `npm run dev -- --host 127.0.0.1 --port 15174 --strictPort`. The editor proxies `/api/prompt-theme` to the isolated local API on port 8773 and `/theme-runs` to its artifact endpoint. `THEME_API_URL` can override that API address. `PYTHON` and `THEME_PORT` can override the launcher defaults. Do not configure `THEME_STATE_URL` for browser-local operation.

## Flow and persistence

### Automatic ruins planning

`POST /integration/plan` now uses our own deterministic `dsl.py` and `planner.py`, not external team code or an LLM. It produces validated DSL, per-group plans and a parent run ID. The UI exposes group selection (two checked by default), and submits only selected rows to `/integration/runs`. A changed parent between analysis and generation is rejected. Rules currently support Christmas/winter snow, lights and explicitly requested fixed-palette recoloring; unsupported themes, decorations and time changes are rejected. Unknown Crypt classes remain generic props, never invented semantic labels. The existing source crop and placement counts are the evidence for each plan. Combined recolor/decorate uses `recolor_base`; source alpha stays unchanged. Color-only rows do not call FLUX. Production decoration extraction now permits a separate three-pixel alpha margin, while preserving base alpha. Composition rebuilds revisions from a stable baseline rather than stacking previous padded results.

Browser verification: prompt `겨울 색과 명암 변경` produced run `ae60917036954007807994cd59421578`, two recolor rows, ready preview and 126 selected placements without a model request. The current game selection was not replaced. New parser/planner tests cover negation, unchanged color, source map matching and unsupported requests.

### Town integration MVP

**Target correction:** the active integration target is the previously used Crypt `floor-1-ruins` editor, not the town demonstration below. `/editor.html?game=crypt&map=floor-1-ruins` keeps its existing selection. Style navigation carries game/map context in both directions, filters history by map and opens its applied result. An unqualified style workspace defaults to this ruins map; town now requires `map=town` explicitly.

`crypt_plan_style.py` reads verified original crops and coordinates from the active ruins run, creates a new revision for selected prop groups and preserves the parent's unrelated overlay. It uses the existing FLUX per-object edit/extraction path, writes six contracts and publishes only via Crypt approval. The revision MVP keeps the existing Christmas lighting and supports extracted prop groups (not ground/wall retheming). Sample contracts are manual, not outputs of Seri's parser. Approval rejects a stale parent selection; the current floor stays unchanged until explicit application. API requests must include `mapId` matching `dsl.target_maps` and cannot silently fall back to town.

The style workspace includes **통합 MVP · DSL + Plan 실행 (town)**. Load the hand-authored sample or paste a `{dsl, plan}` contract from the planner, then generate, inspect Change List / Asset Details / read-only Visual DSL, and approve. `GET /integration/sample` and `POST /integration/runs` use the same API prefix as other theme endpoints. This does not claim to implement Seri's unavailable DSL parser or Planner.

The town slice writes all six contracts, source crops, FLUX inputs/raw outputs, timings and separate recolor/decoration maps. Only `recolor` rows change source colors; the global color filter stays identity. Night shading is applied consistently to the recolored runtime overlay. `add`, `cover` and `skip` are log-only; multiple candidates and explicit seeds are rejected in this MVP rather than silently ignored. FLUX does not expose its actual random seed.

Approval requires complete valid contracts, unchanged source hashes and alpha validation. Applying a town-plan run backs up and replaces the previous generated **town theme as a whole**, preventing stacked full-map overlays; ordinary user placements and other maps remain untouched. Raw original assets are never rewritten. Existing profile runs retain their partial-update behavior. ComfyUI is optional; the verified existing FLUX service is the baseline when no ComfyUI workflow endpoint is available.

After browser application, `/editor.html?styleRun=<id>` opens the original shell with Change List below the game and Visual DSL / Asset Details in the result panel. The original left asset sidebar is unchanged. The ordinary editor without `styleRun` retains its content composer. Structural validation is not visual acceptance: attached lights outside the allowed alpha margin can still be clipped and require a reviewed placement profile.

`restore_first_overlaps.py PARENT_RUN_ID` creates a logged partial restore of the original four demo objects (hall, stall, left fountain, upper tree), preserving the recorded PNG bytes and padded placement coordinates. Its approval contains only those four targets and no theme settings, so unrelated objects and current global night/twinkle settings remain untouched. Approvals replace global settings only when they actually supply new settings.

Every status transition is appended to the run's `events.jsonl`; browser approval and successful browser-save receipts are logged separately. PNGs, raw FLUX responses, prompts and timings remain in the unique run directory. Reuse/corrections create a new run with `parent_run_id`, never overwrite the original experiment. These files are local and git-ignored, not a cloud backup. Run history exposes all retained runs.

`/theme-runs/<id>/review.html` is served through the API with a shared **Apply to editor** button, including older review pages. It enables only for ready results, validates source hashes, preloads artifacts, backs up browser placements, persists the selected targets, records a browser application receipt, then opens `/editor.html`. A logging failure is distinguished from a successful browser apply and remains visible instead of silently redirecting. The style workspace uses the same apply function. Application is local to that browser origin.

`reuse_town_decorations.py PARENT_RUN_ID` creates a reviewed Christmas revision: exact `basket_flowers` tiles receive the existing `prop_5` decoration, including unregistered occurrences. Hidden pixels are clipped against the rendered source map. Trees include TSX-mislabeled `town_prop_325` and `streetlamp_unlit_top_02` canopy fragments; complete original tree sprites plus the existing `tree_3` FLUX decoration cover the lower tree's erroneous window overlap. The composite tree bases use this run's night shade. This is reuse, not a new model generation; timings and provenance say so. It does not edit TMX, change collision, or change differently shaped window planters. The decoration switch hides the revision and reveals the original map again.

1. Interpret a prompt, review the settings and select registered objects.
2. Generate each object with FLUX using its fixed source profile. No source asset is overwritten.
3. Inspect the saved original/generated preview.
4. Approve: the API checks ready status, source hashes and artifact files, then returns placements without touching the remote game's shared state.
5. The browser backs up placements and updates localStorage for `town`. Only selected generated objects and global theme settings are replaced; other objects and maps remain. The game reacts to the storage event, including across editor tabs.

The decoration switch controls color correction, night shading, decorations and lights. Turning it off restores original rendering. Generated settings now drive the renderer instead of forcing night for every result. The original geometry, alpha, doors/windows and collision coordinates remain unchanged.

Records and PNGs remain in `public/theme-runs/` (git-ignored). Placement state belongs to the browser origin: port 15173 and port 15174 intentionally do not share it. Keep the API and artifact directory available when reopening a saved run. The first-quality demo remains available and replaces prompt-theme decorations when explicitly selected.

The parser is rule-based and the profiles currently support snow/lights/garlands. Pixel masks remain heuristic and require visual review. Partial object updates preserve other decorations, but theme-wide color/night settings follow the newest approved run. No claim is made that automatic extraction always matches the first hand-reviewed result.

## Checks

### Full-town attached-material batch

Per-object timing shows pending/running/completed/reused states. New batches record wall-clock start/end plus monotonic elapsed time; `request-timing.json` measures the FLUX HTTP round trip (server computation, transfer and decode), not GPU inference alone. Overall object time includes extraction/placement. Failures retain request timing too. For a batch already running without instrumentation, `watch_batch_timings.py RUN_FOLDER` writes a separate `timings.json` using input/output modification times, explicitly labeled estimated. It never overwrites worker status or interrupts generation. `client/timing-ui.js` displays these values and updates live elapsed time.

`run_town_attached.py` processes all 29 registered placements without stopping on an individual failure. `town_attached_materials.py` isolates same-family TMX tiles instead of cropping all rendered layers; explicit mislabel exceptions restore the hall's roof corner/door and exclude neighboring fountain edges. The hall input matches the first recorded building PNG pixel-for-pixel. Other sources require visual review. FLUX attached decorations include snow, lights and garlands; masks retain connected shading/cables where allowed, with actual generated bulb material relocated only when light extraction misses the source surface. Missing components and failures are recorded, not silently approved. Identical sources and profiles may reuse one generated result.

The batch writes `object_results`, separate processed/succeeded counts, source PNGs, raw FLUX outputs, overlays, a live `review.html`, and a final map/approval manifest. A `.batch.lock` prevents other local API generation submissions during execution. Never restart the API or launch another direct FLUX worker while this batch runs. Game application remains manual. A process crash may leave the lock; inspect worker/process state before recovering it.

### Recorded first-demo replay

`replay_attached_building.py REFERENCE_DIRECTORY` uses the recorded building-only RGBA input, verifies its opaque pixels against the current TMX, and repeats the original attached-decoration prompt at 28 steps / alpha 1.0. It deliberately excludes neighboring fountains/pots that the rectangular all-layer crop includes. The output pauses at `awaiting_review`.

After visually selecting one bulb from the new FLUX output, run `compose_attached_building.py RUN_ID X0 Y0 X1 Y1`. It extracts snow and the central wreath, repeats the selected FLUX bulb at the original 21 eave coordinates, protects windows/doors, checks unchanged original pixels outside the overlay, and writes the review page and browser approval manifest. This is a building-specific reviewed experiment, not the generic 29-object automatic path. Original generation and source records remain separate from the composite. No automatic game apply occurs.

For an already-running batch using the former positional strip extractor, run `python experiments/prompt-theme-server/finalize_town_batch.py RUN_ID`. It waits for completion, re-extracts snow/lights by their color regions from saved FLUX images, rebuilds the preview, and preserves before/after artifacts. This migration helper is for the no-recolor Christmas-night batch only.

`town_profiles.py` extends the four original profiles to 29 static placement targets. Eight overlapping TMX fragment labels belong to their parent sprite, covering all 37 building/tree/fountain/lamp/flower/prop records without duplicate overlays. NPCs and portals are excluded. Additional surface masks are experimental and require visual review.

`python experiments/prompt-theme-server/generate_town_batch.py` creates a Christmas snow/light batch using `THEME_FLUX_URL`. It preserves both attempts, reuses results only when source pixels and local profile settings match, and saves a combined preview and approval manifest. Failed objects are explicitly recorded and omitted, never reported as successful. It does not apply to the game. Run it only while the API is idle; this standalone experiment does not share the API's in-process lock.

The style workspace uses the theme laboratory's charcoal/gold layout: prompt and object selection on the left, comparison and approval on the right, with four progress steps. Detailed logs and previous runs are collapsible. Mobile stacks the panels. This presentation does not change generation, approval, persistence, or the editor sidebar.

`npm run build`, `vitest run src/editor/placementStore.test.ts`, and `python -m unittest discover -s experiments/prompt-theme-server -p 'test_*.py'` cover compilation, partial updates, backup failure, UTF-8 records, source preservation and approval without remote state writes. Browser checks must also cover the asset list, FLUX generation, approval, reload and the decoration toggle.

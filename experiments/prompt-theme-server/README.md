# Prompt-theme server experiments

This folder now supplies the branch's generation API through `npm run theme:dev` (port 8773). The active page is `src/editor/createPromptThemePanel.ts`; see [integration and startup](../../docs/prompt-theme-integration.md). The **첫 결과 복원 · 크리스마스** button remains a separate GPU-free reviewed-asset demo.

## Included logic

- `decoration_profiles.json`, `profile_decorations.py`: active `object-profiles-v1` path. Four explicit object profiles own the generation layout, protected regions and decoration extraction. FLUX produces RGB; code constructs RGBA. No Qwen-Image-Layered dependency.
- `client/`: historical shared-state client snapshots, not the active UI. The active page uses `/approve` and browser-local placements instead of remote shared state.
- `building_quality_experiment.py`: archived single-building quality studies; no automatic application. See `docs/building-quality-experiment.md` for the unsuccessful results and limitations.

- `theme_pipeline.py`: rule-based prompt parsing, TMX composition, RGB gain/bias, per-object FLUX calls, preview and explicit application with backups.
- `object_decorations.py`: object-specific source surfaces and decoration extraction; buildings use roof/eaves, trees foliage, fountains a game-specific rim profile; stalls use strips.
- `refine_object_surfaces.py`: reprocess saved references into a new trial without deleting prior results.
- `restore_approved_decorations.py`: create a new record reusing the original reviewed FLUX PNGs and their exact placement coordinates. This is restoration, not fresh generation.
- `test_*.py`: 32 source-preservation, profile, extraction and browser-approval tests.

The automated generation trials were judged worse than the original hand-tuned decorations. Restoring the reviewed assets fixes the current demo, not arbitrary future generation quality. The generated reference, RGB masks and resizing can still lose shading, wires or fine details.

## Server-only dependencies

Set `THEME_PROJECT` to the repository's absolute path before importing these modules (their snapshot location differs from the live `style-service` location). Install FastAPI, uvicorn, Pillow, NumPy, requests and scikit-image in a dedicated Python environment. The tests use the repository's town TMX and do not call the GPU.

```powershell
$env:THEME_PROJECT = (Get-Location).Path
python -m unittest discover -s experiments/prompt-theme-server -p 'test_*.py'
```

The image generation service is external; running these tests does not download models or call FLUX. To start the experimental API from the repository root after setting `THEME_PROJECT`:

```sh
npm run theme:dev
```

The active Vite proxies point to port 8773. The branch renderer consumes color/night/twinkle settings. Browser approval replaces only selected generated objects and global settings, retaining unrelated placements. The historical `/apply` route still requires explicit `THEME_STATE_URL` and replaces all remote decorations; the active page never calls it.

Type-check the archived client separately:

```sh
npx tsc --noEmit --strict --skipLibCheck --target ES2022 --module ESNext --moduleResolution Bundler --lib ES2022,DOM,DOM.Iterable experiments/prompt-theme-server/client/createPromptThemePanel.ts
```

Real generation requires a running FLUX `/style-transfer` service (`THEME_FLUX_URL`). Browser-local operation does not require `THEME_STATE_URL`. Artifact URLs must return image bytes, not a SPA fallback. The local API reads and writes JSON as UTF-8 on both Windows and Linux.

## Palette-based verification gate

The `feat/style-gate-palette-demo` worktree routes the `object-profiles-v1` and `town-plan-v1`/Crypt plan decoration paths through one shared gate. It freezes one palette plan per request, extracts changed pixels only inside allowed and unprotected geometry, records palette distance before snapping, and saves extracted/fixed overlays, masks, change maps, composites, a palette JSON plan and a color swatch. Palette distance never deletes a candidate pixel. Each object records its score availability, decision, warnings and retries; a structurally valid object can be applied even when another object fails. A ready run with zero applicable objects is not approval-ready.

`gate_config.json` defaults to `demo`. Low SC/PQ estimates can trigger at most two more FLUX requests with the same palette; unscored objects do not retry and remain visibly unscored. These estimates and thresholds are demo heuristics, not calibrated quality claims. `shadow` records decisions without applying them. `enforce` stays unavailable until adopted metrics have calibrated thresholds. A missing required enforce measurement is held as unscored.

Set `THEME_VLM_URL` (OpenAI-compatible server base URL) and `THEME_VLM_MODEL` to enable real palette proposals and visual scoring. Text-only palette plans fail as `palette_unavailable` when that service is missing; explicit colors or a reference image can provide a palette without a proposal call, while visual scores remain unscored if the VLM is unavailable. The model service must support image input, chat completion log probabilities and the configured one-token continuation request. Check `/v1/models` and those request features against the actual deployment before using them; no model name or server capability is assumed by this prototype.

The style UI accepts an optional reference image for Crypt integration runs and links the saved palette swatch. Prompts may specify explicit `#RRGGBB`, `#RGB` or `rgb(r, g, b)` values. A reference image without a selected region uses its full image and records that limitation. This gate does not add pumpkin or leaf support to profiles that do not already support them.

## Current restored result

Live restoration ID: `321f30155d134d03ba3914c1e962d9da`. Its manifest and preview are archived under `public/experiments/restored-first-quality`. Manifest `/theme-runs/…` URLs are historical live-server provenance, not branch asset URLs. The working demo uses `public/experiments/flux-decorations-20260909/placements.json` instead. No credentials, service logs or user-state backups are included.

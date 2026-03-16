# AI Feedback Loop

Use this reference when the goal is not just to run shader tests, but to let AI automatically read deterministic render captures and decide what to change next.

## Main idea

Keep the render and capture path inside the project's JVM language so the project can directly call its own shader APIs. The AI should not depend on free-form console output. Instead, every run should leave machine-readable artifacts in a stable directory.

Core rule:

- the JVM harness captures
- AI interprets

Do not ask the JVM harness to make semantic artistic judgments that the AI can already make from images. Ask it to expose deterministic evidence.

Recommended flow:

1. AI edits shader code, scene data, or test-owned parameters.
2. AI runs a JUnit test or optional JVM CLI entrypoint.
3. The JVM harness renders the scene and writes `actual.png`.
4. The JVM harness captures intermediate pass outputs and probe data.
5. If an expected image exists, the JVM harness also writes `diff.png` and `metrics.json`.
6. The JVM harness writes `capture-manifest.json` as the single summary artifact.
7. AI reads the JSON files and decides the next shader change.

## Primary entrypoints

### JUnit-first path

This is the recommended default because it keeps the feedback loop inside the project's normal test infrastructure.

Use JUnit tests to:

- run one or more scene manifests
- write artifacts into `build/test-artifacts/shaders/...`
- fail the test on unacceptable baseline drift when required
- still emit JSON even when the test fails

### Optional Kotlin CLI path

Add a small `main()` entrypoint for batch runs or parameter sweeps. Reuse the same helper classes as the JUnit path.

Use a CLI when AI needs to:

- run a single scene repeatedly
- sweep shader parameters
- process many manifests in one invocation
- avoid full test discovery overhead

## Recommended helper classes

- `ShaderSceneRunner`
  owns scene loading, shader setup, rendering, and pixel capture
- `ShaderDebugCapture`
  exports intermediate pass outputs, attachments, or debug views
- `ShaderBaselineComparator`
  compares `actual.png` to `expected.png` and writes `diff.png` plus `metrics.json`
- `ShaderProbeRecorder`
  records deterministic state such as sampled pixels, uniforms, attachments, and GL status
- `ShaderCaptureManifestWriter`
  merges metadata, capture outputs, probe data, and comparison results into `capture-manifest.json`

## Artifact contract

### Required per run

- `actual.png`
- `metadata.json`
- `capture-manifest.json`
- pass captures
- probe JSON

### Required when baseline exists

- `expected.png` reference or path
- `diff.png`
- `metrics.json`

## Suggested JSON fields

### metadata.json

```json
{
  "scene_id": "glow-sphere/default",
  "pipeline_variant": "single-pass",
  "gl_version": "4.6.0",
  "driver": "NVIDIA 555.xx",
  "viewport": [512, 512]
}
```

### probe.json

```json
{
  "scene_id": "glow-sphere/default",
  "uniforms": {
    "uTime": 1.25,
    "uGlowRadius": 12.0
  },
  "sampled_pixels": [
    { "x": 256, "y": 256, "rgba": [210, 242, 255, 255] },
    { "x": 256, "y": 180, "rgba": [72, 121, 164, 255] }
  ],
  "attachments": [
    { "name": "color0", "width": 512, "height": 512, "format": "RGBA16F" },
    { "name": "depth", "width": 512, "height": 512, "format": "DEPTH24" }
  ],
  "gl_errors": [],
  "active_passes": [
    "mask",
    "blur-horizontal",
    "blur-vertical",
    "composite"
  ]
}
```

### metrics.json

```json
{
  "scene_id": "glow-sphere/default",
  "matches_baseline": false,
  "rmse": 3.42,
  "max_diff": 28,
  "failing_pixels_ratio": 0.083,
  "bright_region_shift_px": 11.4
}
```

### capture-manifest.json

```json
{
  "scene_id": "glow-sphere/default",
  "status": "captured",
  "passes_baseline": false,
  "artifacts": {
    "actual": "build/test-artifacts/shaders/glow-sphere/default/actual.png",
    "diff": "build/test-artifacts/shaders/glow-sphere/default/diff.png",
    "metrics": "build/test-artifacts/shaders/glow-sphere/default/metrics.json",
    "probe": "build/test-artifacts/shaders/glow-sphere/default/probe.json",
    "passes": [
      "build/test-artifacts/shaders/glow-sphere/default/passes/mask.png",
      "build/test-artifacts/shaders/glow-sphere/default/passes/blur-horizontal.png",
      "build/test-artifacts/shaders/glow-sphere/default/passes/blur-vertical.png"
    ]
  }
}
```

## AI-facing rules

- Always write JSON before throwing a failing assertion when possible.
- Prefer stable field names over prose-heavy output.
- Keep artifact paths deterministic.
- Do not require AI to scrape stack traces to find the result.
- If no baseline exists, still emit `capture-manifest.json`, `probe.json`, and pass captures so AI can iterate by inspecting deterministic evidence plus rendered images.

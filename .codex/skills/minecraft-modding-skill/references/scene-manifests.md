# Scene Manifests

Scene manifests keep shader tests deterministic and reviewable.

## Required fields

Use this shape unless the target project already has an equivalent schema:

```json
{
  "scene_id": "glow-sphere/default",
  "render_pass": "post-process",
  "pipeline_variant": "single-pass",
  "viewport": {
    "width": 512,
    "height": 512
  },
  "time_seconds": 1.25,
  "seed": 7,
  "camera": {
    "projection": "identity",
    "view": "identity"
  },
  "depth_source": "none",
  "textures": [
    {
      "name": "noise",
      "path": "textures/test/noise.png",
      "sampler": {
        "min_filter": "linear",
        "mag_filter": "linear",
        "wrap_s": "repeat",
        "wrap_t": "repeat"
      }
    }
  ],
  "input_channels": [
    {
      "name": "scene_color",
      "source": "generated-flat-color"
    }
  ],
  "global_uniforms": {
    "uTime": 1.25,
    "uViewport": [512.0, 512.0]
  },
  "uniforms": {
    "uGlowColor": [0.1, 0.6, 1.0, 1.0],
    "uGlowRadius": 12.0
  },
  "baseline": "src/test/resources/shader-baselines/glow-sphere/default.png"
}
```

## Why these fields matter

- `pipeline_variant`: distinguishes single-pass, ping-pong blur, composite, distortion, or other pipeline shapes.
- `depth_source`: makes it explicit whether the test uses no depth, synthetic depth, copied depth, or an external depth texture.
- `input_channels`: records what is fed into `ShaderPipe` or equivalent multi-pass abstractions.
- `global_uniforms`: maps well to project patterns like `GlobalUniform<T>`.
- `uniforms`: stores effect-specific values for the program under test.

`baseline` can be omitted when AI is iterating only on visual inspection plus deterministic capture data. In that case the run should still emit `capture-manifest.json`, `probe.json`, and pass captures.

## Determinism rules

- Always set `viewport.width` and `viewport.height`.
- Always set `time_seconds`. Do not use wall-clock time.
- Always set `seed`. Do not use runtime randomness.
- Always record explicit sampler state for textures used in visible output.
- Always choose stable camera matrices or named presets.
- Always set `pipeline_variant` for anything more complex than a single pass.
- Always set `depth_source` if depth participates in visible output.
- Keep one baseline path per scene and variant.

## Avoid these fields or patterns

- `use_real_time`
- `randomize`
- `auto_seed`
- `latest.png`
- scene ids that depend on machine names
- hidden assumptions about current window size
- implicit depth or implicit scene texture inputs

## Suggested naming

- `scene_id`: `<effect>/<variant>`
- baseline path: `src/test/resources/shader-baselines/<effect>/<variant>.png`
- artifact path: `build/test-artifacts/shaders/<effect>/<variant>/`

## Mapping to Kotlin shader APIs

In projects that expose APIs similar to CooParticlesAPI:

- `textures` maps to `GlTexture` setup
- `global_uniforms` maps to shared upload logic such as `GlobalUniform<T>`
- `input_channels` maps to `PipeLinker` or `ShaderPipe.writeFromChannel(...)`
- `depth_source` maps to `GlFrameBuffer` ownership, copied depth, or external depth suppliers

The manifest should describe enough state that a test can rebuild the scene without touching Minecraft-owned frame state.

## Capability buckets

If the mod must support materially different rendering buckets, extend the manifest or baseline path with a capability suffix such as:

- `gl46-nvidia`
- `gl43-amd`
- `macos-core-profile`

Do not mix incompatible baselines in a single expected file.

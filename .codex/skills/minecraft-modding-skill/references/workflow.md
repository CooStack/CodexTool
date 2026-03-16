# Workflow

Use this execution order when Codex is asked to add or repair shader tests in a Minecraft/OpenGL project.

## 1. Locate the split point

Find the lowest layer that can render with an independent OpenGL context. Keep offscreen tests at that layer. Leave Minecraft wrappers, hooks, and reload plumbing to smoke tests.

In a CooParticlesAPI-style JVM project, the usual split is:

- offscreen layer:
  `renderer/shader/api`
  `renderer/shader/*`
- smoke layer:
  client managers
  resource reload
  world or HUD pass hooks
  code that reads `Minecraft.getInstance()` or `mainRenderTarget`
  runtime test registries such as `test/api` and `TestManager`

If a shader path cannot run without Minecraft-owned frame state, do not pretend it is a pure golden test. Keep it in smoke coverage and explain why.

## 2. Choose Kotlin or Java, then copy the templates

Default rule:

- choose Kotlin when the module already uses Kotlin and includes Kotlin stdlib
- choose Java when render code is mainly Java or the build has no Kotlin stdlib

Do not force Kotlin into a Java-first module just because the skill examples started in Kotlin.

Copy these files into the target repo instead of making the project depend on the skill directory:

- `assets/templates/ShaderTestExtension.kt.template`
- `assets/templates/OffscreenShaderGoldenTest.kt.template`
- `assets/templates/MinecraftShaderSmokeTest.kt.template`
- `assets/templates/ShaderSceneRunner.kt.template`
- `assets/templates/ShaderDebugCapture.kt.template`
- `assets/templates/ShaderBaselineComparator.kt.template`
- `assets/templates/ShaderProbeRecorder.kt.template`
- `assets/templates/ShaderCaptureManifestWriter.kt.template`
- `assets/templates/ShaderCaptureCli.kt.template`
- `assets/templates/shader-scene.sample.json`

Rename them into project-local paths that fit the module's conventions.

## 3. Create the deterministic scene manifests

Start with one small scene per effect. Avoid broad "kitchen sink" scenes. A scene should lock the inputs needed to reproduce one effect or failure mode.

For multi-pass pipelines, record:

- pipeline variant
- pass inputs
- bound textures
- global uniforms
- depth source

Keep manifest validation in the chosen JVM language so the same scene model powers JUnit tests, optional CLI runs, and AI-readable feedback generation.

## 4. Write offscreen tests first

For each effect:

1. Create a scene manifest.
2. Render it through a hidden context and FBO.
3. Save the actual output into the test artifact directory.
4. Capture intermediate pass outputs and write probe data.
5. If a reviewed baseline exists, compare against it and write `metrics.json` plus `diff.png`.
6. Write `capture-manifest.json` so AI has one stable summary artifact.

For CooParticlesAPI-style APIs, the core offscreen seam usually lives around:

- `CooShaderProgram`
- `GlShader`
- `GlFrameBuffer`
- `VertexBuffer`
- `GlTexture`
- `ShaderPipe`
- `PipeLinker`

Tests at this layer should prove program compile/link, uniform upload, texture sampling, pipe graph wiring, and framebuffer output without asking Minecraft to own the frame.

## 5. Keep the AI loop machine-readable

Every render run should leave a stable artifact bundle that AI can read directly:

- `actual.png`
- `metadata.json`
- `capture-manifest.json`
- pass captures
- probe JSON

When a baseline exists, add:

- `diff.png`
- `metrics.json`

Do not make AI depend on free-form logs when JSON can express the result. Let Kotlin capture deterministic state. Let AI interpret the images and captured state.

## 6. Review baseline updates

Baseline updates are manual review points. Do not overwrite the expected image on a normal test run. A safe workflow is:

1. Run the test and collect `actual`, `expected`, `diff`, metrics, metadata, and pass captures.
2. Review the diff artifact.
3. Confirm the visual change is intentional.
4. Replace the baseline in a dedicated baseline-update change.

## 7. Add Minecraft smoke checks

Only after the offscreen test exists, unless the effect is impossible to exercise outside Minecraft and the reason is documented.

Smoke checks should verify client integration risks such as:

- resource load and reload
- correct render pass hook
- shader use after window resize
- cleanup on world exit or client shutdown
- live use of `mainRenderTarget`
- compatibility with runtime test group orchestration

One smoke scenario should prove one integration path.

## 8. Reuse existing runtime harnesses

If the mod already contains a runtime test harness, use it as the smoke driver instead of building a new abstraction first.

For CooParticlesAPI-style code:

- `TestOption` is a single smoke case unit
- `TestGroup` is a live-client scenario bundle
- `TestManager` is the registry and ticking coordinator

That harness is appropriate for smoke and demo validation, not for pixel-perfect offscreen golden comparison.

## 9. Keep artifact paths stable

Prefer a build-local artifact root such as `build/test-artifacts/shaders/`. Save at least:

- actual image
- expected image copy or reference
- diff image
- metrics JSON
- metadata JSON for driver, version, scene id, shader variant, and pass configuration

## 10. Split local and CI lanes

Recommended lanes:

- fast lane: compile-only or manifest validation checks
- gpu lane: offscreen tests
- smoke lane: Minecraft integration checks

If the project cannot guarantee stable GPU hardware in CI, keep golden tests behind an explicit lane or runner label.

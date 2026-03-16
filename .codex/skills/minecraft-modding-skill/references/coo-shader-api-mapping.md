# Coo Shader API Mapping

Use this reference when a Kotlin Minecraft mod exposes a split similar to CooParticlesAPI and you need to decide what belongs in offscreen golden tests versus Minecraft smoke checks.

## Standalone OpenGL layer

These are strong candidates for deterministic offscreen tests because they describe explicit graphics resources or explicit pipeline operations:

- `renderer/shader/api/CooShaderProgram.kt`
- `renderer/shader/api/glsl/GlShader.kt`
- `renderer/shader/api/glsl/GlFrameBuffer.kt`
- `renderer/shader/api/VertexBuffer.kt`
- `renderer/shader/api/texture/GlTexture.kt`
- `renderer/shader/api/pipe/ShaderPipe.kt`
- `renderer/shader/api/pipe/PipeLinker.kt`
- `renderer/shader/api/pipe/GlobalUniform.kt`
- implementations under `renderer/shader/*` such as `SimpleShaderProgram`, `ShaderProgramBuilder`, and pipe implementations

Typical offscreen assertions at this layer:

- shader compile and program link succeeds or fails with useful diagnostics
- uniforms are uploaded to the expected program
- textures and samplers are bound in a deterministic way
- framebuffer outputs and channel counts are correct
- `PipeLinker` or equivalent graph wiring sends the expected outputs into the expected inputs
- resize and release paths behave safely

## Minecraft integration layer

These belong in smoke checks because they depend on Minecraft-owned lifecycle, live frame ownership, or client hooks:

- code that calls `Minecraft.getInstance()`
- code that reads `mainRenderTarget`
- resource reload handlers
- world render pass glue
- HUD or overlay render glue
- client render managers
- post-process orchestration bound to the live client

Examples from a CooParticlesAPI-style project:

- `renderer/client/*`
- `renderer/effects/*` when they depend on frame collection owned by the client
- runtime renderer demos in `test/options/renderer/*`
- project-local runtime test orchestration in `test/api/*`, `TestManager.kt`, and group builders

## Example classification

### Offscreen-friendly

- `ShaderProgramBuilder`
- `SimpleShaderProgram`
- `SimpleFrameBuffer`
- `SimpleShaderPipe`
- `PingPongShaderPipe`
- `OutputDepthPipe`, when exercised with test-owned depth inputs

### Smoke-only or smoke-first

- `TestRelativisticShaderPipelines`

Reason:

- it reads from `Minecraft.mainRenderTarget`
- it composes runtime pipes around the live client frame
- it depends on world-facing rendering and real client-owned textures

That makes it a strong integration smoke example, even if parts of the underlying pipes can still have their own offscreen golden tests.

## Recommended split for similar mods

1. Golden tests:
   prove each standalone shader program, pipe, blur pass, or composite stage with hidden-context rendering and manifest-defined inputs.
2. Smoke tests:
   prove that the live client can load resources, build the runtime graph, survive resize, render through the correct pass, and clean up.

If you cannot draw a clean line, bias toward keeping only the lowest explicit GL layer in golden coverage and move the rest into smoke.

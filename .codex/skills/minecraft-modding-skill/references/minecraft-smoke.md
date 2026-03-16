# Minecraft Smoke Checks

Minecraft smoke checks are for integration risk, not full visual regression.

## What to verify

Choose a small set of scenarios that prove the shader survives the real client lifecycle:

- resource load or reload
- world render pass hook
- HUD or overlay path
- post-process chain
- window resize
- world exit or shutdown cleanup
- compatibility with live client-owned render targets

## What not to verify here

- every pixel of every effect
- every visual variant
- broad world traversal
- large random scene coverage
- low-level uniform upload or FBO ownership behavior that can already be proven offscreen

That belongs in offscreen golden tests.

## Evidence to capture

Every smoke failure should save enough evidence to debug without rerunning immediately:

- screenshot or frame capture
- log excerpt
- OpenGL error summary
- scene id or smoke scenario id
- driver and version strings
- pass name such as `world-pass`, `hud-pass`, or `post-process`

## Scenario design

Keep each smoke case narrow. Good examples:

- "effect renders in world pass after resource reload"
- "HUD overlay shader survives window resize"
- "post-process shader cleans up on world exit"
- "pipe chain using mainRenderTarget still renders after resize"

Bad examples:

- "play around in a world for five minutes"
- "verify all shaders in one smoke case"
- "treat a live Minecraft render as a baseline-perfect golden image"

## Integration pattern

Prefer a project-local smoke driver abstraction. The test should call a small interface such as:

- launch or attach client
- load test world
- enable shader or render path
- wait for one known frame boundary
- capture a frame
- collect diagnostics

The bundled JVM templates show the structure, but the actual client driver depends on Fabric, Forge, NeoForge, or a custom harness.

## Reuse existing test harnesses

If the target mod already exposes runtime test abstractions, prefer reusing them.

For a CooParticlesAPI-style project:

- `TestOption` is the smallest smoke unit
- `TestGroup` bundles smoke options into a client scenario
- `TestManager` starts, tracks, and ticks those groups
- renderer demo entities under `test/options/renderer` are strong smoke candidates

That arrangement is useful for proving lifecycle integration and stability. It is not a substitute for deterministic offscreen golden tests against the standalone shader API.

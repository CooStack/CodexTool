---
name: minecraft-modding-skill
description: Use when the task is Minecraft mod development or Minecraft-adjacent JVM development. Trigger this skill when Gradle dependencies indicate Minecraft modding, source imports include net.minecraft packages, or the user explicitly says the work is for a Minecraft mod. Covers mod structure, mod ID rules, mapping and version checks, dependency API extension work, and shader or render-output testing workflows.
---

# Minecraft Modding Skill

## When To Use

Use this skill first whenever any of the following is true:

- Gradle files contain Minecraft-related dependencies or plugins such as Minecraft, Forge, NeoForge, Fabric, Fabric Loader, Fabric API, Architectury, Loom, ForgeGradle, NeoGradle, or mappings configuration.
- Source code imports `net.minecraft.*`, `com.mojang.*`, loader APIs, or common Minecraft modding namespaces.
- The user explicitly says the task is Minecraft mod development, addon development, mod compatibility work, mod API integration, Forge development, Fabric development, or NeoForge development.

If there is uncertainty, inspect the dependency graph and imports before proceeding. Treat confirmed Minecraft modding context as a requirement to use this skill.

Once the project is confirmed to be a Minecraft project, reply exactly once with:

`我已知道这是一个MC项目`

After that acknowledgement, continue implementing the user's actual request without waiting unless clarification is otherwise required.

## Core Rules

### 1. `MOD_ID` must be a static string constant

Any place that needs a mod ID must reference a user-defined static string constant instead of repeating a raw string literal.

Correct pattern:

```java
public final class UsefulMagic {
    public static final String MOD_ID = "usefulmagic";
}
```

Use sites must reference the constant:

```java
new ResourceLocation(UsefulMagic.MOD_ID, "spell_book");
```

Requirements:

- Prefer the main mod class as the owner of `MOD_ID`.
- If the user has not defined such a constant, add one to the main mod class before doing further work.
- When touching existing code, replace repeated raw mod ID literals with the shared constant unless the user explicitly wants otherwise.

### 2. Respect mappings, loader, and Minecraft version

Before implementing or refactoring Minecraft code, identify:

- Minecraft game version
- Loader and platform: Forge, NeoForge, Fabric, Architectury, or mixed setup
- Mapping type in use: Mojmap, Yarn, MCP, intermediary, parchment, or project-specific overlays
- Target side: common, client-only, server-only, or data generation

Do not guess names for Minecraft classes, methods, fields, registries, or lifecycle hooks across mapping sets. Verify against the project's actual dependencies and resolved jars.

Preferred evidence order:

1. User project dependencies and Gradle configuration
2. Resolved dependency jars in the local cache or project environment
3. The exact target mod jar or source attached to the dependency
4. Official docs or upstream source for the exact version
5. GitHub source for the exact tag, branch, or latest compatible version when the exact version is not locally available

If necessary, inspect the target Minecraft jar or build a lightweight knowledge base for the Minecraft game jar to avoid incorrect symbol guesses.

### 3. Build a knowledge base before using another mod's API

If the user wants extension work based on another mod's API, do not start implementation immediately.

Required sequence:

1. Parse the user request into a concrete implementation plan.
2. Identify the exact dependency version, loader, mapping set, and Minecraft version.
3. Build a focused knowledge base for the target mod from the best available sources.
4. Implement only after the API surface and conventions are understood.

Preferred knowledge sources:

1. The dependency version already used by the user project
2. The resolved target jar and any attached sources
3. The target mod's GitHub repository, favoring the exact compatible version or newest compatible branch
4. Official mod documentation, wiki, examples, or API docs

When building the knowledge base, capture at minimum:

- Public entry points and registries
- Required init timing and event hooks
- Expected side restrictions
- Capability, component, attachment, or data access patterns
- Serialization or codec expectations
- Network sync requirements
- Extension points and lifecycle constraints

Implementation should follow the target mod's documented patterns as closely as practical.

## Working Process

Use this sequence for Minecraft mod development tasks:

1. Confirm the project is Minecraft-related using dependencies, imports, or the user's statement.
2. Reply once with `我已知道这是一个MC项目`.
3. Identify loader, mappings, Minecraft version, and target side.
4. Find or define the main-class `MOD_ID` constant.
5. Inspect relevant Minecraft or mod dependency classes before changing behavior.
6. If another mod API is involved, create a short requirements and integration plan first.
7. Build against verified symbols from the exact dependency version instead of memory.
8. Implement with the project's established architecture and registration style.
9. Verify compile-time references, side usage, and resource identifiers.

## Shader And Render Work

This skill also covers shader, framebuffer, render-pass, and RenderEntity workflows in Minecraft-adjacent JVM projects.

For rendering-heavy tasks:

- Prefer deterministic offscreen regression tests around the lowest standalone OpenGL boundary.
- Add narrow Minecraft smoke checks only for lifecycle, resource reload, or render-hook integration risk.
- Emit machine-readable artifacts such as metadata, manifests, metrics, actual images, and diffs when visual output matters.

Use the bundled templates and references in this skill directory when the target project needs shader capture, baseline comparison, or smoke-test scaffolding.

## Implementation Notes

- Avoid hardcoding resource IDs, registry names, or packet channel names when a shared constant belongs in the mod entrypoint or a dedicated constants holder.
- Keep client-only code out of common or dedicated-server paths.
- Match the project's existing registration style instead of mixing patterns from other loaders or versions.
- When source names differ from online examples, trust the local mapped dependency actually used by the project.
- If version evidence is inconsistent, stop and resolve the mismatch before coding.

# f8platform

Independent Feel8 launcher and lifecycle manager. It is bootstrap infrastructure,
not an extension. This repository owns its runtime
`pixi.toml`/`pixi.lock`, implementation, build inputs and publisher workflow.
Official code retains AGPL-3.0/commercial licensing; SDK dependencies use their own license.

See [DEVELOPMENT.md](DEVELOPMENT.md) for local builds. Publisher artifacts contain
built wheels and a locked portable runtime. Installation never builds source code.

The launcher daemon owns extensions, environments, tool jobs, application releases
and service processes. Its bundled management portal works without WebStudio.
Use `f8platform serve --open-browser`, or install the optional `desktop` extra and
run `f8platform serve --tray`. The headless CLI supports `extensions`,
`environments`, `tools`, `services`, application operations and startup settings.
Public API models and the HTTP client are supplied by the SDK. WebStudio is a
client and can be stopped or upgraded while platform management remains available.

The `platform-runtime` Pixi environment is headless; `platform-desktop` adds the
tray dependencies. A direct source process is distinct from an installed release.
Continuous tool jobs and service processes belong to the daemon's lifetime.

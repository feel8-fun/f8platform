# f8platform

Independent Feel8 launcher and lifecycle manager. It is bootstrap infrastructure,
not an extension. This repository owns its runtime
`pixi.toml`/`pixi.lock`, implementation, build inputs and publisher workflow.
Official code retains AGPL-3.0/commercial licensing; SDK dependencies use their own license.

See [DEVELOPMENT.md](DEVELOPMENT.md) for local builds. Publisher artifacts contain
built wheels and a locked portable runtime. Installation never builds source code.

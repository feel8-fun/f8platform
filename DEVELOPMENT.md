# Development and publication

Prepare `.sdk` as a checkout of `feel8-fun/f8sdk` at a reviewed application-capable
commit. Inside the development workspace checkout, run:

```sh
pixi run -e build-check python scripts/workspace_inputs.py prepare
```

Build dependencies live in `.ci/pixi.toml` and `.ci/pixi.lock`. Runtime dependencies
live in the repository's root workspace and lock. Environment names are local;
no Studio rule limits their names or number.

Publish independently:

```sh
pixi run --locked --manifest-path .ci/pixi.toml publish
```

The publisher builds this implementation's wheel, converts declared local library
inputs to wheels, locks the portable runtime and writes a ZIP plus SHA-256 in
`dist/`. No other application implementation is compiled. Configure the publisher
workflow with reviewed dependency commits; it uploads artifacts, not a remote release.

Run the headless manager using `pixi run -e platform-runtime python -m f8platform
--data-dir <absolute-path> serve`. Other terminal sessions can use `list`, `import`,
`prepare`, `select`, `configure`, `start`, `stop`, `update`, and `uninstall` with the
same data directory. Updating a running application requires its consumers to stop;
a failed readiness check restores the previous selection and restarts its old release.

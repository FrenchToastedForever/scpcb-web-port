# SCP: Containment Breach — Low-Power Chromebook Web Port

> **This is a fork.** It is based on the excellent
> [q8j-dev/scpcb-web-port](https://github.com/q8j-dev/scpcb-web-port) WebGPU port
> of *SCP: Containment Breach*. Upstream remains the reference implementation;
> this fork is tuned specifically for low-performance Chromebooks and ARM Mali
> Bifrost GPUs (MediaTek SoCs).

This fork runs the original game in the browser with WebAssembly and WebGPU.
The renderer is built on [blitz3d-ng](https://github.com/blitz3d-ng/blitz3d-ng),
extended with the WebGPU backend in
`blitz3d-ng/src/modules/bb/graphics.webgpu`.

The target is a managed Chromebook with limited VRAM and a slow ARM CPU. The
goal is not maximum visual quality — it is to make the game *start reliably*,
stay inside the GPU watchdog, and remain playable at 480p.

---

## What this fork changes

### Chromebook / ARM Mali hardening

- **Auto 480p resolution** fills the display using its native aspect ratio:
  vertical resolution stays at 480 px, width is calculated from the screen
  aspect ratio and rounded to an even number (for example 1366×768 → 854×480).
- A fixed **640×480** option remains as a fallback. Chromebooks are limited to
  480p options; higher desktop modes are not used on the target device.
- The canvas is styled with `width:100vw; height:100vh; object-fit:fill`, so
  there are no pillarbox/letterbox bars.
- **8-bit rendering only**: the offscreen framebuffer uses `RGBA8Unorm`; the
  surface follows `navigator.gpu.getPreferredCanvasFormat()` (normally
  `bgra8unorm` on Mali). RGB10A2 / 10-bit render passes are never selected.
- The GPU adapter is requested with `powerPreference: "low-power"`.
- The Dreamfilter blur target is capped at **512×512** instead of growing to
  2048/4096 with screen resolution.
- Map generation yields to the browser every 3 room placements (and every map
  row), preventing Chrome's GPU watchdog from treating long ARM CPU work as a
  hang.
- Low-end graphics defaults are enforced: bump mapping, room lights,
  antialiasing, high texture detail, extra particles and VRAM mode are off.
  These settings are re-applied from the web shell on every boot, so an old
  `options.ini` cannot silently restore desktop settings.
- The WebAssembly heap is capped at 1 GiB with a 512 MiB initial size, and the
  packaged asset set is reduced from roughly 295 MiB to about **142 MiB**.

### Save system

The web port keeps full save-game support in the browser:

- Saves are stored as flat `.cbsav` files under `/Saves`, persisted in
  **IndexedDB** per file (key prefix `save:/Saves/`) rather than as one large
  blob.
- Existing localStorage save bundles are migrated into IndexedDB, and the old
  single-blob IndexedDB format is migrated into per-file entries.
- Saves are restored into the Emscripten filesystem before the game starts and
  synced back to IndexedDB every few seconds, on tab hide, and on page unload.
- **Community Saves** UI: browse shared saves, import one into the game, and
  share a local save. Imported saves are written canonically to
  `/Saves/[Shared] <name>.cbsav`, with a compatibility mirror under
  `/Save/[Shared] <name>/`.
- The save seed is read from the `.cbsav` header so duplicate community saves
  for the same seed can be detected/overwritten instead of piling up.
- `serve.py` provides the local Community Save API (`GET/POST /api/saves`,
  `GET /api/saves/<id>`, `/api/clientlog`) and stores shared saves under
  `webgame/shared_saves/`.
- **Clean boot** resets browser-stored game data. It is checked by default in
  this branch; uncheck it before launching if you want to preserve local saves.
- **Clear Cache (download fresh)** is checked by default. It deletes the
  `scpcb-assets` Cache API entry and downloads the asset chunks fresh on every
  launch, avoiding stale/corrupt cache state. Uncheck it to reuse the cached
  asset bundle.

### Web shell quality-of-life

- Fullscreen button; fullscreen hides the GPU diagnostics overlay.
- On-screen startup stages (`[1/5]` … `[5/5]`) and cache/network warnings.
- GPU diagnostics panel showing vendor, architecture, description and key
  adapter limits.
- Early adapter/device rejection messages are shown as large red on-screen
  errors instead of only going to DevTools, which is important on managed
  Chromebooks where DevTools is blocked.
- Sequential cache loading keeps only one asset chunk in flight at a time,
  avoiding the memory spike from four parallel 24 MiB reads.
- A missing/corrupt cached chunk falls back to the network for that chunk; a
  cache read failure clears the asset cache and retries fresh.

### Asset optimization

- Textures capped at 512×512 (map textures at 256×256), preserving format.
- Music re-encoded stereo at 22050 Hz / 64 kbps; other sound effects mono at
  22050 Hz (Vorbis quality 1).
- Packaged game data is approximately **142 MiB**, down from roughly 295 MiB.

---

## Requirements

- Chrome or Edge **137+** with **WebGPU** and JavaScript Promise Integration.
  Safari 26+ may work where WebGPU/JSPI support is available.
- A browser allowed to use WebGPU; managed ChromeOS devices may require the
  relevant enterprise policy to allow WebGPU.
- HTTPS or `localhost` (required for WebGPU and pointer lock).

The game is designed for Chromebook-class hardware, but it still needs a
browser with working WebGPU. If WebGPU is blocked by policy, the diagnostics
overlay will report the adapter/device rejection.

## Building

Install Python 3.10+, CMake, Ninja and the
[Emscripten SDK](https://emscripten.org/docs/getting_started/downloads.html),
then activate Emscripten in your terminal. Windows additionally needs Visual
Studio 2022 with C++ and MFC. Ubuntu/Debian needs:

```sh
sudo apt install git autoconf libtool gettext autopoint gperf clang \
  libxml2-dev zlib1g-dev libwxgtk3.0-gtk3-dev libxrandr-dev libxinerama-dev \
  libxcursor-dev uuid-dev libfontconfig1-dev
```

Build everything:

```sh
python3 build.py
```

The first Linux build compiles LLVM from source and can take an hour or more.
macOS on Apple Silicon and Windows download a prebuilt LLVM.

Incremental targets:

```sh
python3 build.py deps      # dependency sources
python3 build.py llvm      # LLVM toolchain
python3 build.py blitzcc   # native compiler
python3 build.py runtime   # wasm WebGPU runtime
python3 build.py pack      # package assets
python3 build.py game      # compile/link the game
```

A normal game relink takes about 30 seconds. Changing only assets repackages
them in a few seconds; changing game or runtime code relinks the game.

## Running locally

```sh
python3 build.py --serve
```

Open <http://127.0.0.1:8090/>. `--serve` now starts `serve.py`, not a bare
static server, so the Community Save API is available. You can also run it
directly:

```sh
python3 serve.py
```

`PORT` can override the default port:

```sh
PORT=8080 python3 serve.py
```

## Controls

- **Alt+Enter** — toggle browser fullscreen.
- **Escape** — release pointer lock / open the in-game menu (browser default
  pointer-lock behavior).
- Save, load and options are handled by the original in-game menus.

## Project layout

- `upstream-scpcb/` — game source (`.bb` files, `GFX/`, `SFX/`, etc.).
- `blitz3d-ng/` — compiler/runtime, including the WebGPU renderer.
- `web-shell/index.html` — launcher, diagnostics, save/cache UI.
- `serve.py` — local server + Community Save Sharing API.
- `build.py` — build orchestration.
- `webgame/` — generated deployable build (ignored by git).

## Save data and cache notes

- The canonical save format is flat `.cbsav` under `/Saves`; the legacy
  directory layout is mirrored for compatibility.
- Clearing the browser's site data (or using **Clean boot**) removes local saves
  and options. Uncheck **Clean boot** if you want to keep saves between runs.
- **Clear Cache (download fresh)** removes only the Cache API asset bucket, not
  IndexedDB saves or options.
- Imported community saves are written into the game filesystem by the shell;
  they do not modify the packaged `upstream-scpcb/` files on disk.

## Credits and license

- Original WebGPU web port: [q8j-dev/scpcb-web-port](https://github.com/q8j-dev/scpcb-web-port).
- SCP: Containment Breach and its community preservation projects.
- [blitz3d-ng](https://github.com/blitz3d-ng/blitz3d-ng) compiler/runtime.
- This fork's web-port glue, Chromebook tuning and WebGPU changes are covered
  by the repository's MIT license; vendored third-party projects retain their
  own licenses. See `LICENSE` and the individual dependency licenses.

This is an unofficial fan project and is not affiliated with the SCP Foundation
or the original SCP: Containment Breach developers.

# Camera streaming service — rewrite notes

This is a rewrite of the original dual-camera Rust streaming app, adapted
for your actual hardware: a single 4K USB camera (Ailipu-based, USB ID
`32e4:0415`) on a Raspberry Pi 5.

## Bugs fixed vs. the original

| Bug | Original behavior | Fix |
|---|---|---|
| Lifetime error | `Stream<'a>` borrowed from a `Device` that was dropped when `initialize_video_stream()` returned | `Device` and `Stream` are now created and used entirely within `capture_worker()` — no cross-function lifetime |
| `videoresolution` silently ignored | Updated a shared `VideoFormat` struct, but the running stream never re-read it | Capture loop now compares the live format against the shared one every iteration and restarts the stream on change |
| Hardcoded device paths | `main()` matched on literal PCIe path strings for two specific cameras | Device path is resolved generically from `config.toml`'s `path_identifier`, matched against `/dev/v4l/by-path` |
| Mixed channel types | `std::sync::mpsc` and `tokio::sync::mpsc` both used inside the same async task (`process_stream`) | One channel type per pipeline: `std::sync::mpsc` for the blocking capture thread, consumed there only |
| Blocking the async runtime | Sensor-alert file polling created a brand-new `tokio::runtime::Runtime` and called `block_on()` from inside an already-running async task | Polling now uses `tokio::select!` with async file I/O (`tokio::fs`), no nested runtime |
| Hidden warnings | `#![allow(warnings)]` at the top of `main.rs` | Removed |
| Per-frame thread spawn | A new OS thread was spawned for *every* captured frame | One dedicated frame-writer thread, fed by a channel |
| Continuous disk writes | Every frame was timestamped and written to disk whenever the stream was "active" (default), regardless of whether anyone asked for it | Off by default (`continuous_save = false` in config.toml) — snapshot/multisnapshot commands still write frames on request. Flip it on if you actually want continuous recording, but know that's heavy SD card wear at 30fps. |
| Dead code | `BarcodeDetect` (QR scanning) and the WebRTC track-creation functions were imported/defined but never called anywhere in `main()` | Removed — they added dependency weight for code that wasn't wired up. Can be re-added later if you want QR or WebRTC support for real. |

## What's still the same

- Two WebSocket listeners: port 8080 (camera control: snapshot, resolution,
  print jobs, sensor alarm) and port 8081 (stream control: start/stop)
- MJPG capture via `v4l`, timestamp overlay, JPEG re-encode via `turbojpeg`
- Printer integration via your existing `scripts/printer_enum.sh` /
  `printer_helper.sh`

## Before building: confirm `path_identifier`

```bash
ls -la /dev/v4l/by-path
```

You should see an entry like:

```
usb-xhci-hcd.1-1-video-index0 -> ../../video0
```

Confirm the string in `config.toml`'s `path_identifier` matches what's
actually there — your earlier `lsusb` output suggested
`usb-xhci-hcd.1-1`, so I've set it to `usb-xhci-hcd.1-1-video-index0`, but
please double check against the real `ls` output before building, since
USB bus/port numbering can shift.

## Build dependencies (on the Pi)

```bash
sudo apt update
sudo apt install -y build-essential pkg-config clang libclang-dev \
    libjpeg-turbo-progs libturbojpeg0-dev v4l-utils
```

**Rust toolchain:** if `apt list rustc` on your Pi gives you anything older
than roughly 1.85, install via rustup instead — one of the crates here
(`v4l`'s FFI binding generator) needs a fairly current cargo:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

## Build and run

```bash
cd camera
cargo build --release
./target/release/camera
```

First build will take a while on a Pi 5 (1GB) — `bindgen` and friends are
heavy. If you hit out-of-memory during the build, add swap temporarily or
build with `cargo build --release -j 1` to limit parallel codegen units.

## A note on how I verified this

I rewrote and reviewed this code carefully, including pulling the exact
source of the `v4l` crate from GitHub to confirm method signatures
(`Device::with_path`, `MmapStream::new`, etc.) line by line. I wasn't able
to get a full `cargo check` running in my own sandbox — its Rust toolchain
is a couple years old at this point and chokes on parts of today's
dependency graph that need a newer Rust edition. That's a sandbox
limitation, not a reflection of the code. Your Pi will have (or should
have, via rustup) a current toolchain, so the real test is just running
`cargo build` there. If it throws errors, send them my way and we'll fix
them together — much faster to debug with a real compiler in the loop
anyway.

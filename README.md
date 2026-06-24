# Roost

A self-hosted camera pipeline you own end to end. A Raspberry Pi captures
from a USB camera, a Rust app exposes control over WebSocket, a Python
script pushes snapshots to a private S3 bucket, and a dashboard pulls them
back down and gives you live controls. No cloud camera subscription, no
app phoning home to someone else's servers.

## The pipeline

```
[USB camera] --MJPG--> [capture app, on the Pi]
                              |  WebSocket control (ports 8080/8081)
                              |  writes snapshots to frames/
                              v
                        [s3 uploader, on the Pi]
                              |  watches frames/, uploads, deletes local copy
                              v
                        [S3 bucket, private]
                              ^
                              |  presigned URLs
                        [dashboard, anywhere]
                              |  Flask backend + React frontend
                              |  controls the camera directly over WebSocket
```

## What's in this repo

### Capture app (`src/`, `Cargo.toml`, `config.example.toml`)

The core, written in Rust. It opens a USB camera via `v4l`, streams MJPG,
overlays a timestamp on snapshots, and exposes two WebSocket control
sockets:

- **Port 8080 (camera control):** snapshot, multi-snapshot, resolution
  change, shutdown, plus printer commands.
- **Port 8081 (stream control):** start and stop the stream.

Commands are JSON, for example `{"command":"snapshot"}`. The capture app
identifies the camera by a stable `/dev/v4l/by-path` substring set in
`config.toml`, rather than a `/dev/videoN` number that can shift between
boots, and captures MJPG because most generic USB cameras only reach
usable frame rates in that format.

The control socket also watches for an external trigger file on disk and
fires a burst of captures when it appears. As written that is a simple
file watch, but the same hook generalizes: a motion sensor, a doorbell, a
webhook from elsewhere on your network, anything that can drop a signal,
can make this camera react without a direct command.

### S3 uploader (`s3_uploader.py`)

A small Python script that watches the capture output folder, uploads each
new frame to a private S3 bucket, and deletes the local copy once the
upload succeeds. If an upload fails it leaves the file in place and retries
on the next pass, so a flaky connection never costs you a capture.

### Dashboard (`viewer/`)

A Flask backend plus a single-file React frontend. The backend holds the
AWS credentials and hands the page short-lived presigned URLs, which keeps
the S3 bucket fully private rather than public. The frontend talks directly
to the camera's WebSocket ports to send commands, and shows a live gallery
of captures pulled from S3. It runs anywhere with network access to both
AWS and the camera, so it does not have to live on the Pi.

### Service file (`camera.service`)

A `systemd` unit that runs the capture app on boot and restarts it on
failure, so the pipeline survives reboots and dropped SSH sessions.

## Setup

The full step-by-step guide, from flashing the Pi through a working
dashboard, including the AWS bucket, the scoped IAM user, and credentials,
lives on the blog:

**[blog.hiimmichael.com/articles/roost-setup-tutorial.html](https://blog.hiimmichael.com/articles/roost-setup-tutorial.html)**

Start with the camera. Get a single frame off the device and onto disk
before worrying about WebSockets, S3, or dashboards. Everything else is
plumbing once that first frame exists.

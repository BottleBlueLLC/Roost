# Roost

An open-source, self-hosted security camera pipeline you own end to end. Roost runs on a Raspberry
Pi with a USB camera, and a Rust app exposes the camera as a set of commands
over WebSocket. Anything that can send a command can drive it: the dashboard's
live controls but also a sensor on your network, a door or garage
opening, a motion detector tripping, a webhook firing. When something happens,
Roost captures it, a Lambda runs Claude vision on the photo to describe and tag
what's in the frame, and everything lands in a private store you can search by
contents ("show me every photo with a delivery truck"). No cloud camera
subscription, no app phoning home to someone else's servers.

Under the hood: a Python script pushes snapshots to a private S3 bucket, the
vision Lambda writes Claude's descriptions and tags to DynamoDB, and a dashboard
pulls it all together with live controls and search-by-contents.

<img width="1200" height="627" alt="roost" src="https://github.com/user-attachments/assets/d69d732b-1ec4-4320-806d-025825d3e425" />

## The pipeline

```
                        [USB camera]
                              | MJPG
                              v
                        [capture app, on the Roost device]
                              |  WebSocket control (ports 8080/8081)
                              |  writes snapshots to frames directory
                              v
                        [s3 uploader, on the Roost device]
                              |  watches frames/, uploads, deletes local copy
                              v
                        [S3 bucket, private] --- new image event --->  [vision Lambda]
                              ^                                              |  Claude describes + tags
                              |  presigned URLs                             v
                              |                                       [DynamoDB: Roost]
                        [dashboard, anywhere]  <--- tag search --------------+
                              |  Flask backend + React frontend
                              |  controls the camera directly over WebSocket
                              |  search photos by what's in them
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

### Vision recognition (`vision/`)

An AWS Lambda that fires on every new image landing in the S3 bucket. It
sends the photo to Claude, which returns a natural-language description
and a list of search tags, and writes both to a DynamoDB table. Because
Claude actually sees the image, the tags are open-vocabulary and the
descriptions reflect the whole scene, not a fixed category list.

The DynamoDB table uses a single-table design (generic PK/SK keys plus a
GSI for tag lookups), so searching "find all my photos tagged dog, newest
first" is one indexed query, and the schema already supports multiple
cameras and users without a migration.

### Dashboard (`viewer/`)

A Flask backend plus a single-file React frontend. The backend holds the
AWS credentials and hands the page short-lived presigned URLs, which keeps
the S3 bucket fully private rather than public. The frontend talks directly
to the camera's WebSocket ports to send commands, shows a live gallery of
captures pulled from S3, and lets you search photos by what's in them
(querying the recognition tags). Clicking a result shows Claude's
description and the tags. It runs anywhere with network access to both AWS
and the camera, so it does not have to live on the Roost device.

### Service file (`camera.service`)

A `systemd` unit that runs the capture app on boot and restarts it on
failure, so the pipeline survives reboots and dropped SSH sessions.

## Setup

The full step-by-step guide, from flashing the device through a working
dashboard with recognition and search, including the AWS bucket, the
DynamoDB table, the Lambda, the scoped IAM users, and credentials, lives
on the blog:

**[blog.hiimmichael.com/articles/roost-setup-tutorial.html](https://blog.hiimmichael.com/articles/roost-setup-tutorial.html)**

Start with the camera. Get a single frame off the device and onto disk
before worrying about WebSockets, S3, recognition, or dashboards.
Everything else is plumbing once that first frame exists.

---

Roost is open source, built and maintained by Bottle Blue LLC.

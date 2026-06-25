# viewer

The dashboard for the Roost camera pipeline. A small Flask backend
generates short-lived presigned S3 URLs so the private bucket never needs
to be exposed, and a React frontend (served from `static/index.html`)
handles the UI: live camera controls, a rolling photo gallery, and
search-by-tag powered by the vision Lambda's DynamoDB output.

## Why a backend at all

The S3 bucket is private. Listing or reading it from browser JavaScript
would require either making the bucket public or shipping AWS credentials
to the browser. The Flask server runs on your local machine, uses the
`roost` AWS profile already configured for the uploader, and hands
the frontend short-lived presigned URLs instead.

Camera control (WebSocket commands) does not route through the backend.
The frontend connects directly to the capture app's WebSocket endpoints
on the Roost device, since those are plain local-network sockets with no secrets.

## Structure

```
viewer/
  server.py          Flask app -- S3 listing, presigned URLs, DynamoDB tag search
  requirements.txt   Python dependencies (flask, boto3)
  static/
    index.html       Single-page React app (no build step, loaded via Babel standalone)
```

## Prerequisites

- Python 3.9+
- An AWS profile named `roost` in `~/.aws/credentials` with read
  access to the `picamera-mcoughlin-frames` S3 bucket and the `Roost`
  DynamoDB table
- The Roost DynamoDB table with a `GSI1` index (written by the vision
  Lambda; see `roost-vision/`)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Running

```bash
python server.py
```

The server binds to `0.0.0.0:5000` by default. Override the port with the
`PORT` environment variable:

```bash
PORT=8000 python server.py
```

Open `http://localhost:5000` in a browser.

## API routes

| Route | Description |
|---|---|
| `GET /` | Serves `static/index.html` |
| `GET /api/photos` | Lists up to 60 most recent objects from S3, newest first. Returns keys, presigned URLs, sizes, and timestamps. |
| `GET /api/search?tag=<label>` | Queries the `GSI1` DynamoDB index for photos tagged with the given label. Returns matches newest first, each with a presigned URL, description, and full tag list. |

## Frontend features

**Camera tile (left panel)**

- Live/paused status indicator with animated pulse ring
- Take snapshot: sends `{"command": "snapshot"}` to the control socket
- Multi-snapshot: sends `{"command": "multisnapshot", "count": N}` with a
  count selector (2, 3, 5, 10, or 15 frames)
- Start and Stop buttons: sends `start_camera` / `stop_camera` to the
  stream control socket
- Timestamped activity feed for all commands sent
- Configurable camera host: click the host pill in the top bar to edit

The camera host is persisted in `localStorage` under the key
`roost-host` and defaults to `roost.lan`.

**Gallery (right panel)**

- Auto-refreshes every 10 seconds when not in search mode
- Tag search: type any label (e.g. `dog`, `car`, `package`) and press
  Search to query photos by what the vision Lambda detected in them
- Click any photo to open a lightbox with the full image, Claude's
  description, and the complete tag list
- Lazy-loads images; graceful fade-in on load

## Camera WebSocket ports

The frontend connects to two ports on the configured host:

| Port | Purpose |
|---|---|
| 8080 | Camera control (snapshot, multi-snapshot, resolution, shutdown) |
| 8081 | Stream control (start and stop) |

Both expect and send JSON. Connection status is shown as pills in the
top bar; the frontend retries automatically on disconnect with a 4-second
backoff.

---

Part of the [Roost](../README.md) self-hosted camera pipeline.

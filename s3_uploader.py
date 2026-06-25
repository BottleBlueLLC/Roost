#!/usr/bin/env python3
"""
Watches the camera's frame output directory. Whenever a new .jpg shows up,
uploads it to S3, then deletes the local copy.

Run with the venv's Python:
    source ~/camera/uploader-venv/bin/activate
    python3 s3_uploader.py
"""

import os
import sys
import time
import logging

import boto3
from botocore.exceptions import ClientError

# --- Config ---------------------------------------------------------------
BUCKET_NAME = "picamera-mcoughlin-frames"
FRAMES_DIR = os.path.expanduser("~/Roost-main/frames")
POLL_INTERVAL_SECONDS = 2
# Skip files modified within this many seconds, in case one is still being
# written to disk when we list the directory.
MIN_FILE_AGE_SECONDS = 1.0
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("s3_uploader")


def upload_and_remove(s3_client, filepath: str) -> None:
    filename = os.path.basename(filepath)
    try:
        s3_client.upload_file(filepath, BUCKET_NAME, filename)
    except ClientError as e:
        log.error("Upload failed for %s: %s", filename, e)
        return  # leave the file in place, retry next loop
    except OSError as e:
        log.error("Could not read %s: %s", filename, e)
        return

    try:
        os.remove(filepath)
        log.info("Uploaded and removed %s", filename)
    except OSError as e:
        # Upload succeeded but local delete failed -- not fatal, just leaves
        # a leftover file. Log it so it's not a silent surprise later.
        log.warning("Uploaded %s but failed to delete local copy: %s", filename, e)


def main() -> None:
    if not os.path.isdir(FRAMES_DIR):
        log.error("Frames directory does not exist: %s", FRAMES_DIR)
        sys.exit(1)

    s3_client = boto3.client("s3")
    log.info("Watching %s, uploading to s3://%s", FRAMES_DIR, BUCKET_NAME)

    try:
        while True:
            now = time.time()
            for entry in os.scandir(FRAMES_DIR):
                if not entry.is_file() or not entry.name.lower().endswith(".jpg"):
                    continue
                age = now - entry.stat().st_mtime
                if age < MIN_FILE_AGE_SECONDS:
                    continue  # might still be being written, check next loop
                upload_and_remove(s3_client, entry.path)

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()

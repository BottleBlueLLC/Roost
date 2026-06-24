#!/usr/bin/env python3
"""
Small backend for the camera viewer app.

Why a backend at all, instead of a pure static page: the S3 bucket is
private (on purpose), so listing/reading it from browser JS would mean
either making the bucket public or shipping AWS secret keys to the
browser -- both bad ideas. This Flask app holds the AWS credentials
(the same ~/.aws/credentials already set up for the uploader script) and
hands the frontend short-lived presigned URLs instead.

Camera control (WebSocket commands) does NOT go through this backend --
the frontend connects straight to the camera binary's ws://<host>:8080 and
:8081, since those are plain local-network sockets with no auth and no
secrets involved.
"""

import os
from flask import Flask, jsonify, send_from_directory
import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = "picamera-mcoughlin-frames"
PRESIGNED_URL_EXPIRY_SECONDS = 3600
MAX_PHOTOS_RETURNED = 60

app = Flask(__name__, static_folder="static", static_url_path="")
session = boto3.Session(profile_name="picamera")
s3 = session.client("s3")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/photos")
def list_photos():
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME)
    except ClientError as e:
        return jsonify({"error": str(e)}), 502

    objects = response.get("Contents", [])
    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    objects = objects[:MAX_PHOTOS_RETURNED]

    photos = []
    for obj in objects:
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET_NAME, "Key": obj["Key"]},
                ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
            )
        except ClientError as e:
            continue
        photos.append({
            "key": obj["Key"],
            "url": url,
            "lastModified": obj["LastModified"].isoformat(),
            "size": obj["Size"],
        })

    return jsonify({"photos": photos})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

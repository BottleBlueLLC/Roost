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
from flask import Flask, jsonify, send_from_directory, request
import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = "picamera-mcoughlin-frames"
AWS_PROFILE = "picamera"
PRESIGNED_URL_EXPIRY_SECONDS = 3600
MAX_PHOTOS_RETURNED = 60

# Single-table DynamoDB design (see roost-vision/handler.py for how items
# are written). Single-user for now; the schema already supports more.
DYNAMO_TABLE = "Roost"
USER_ID = "michael"

app = Flask(__name__, static_folder="static", static_url_path="")
session = boto3.Session(profile_name=AWS_PROFILE)
s3 = session.client("s3")
dynamodb = session.resource("dynamodb")
table = dynamodb.Table(DYNAMO_TABLE)


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


@app.route("/api/search")
def search_by_tag():
    """
    Search photos by a recognition tag (e.g. "dog", "car", "package").

    Queries the GSI1 index on the Roost table for
    USER#<user>#TAG#<tag>, which the vision Lambda populates -- one row
    per (image, tag) pair. Returns matches newest first, each with a
    presigned S3 URL and the image's description.
    """
    tag = request.args.get("tag", "").strip().lower()
    if not tag:
        return jsonify({"error": "missing 'tag' query parameter"}), 400

    try:
        tag_response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"USER#{USER_ID}#TAG#{tag}"},
            ScanIndexForward=False,  # newest first
        )
    except ClientError as e:
        return jsonify({"error": str(e)}), 502

    results = []
    for tag_item in tag_response.get("Items", []):
        image_key = tag_item.get("image_key")
        timestamp = tag_item.get("timestamp")
        if not image_key or not timestamp:
            continue

        # Fetch the full IMAGE record for its description and tag list.
        description = ""
        all_tags = []
        try:
            image_response = table.get_item(
                Key={
                    "PK": f"USER#{USER_ID}",
                    "SK": f"IMAGE#{timestamp}#{image_key}",
                }
            )
            image_item = image_response.get("Item")
            if image_item:
                description = image_item.get("description", "")
                all_tags = [t.get("label") if isinstance(t, dict) else t
                            for t in image_item.get("tags", [])]
        except ClientError:
            pass  # still return the photo, just without a description

        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET_NAME, "Key": image_key},
                ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
            )
        except ClientError:
            continue

        results.append({
            "key": image_key,
            "url": url,
            "lastModified": timestamp,
            "description": description,
            "tags": all_tags,
        })

    return jsonify({"tag": tag, "photos": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

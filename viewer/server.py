#!/usr/bin/env python3
"""
server.py -- Flask backend for the Roost camera viewer dashboard.

Why this exists:
    The S3 bucket that stores camera frames is private. Accessing it from
    the browser directly would require either making the bucket public (bad)
    or embedding AWS credentials in the page JavaScript (also bad). Instead,
    this server holds the credentials server-side via the "picamera" AWS
    profile already set up in ~/.aws/credentials for the s3_uploader.py
    script, and hands the browser short-lived presigned URLs that grant
    temporary read access to individual S3 objects without exposing any
    credentials.

What it does:
    GET /              -- Serves static/index.html (the React single-page app).
    GET /api/photos    -- Lists the most recent objects in the S3 bucket and
                         returns a presigned URL for each so the browser can
                         load images directly from S3.
    GET /api/search    -- Queries a DynamoDB Global Secondary Index to find
                         photos that the vision Lambda tagged with a particular
                         label (e.g. "dog", "car", "package"), then returns
                         presigned URLs and metadata for those photos.

What it does NOT do:
    Camera control (WebSocket commands to start/stop the stream, trigger
    snapshots) does not route through this backend. The frontend JavaScript
    connects directly to the camera capture app's WebSocket endpoints on the
    Pi (ports 8080 and 8081). Those are plain local-network sockets with no
    credentials involved, so there is no reason to proxy them.
"""

import os

# Flask: the web framework.
#   Flask        -- the application class
#   jsonify      -- serializes a Python dict/list to a JSON HTTP response
#   send_from_directory -- safely serves a file from a directory (prevents
#                          directory traversal attacks)
#   request      -- gives access to the incoming HTTP request, including
#                   query string parameters (e.g. ?tag=dog)
from flask import Flask, jsonify, send_from_directory, request

# boto3: the AWS SDK for Python.
# Used here to talk to S3 (list objects, generate presigned URLs) and
# DynamoDB (query the tag Global Secondary Index).
import boto3

# ClientError is the exception boto3 raises when an AWS API call fails --
# e.g. the bucket does not exist, permissions are missing, or there is a
# network problem. We catch it explicitly in each route so we can return a
# meaningful HTTP error to the browser instead of letting an unhandled
# exception bubble up and crash the server.
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# The S3 bucket where s3_uploader.py deposits camera frames.
# This bucket has no public access; all reads go through presigned URLs.
BUCKET_NAME = "picamera-mcoughlin-frames"

# The named AWS profile from ~/.aws/credentials that has read access to
# BUCKET_NAME and DYNAMO_TABLE. Using a named profile avoids hardcoding
# credentials in this file or relying on environment variables.
AWS_PROFILE = "picamera"

# How long (in seconds) a presigned URL remains valid after it is generated.
# 3600 seconds = one hour. After this window, the URL stops working and the
# browser would see an HTTP 403 from S3. For a local dashboard that is
# refreshed periodically this is generous enough that photos remain loadable
# during a normal viewing session.
PRESIGNED_URL_EXPIRY_SECONDS = 3600

# Maximum number of photos returned by /api/photos. The bucket can grow
# indefinitely; returning every object would get slow. We sort by
# LastModified descending before truncating, so this is always the most
# recent N photos, not an arbitrary slice.
MAX_PHOTOS_RETURNED = 60

# The DynamoDB table name. A single table stores all Roost data using a
# composite primary key design (PK + SK). See roost-vision/handler.py for
# how the vision Lambda writes to this table.
DYNAMO_TABLE = "Roost"

# The user identifier used as part of DynamoDB partition keys.
# The schema supports multiple users (each user's data is namespaced under
# their ID), but this deployment is single-user.
USER_ID = "michael"


# ---------------------------------------------------------------------------
# Flask app and AWS client setup
# ---------------------------------------------------------------------------

# Create the Flask application.
#   static_folder="static"   -- tells Flask where to find static assets
#   static_url_path=""       -- serve those files at the root URL path
#                               (so /index.html works) rather than the
#                               default /static/ prefix
app = Flask(__name__, static_folder="static", static_url_path="")

# Create a boto3 Session pinned to the picamera AWS profile. A Session holds
# credentials and region configuration. All AWS clients created from this
# session inherit the same identity, so the profile name only needs to appear
# once here.
session = boto3.Session(profile_name=AWS_PROFILE)

# Low-level S3 client: used to list bucket contents and generate presigned URLs.
# "Client" maps directly to the S3 REST API; each method corresponds to one
# API call.
s3 = session.client("s3")

# Higher-level DynamoDB resource: lets us work with tables and items as Python
# objects rather than raw API response dictionaries. We use it to query the
# GSI1 index that the vision Lambda populates.
dynamodb = session.resource("dynamodb")

# Get a Table object pointing at the Roost table. This does not make a network
# call; it just creates a local handle so we can call table.query() and
# table.get_item() later.
table = dynamodb.Table(DYNAMO_TABLE)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """
    Serve the single-page React app.

    send_from_directory safely serves a file from a directory, preventing
    path traversal. The React app in static/index.html takes over from here
    and handles all UI rendering client-side.
    """
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/photos")
def list_photos():
    """
    Return the most recent photos from S3 as JSON.

    Flow:
        1. Call S3 list_objects_v2 to get all objects in the bucket.
           (S3 does not sort by modification time natively, so we list
           everything and sort in Python.)
        2. Sort by LastModified descending and truncate to MAX_PHOTOS_RETURNED.
        3. Generate a presigned GET URL for each object. A presigned URL is a
           normal HTTPS URL with time-limited AWS authentication parameters
           embedded in the query string. The browser can use it to fetch the
           image bytes directly from S3 without any credentials of its own.
        4. Return the array of photo metadata as a JSON response.

    Error handling:
        If the S3 list call fails (wrong bucket, missing IAM permissions,
        network error), return HTTP 502 so the frontend can show a useful
        error state. Individual presigned URL failures are silently skipped
        so one bad object does not abort the whole response.
    """
    # list_objects_v2 returns up to 1000 objects per call. For a camera that
    # takes occasional snapshots this is sufficient. If the bucket ever exceeds
    # 1000 objects, this would silently miss the oldest ones; proper pagination
    # (using the ContinuationToken) would be needed at that point.
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME)
    except ClientError as e:
        # 502 Bad Gateway is the right status code when our upstream dependency
        # (S3) returns an error. Passing the AWS error message through makes
        # it visible in the browser's network tab for debugging.
        return jsonify({"error": str(e)}), 502

    # "Contents" is absent entirely from the response when the bucket is empty.
    # Default to [] so the code below does not blow up with a KeyError.
    objects = response.get("Contents", [])

    # Sort newest-first. LastModified is a timezone-aware datetime object, so
    # Python can compare them directly with the < operator.
    objects.sort(key=lambda o: o["LastModified"], reverse=True)

    # Truncate after sorting so we always keep the most recent N objects, not
    # an arbitrary first-N from the S3 listing order.
    objects = objects[:MAX_PHOTOS_RETURNED]

    photos = []
    for obj in objects:
        # Generate a presigned URL for a GET request on this specific object.
        # The URL is valid for PRESIGNED_URL_EXPIRY_SECONDS from the moment
        # generate_presigned_url is called. After that window it returns 403.
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET_NAME, "Key": obj["Key"]},
                ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
            )
        except ClientError as e:
            # Skip this object if URL generation fails. This should be rare
            # (it would mean credentials became invalid mid-request) but
            # skipping is safer than aborting the whole response.
            continue

        photos.append({
            "key": obj["Key"],                            # S3 object key, e.g. "2024-01-15T12:34:56.jpg"
            "url": url,                                   # Presigned HTTPS URL the browser uses to load the image
            "lastModified": obj["LastModified"].isoformat(), # ISO 8601 timestamp string
            "size": obj["Size"],                          # File size in bytes
        })

    return jsonify({"photos": photos})


@app.route("/api/search")
def search_by_tag():
    """
    Search for photos by a vision tag and return them as JSON.

    Background:
        The vision Lambda (roost-vision/handler.py) runs Claude on each new
        photo uploaded to S3. Claude produces a text description and a list
        of object labels (tags). The Lambda writes one DynamoDB item per
        (image, tag) pair to the GSI1 index, so querying the GSI for a
        specific tag efficiently retrieves all images that contain that object.

    Query parameter:
        tag (str): The label to search for, e.g. "dog", "car", "package".
                   Whitespace is stripped and the value is lowercased before
                   querying, so "Dog" and " dog " both work.

    DynamoDB data model (relevant parts):

        TAG item (written by the Lambda, one per image-tag pair):
            GSI1PK:    USER#<user_id>#TAG#<tag>
            GSI1SK:    IMAGE#<timestamp>#<image_key>
            image_key: the S3 object key for the photo
            timestamp: the ISO timestamp of when the photo was processed

        IMAGE item (written by the Lambda, one per image):
            PK:          USER#<user_id>
            SK:          IMAGE#<timestamp>#<image_key>
            description: Claude's natural-language description of the photo
            tags:        list of all labels detected in this photo

        This is the standard single-table DynamoDB "adjacency list" pattern.
        The GSI query returns TAG items (cheap, index-only read), and we then
        do a point get_item lookup per image to retrieve the full description
        and complete tag list.

    Returns:
        JSON: { "tag": "<queried tag>", "photos": [ ...photo objects... ] }
        Each photo object: key, url, lastModified, description, tags.
    """
    # Read the "tag" query parameter from the URL (?tag=dog).
    # Strip whitespace and lowercase so the caller does not need to worry
    # about casing or accidental spaces.
    tag = request.args.get("tag", "").strip().lower()

    # Reject the request early if the tag parameter is missing or empty.
    # Running a DynamoDB query with an empty partition key would return
    # every item under a malformed key, which is not useful.
    if not tag:
        return jsonify({"error": "missing 'tag' query parameter"}), 400

    # Query the GSI1 index for all TAG items whose partition key matches
    # this user and tag combination. ScanIndexForward=False means DynamoDB
    # returns items sorted by the sort key in descending order, which puts
    # the newest timestamps first since the sort key is prefixed with a
    # timestamp.
    try:
        tag_response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"USER#{USER_ID}#TAG#{tag}"},
            ScanIndexForward=False,  # newest first by sort key
        )
    except ClientError as e:
        return jsonify({"error": str(e)}), 502

    results = []
    for tag_item in tag_response.get("Items", []):
        # Each TAG item stores references back to the image it belongs to.
        image_key = tag_item.get("image_key")
        timestamp = tag_item.get("timestamp")

        # Skip malformed items that are missing required fields. In normal
        # operation this should never happen, but it is worth guarding against
        # in case a bug in a previous Lambda version wrote incomplete records.
        if not image_key or not timestamp:
            continue

        # Fetch the full IMAGE item so we can include the description and the
        # complete list of all tags for this photo (not just the one we searched
        # for -- a photo tagged "dog" might also have "leash", "grass", etc.).
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
                # Tags may be stored as plain strings or as dicts with a "label"
                # key depending on which version of the Lambda wrote them.
                # Normalize both forms to plain strings so the frontend does not
                # need to handle the difference.
                all_tags = [t.get("label") if isinstance(t, dict) else t
                            for t in image_item.get("tags", [])]
        except ClientError:
            # If the IMAGE record fetch fails, continue and return the photo
            # without a description. A presigned URL without metadata is still
            # more useful than omitting the result entirely.
            pass

        # Generate a presigned URL so the browser can load the image from
        # the private S3 bucket.
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET_NAME, "Key": image_key},
                ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
            )
        except ClientError:
            # If URL generation fails, skip this result entirely. Returning a
            # result without a loadable image URL would just show a broken
            # image placeholder in the gallery.
            continue

        results.append({
            "key": image_key,
            "url": url,
            "lastModified": timestamp,
            "description": description,  # Claude's text description of the photo
            "tags": all_tags,            # All object labels detected in the photo
        })

    return jsonify({"tag": tag, "photos": results})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Read the port from the PORT environment variable so it can be overridden
    # at startup without editing this file. Defaults to 5000.
    # host="0.0.0.0" binds to all network interfaces so the dashboard is
    # reachable from other machines on the local network (e.g. from a laptop
    # when the server is running on a Pi or another machine), not just from
    # localhost on the same machine.
    #
    # Note: Flask's built-in development server is appropriate for this use
    # case (single user, local network, no public exposure). Do not point it
    # at the public internet.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

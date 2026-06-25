#
# Roost
# Bottle Blue LLC
# Authored by Michael Coughlin
# 2026-06-25
# https://bottlebluellc.com
# https://blog.hiimmichael.com/articles/roost-setup-tutorial.html
"""
Roost vision Lambda.

Triggered by S3 object-created events. For each new image:
  1. download it from S3
  2. send it to Claude (vision), which returns a JSON object with a
     natural-language description and a list of search tags
  3. write the image record (and one row per tag) to the Roost DynamoDB
     table, using a single-table design

The dashboard then queries DynamoDB to search photos by tag and show the
description.

Single-table design (table name: Roost)
---------------------------------------
Main keys:
  PK = USER#<user_id>
  SK = IMAGE#<timestamp>#<image_key>

The image record holds the description, the full tag list, and metadata.

For each tag, an additional lightweight row is written that is indexed by
GSI1 for global (cross-camera) tag search within a user:
  GSI1PK = USER#<user_id>#TAG#<tag>
  GSI1SK = IMAGE#<timestamp>#<image_key>

So "find all of this user's photos tagged dog, newest first" is a single
Query on GSI1 against USER#<user_id>#TAG#dog.

camera_id is stored as a plain attribute (not in the key), since tag
search spans all of a user's cameras.

This is a normal zip Lambda. Its only dependency beyond the AWS runtime is
the `anthropic` package (see requirements.txt). No TensorFlow, no
container image.
"""

import base64
import json
import os
import urllib.parse

import boto3
import anthropic

# --- Config (overridable via Lambda environment variables) ----------------
DYNAMO_TABLE = os.environ.get("DYNAMO_TABLE", "Roost")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "400"))
# Single-user for now; the schema supports multi-user when needed. Override
# via env var per deployment if you ever run separate users.
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "michael")
# The capture app writes one camera's frames today. If you add cameras and
# encode the camera in the object key prefix, parse it here instead.
DEFAULT_CAMERA_ID = os.environ.get("DEFAULT_CAMERA_ID", "cam1")
# ---------------------------------------------------------------------------

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMO_TABLE)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

PROMPT = """You are analyzing a photo from a home security/monitoring camera.

Respond with ONLY a JSON object, no other text, no markdown fences, in exactly this shape:

{
  "description": "one or two natural sentences describing what the photo shows",
  "tags": ["lowercase", "single-or-short", "search", "keywords"]
}

Guidelines:
- The description should be factual and specific to what you actually see. Note people, animals, vehicles, objects, and the setting. Do not invent details you cannot see.
- Tags should be 5 to 12 short lowercase keywords useful for searching later: the main subjects, objects, setting, and notable attributes (e.g. "person", "dog", "car", "nighttime", "driveway", "package", "delivery").
- If the image is unclear, empty, or shows nothing notable, say so in the description and use tags like "empty" or "unclear".
- Output the raw JSON object only."""


def extract_media_type(key: str) -> str:
    ext = os.path.splitext(key)[1].lower()
    return MEDIA_TYPES.get(ext, "image/jpeg")


def analyze_image(data: bytes, media_type: str) -> dict:
    """Send the image to Claude, parse its JSON response."""
    b64 = base64.standard_b64encode(data).decode("utf-8")

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )

    raw = "".join(
        block.text for block in message.content if block.type == "text"
    ).strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)
    description = parsed.get("description", "").strip()
    tags = [str(t).lower().strip() for t in parsed.get("tags", []) if str(t).strip()]
    return {"description": description, "tags": tags}


def write_records(user_id, camera_id, image_key, timestamp, description, tags, bucket):
    """
    Write the single-table records for one image:
      - one IMAGE record (holds description + full tag list + metadata)
      - one TAG-index record per tag (for global tag search via GSI1)

    Uses a batch writer so all rows go in together.
    """
    sk = f"IMAGE#{timestamp}#{image_key}"

    with table.batch_writer() as batch:
        # Main image record
        batch.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": sk,
                "entity_type": "IMAGE",
                "image_key": image_key,
                "bucket": bucket,
                "camera_id": camera_id,
                "timestamp": timestamp,
                "description": description,
                "tags": tags,
                # Mirror the image record into GSI1 under a generic key too,
                # so "all images for a user, newest first" is also a GSI1
                # query if you ever want it. Harmless if unused.
                "GSI1PK": f"USER#{user_id}#IMAGES",
                "GSI1SK": sk,
            }
        )

        # One row per tag, indexed for tag search
        for tag in tags:
            batch.put_item(
                Item={
                    "PK": f"USER#{user_id}",
                    "SK": f"TAG#{tag}#{timestamp}#{image_key}",
                    "entity_type": "TAG",
                    "tag": tag,
                    "image_key": image_key,
                    "camera_id": camera_id,
                    "timestamp": timestamp,
                    "GSI1PK": f"USER#{user_id}#TAG#{tag}",
                    "GSI1SK": sk,
                }
            )


def handler(event, context):
    processed = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        if not key.lower().endswith(tuple(MEDIA_TYPES.keys())):
            print(f"Skipping non-image key: {key}")
            continue

        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read()
        except Exception as e:
            print(f"Failed to fetch s3://{bucket}/{key}: {e}")
            continue

        try:
            result = analyze_image(data, extract_media_type(key))
        except Exception as e:
            print(f"Claude analysis failed for {key}: {e}")
            continue

        last_modified = obj.get("LastModified")
        timestamp = last_modified.isoformat() if last_modified else ""

        try:
            write_records(
                user_id=DEFAULT_USER_ID,
                camera_id=DEFAULT_CAMERA_ID,
                image_key=key,
                timestamp=timestamp,
                description=result["description"],
                tags=result["tags"],
                bucket=bucket,
            )
            print(f"Analyzed {key}: {result['tags']}")
            processed.append({"key": key, "tags": result["tags"]})
        except Exception as e:
            print(f"Failed to write DynamoDB records for {key}: {e}")

    return {"statusCode": 200, "body": json.dumps({"processed": processed})}

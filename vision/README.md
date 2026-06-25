# vision

An AWS Lambda that runs Claude's vision model on every new photo landing
in the S3 bucket. Claude returns a natural-language description plus a list
of search tags, which get written to the `Roost` DynamoDB table. The
dashboard then lets you search photos by tag and read the description.

```
new image in S3 --> S3 event --> Lambda (Claude vision)
                                     |  description + tags as JSON
                                     v
                              DynamoDB: Roost (single-table)
                                     ^
                                     |  query GSI1 by tag
                                  dashboard search
```

No TensorFlow, no container image, no Step Functions. One small zip Lambda.

## Files

- `handler.py` -- the Lambda. Downloads each new image, sends it to Claude,
  parses the JSON response, and writes records to DynamoDB.
- `requirements.txt` -- just the `anthropic` package.
- `lambda-execution-policy.json` -- the Lambda's execution role
  permissions (read S3, write the Roost table, log).
- `dashboard-read-policy.json` -- read-only DynamoDB access for the
  dashboard's IAM user, so the search endpoint can query the table.
- `s3-notification.example.json` -- template for wiring the bucket's
  event notification to the Lambda. Copy to `s3-notification.json` and
  fill in your account ID.

## Data model (single-table design)

The table is named `Roost` with generic `PK`/`SK` keys and a `GSI1` index.
For each analyzed image the handler writes:

- One IMAGE record holding the description, full tag list, and metadata:
  - `PK = USER#<user>`, `SK = IMAGE#<timestamp>#<image_key>`
- One TAG record per tag, indexed in GSI1 for fast tag search:
  - `GSI1PK = USER#<user>#TAG#<tag>`, `GSI1SK = IMAGE#<timestamp>#<image_key>`

So "find all of a user's photos tagged dog, newest first" is a single
query on GSI1. The schema supports multiple cameras and users without a
redesign; `user_id` and `camera_id` are set via the `DEFAULT_USER_ID` and
`DEFAULT_CAMERA_ID` environment variables.

## Packaging note

Lambda runs Linux on Python 3.11. When installing dependencies, fetch
Linux wheels explicitly or the function will fail at import:

```bash
python -m pip install -r requirements.txt -t package \
  --platform manylinux2014_x86_64 --python-version 3.11 --only-binary=:all:
cp handler.py package/
cd package && zip -r ../function.zip . && cd ..
```

## Full setup

The complete deploy (DynamoDB table, IAM role, Lambda creation, S3 event
wiring, and adding search to the dashboard) is covered step by step on the
blog, not here:

**[blog.hiimmichael.com/articles/roost-setup-tutorial.html](https://blog.hiimmichael.com/articles/roost-setup-tutorial.html)**

---

Part of [Roost](../README.md), an open-source, self-hosted camera pipeline by Bottle Blue LLC.

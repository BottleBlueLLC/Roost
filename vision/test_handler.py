"""Roost vision Lambda (S3 object-created -> Claude vision -> DynamoDB): the
media-type mapping, model-response parsing (raw JSON, fenced JSON, truncated
JSON, prompt-injected tag floods), the single-table writes including the
month-sharded PK and the stale-TAG-row cleanup a re-analysis has to do, and -
load-bearing - the rule that ANY per-record failure raises so Lambda's async
retry/DLQ engages instead of dropping the frame forever.

This function's entrypoint is ``handler.py``, not ``lambda_function.py``, so
the shared ``load_lambda`` fixture cannot load it; it is imported directly
here. The ``anthropic`` SDK is not installed on a dev/CI box (its vendored copy
carries linux-only wheels), so a stub module stands in, and the loaded module's
``s3``/``table``/``client`` are all replaced - no network, no AWS."""

import datetime
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

VISION_DIR = Path(__file__).resolve().parent

# The function's configured Lambda timeout. CLAUDE_TIMEOUT must stay well under
# it (see the INVARIANT comment in handler.py): until 2026-07-19 the Claude call
# budget was 60s inside a 60s Lambda, so a stalled upstream burned the whole
# invocation and the `raise RuntimeError` that drives retry/DLQ never ran.
LAMBDA_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Stubs for every external collaborator
# ---------------------------------------------------------------------------
class _Block:
    def __init__(self, text, type="text"):
        self.text = text
        self.type = type


class _Message:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    def __init__(self):
        self.calls = []
        self.reply = "{}"
        self.error = None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if isinstance(self.reply, str):
            # A non-text block must be ignored by the parser, not crash it.
            return _Message([_Block(self.reply), _Block(None, type="thinking")])
        return _Message(self.reply)


class _FakeAnthropic:
    def __init__(self, api_key=None, timeout=None):
        self.api_key = api_key
        self.timeout = timeout
        self.messages = _FakeMessages()


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self, data=b"\x89PNGfake", last_modified=None, error=None):
        self.data = data
        self.last_modified = last_modified
        self.error = error
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        if self.error is not None:
            raise self.error
        obj = {"Body": _FakeBody(self.data)}
        if self.last_modified is not None:
            obj["LastModified"] = self.last_modified
        return obj


class _FakeBatch:
    def __init__(self, table):
        self._table = table

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_item(self, Item):
        self._table.puts.append(Item)

    def delete_item(self, Key):
        self._table.deletes.append(Key)


class _FakeTable:
    def __init__(self, existing=None, get_error=None, write_error=None):
        self.existing = existing
        self.get_error = get_error
        self.write_error = write_error
        self.get_keys = []
        self.puts = []
        self.deletes = []

    def get_item(self, Key):
        self.get_keys.append(Key)
        if self.get_error is not None:
            raise self.get_error
        return {"Item": self.existing} if self.existing is not None else {}

    def batch_writer(self):
        if self.write_error is not None:
            raise self.write_error
        return _FakeBatch(self)

    # Convenience views the assertions read
    def items_of(self, entity_type):
        return [i for i in self.puts if i["entity_type"] == entity_type]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
@pytest.fixture()
def roost(monkeypatch):
    """Import handler.py with a stub anthropic SDK, then replace
    its module-level s3/table/client with fakes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-anthropic-key")

    fake_sdk = types.ModuleType("anthropic")
    fake_sdk.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)

    alias = "roost_vision_handler"
    monkeypatch.delitem(sys.modules, alias, raising=False)
    spec = importlib.util.spec_from_file_location(
        alias, VISION_DIR / "handler.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, alias, module)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "s3", _FakeS3())
    monkeypatch.setattr(module, "table", _FakeTable())
    return module


def s3_event(*keys, bucket="roost-frames"):
    return {"Records": [{"s3": {"bucket": {"name": bucket},
                                "object": {"key": k}}} for k in keys]}


GOOD_REPLY = json.dumps({
    "description": "A dog on a driveway at night.",
    "tags": ["Dog", "driveway", "nighttime"],
})


def _reply(module, text):
    module.client.messages.reply = text


# ---------------------------------------------------------------------------
class TestTimeoutInvariant:
    """The Claude budget must stay strictly below the Lambda timeout, or the
    handler is killed before the `raise` that drives async retry/DLQ can run."""

    def test_claude_timeout_is_60s_and_well_under_the_lambda_timeout(self, roost):
        assert roost.CLAUDE_TIMEOUT == 60.0
        assert roost.CLAUDE_TIMEOUT < LAMBDA_TIMEOUT_SECONDS
        # Not merely below: one invocation processes a batch of frames, so the
        # per-call budget needs real headroom. A 1:5 ratio leaves room for
        # several frames plus the S3 read and DynamoDB writes.
        assert roost.CLAUDE_TIMEOUT <= LAMBDA_TIMEOUT_SECONDS / 5

    def test_the_budget_is_actually_handed_to_the_sdk(self, roost):
        # An unenforced constant is the same bug in a different place.
        assert roost.client.timeout == roost.CLAUDE_TIMEOUT
        assert roost.client.api_key == "unit-test-anthropic-key"


class TestMediaTypes:
    def test_known_extensions_map_case_insensitively(self, roost):
        assert roost.extract_media_type("a/b/frame.jpg") == "image/jpeg"
        assert roost.extract_media_type("frame.JPEG") == "image/jpeg"
        assert roost.extract_media_type("frame.PNG") == "image/png"
        assert roost.extract_media_type("frame.gif") == "image/gif"
        assert roost.extract_media_type("frame.webp") == "image/webp"

    def test_unsupported_extension_falls_back_to_jpeg(self, roost):
        assert roost.extract_media_type("frame.bmp") == "image/jpeg"
        assert roost.extract_media_type("frame_with_no_extension") == "image/jpeg"

    def test_a_non_image_key_is_skipped_permanently_not_retried(self, roost):
        # A permanent skip must NOT raise: retrying a .txt forever is a loop.
        r = roost.handler(s3_event("notes/readme.txt"), None)
        assert r["statusCode"] == 200
        assert json.loads(r["body"])["processed"] == []
        assert roost.s3.calls == []


class TestResponseParsing:
    def test_raw_json_is_parsed(self, roost):
        _reply(roost, GOOD_REPLY)
        out = roost.analyze_image(b"img", "image/png")
        assert out["description"] == "A dog on a driveway at night."
        assert out["tags"] == ["dog", "driveway", "nighttime"]

    @pytest.mark.parametrize("fence", ["```json\n{body}\n```", "```\n{body}\n```"])
    def test_markdown_fences_are_stripped(self, roost, fence):
        _reply(roost, fence.replace("{body}", GOOD_REPLY))
        assert roost.analyze_image(b"img", "image/png")["tags"] == [
            "dog", "driveway", "nighttime"]

    def test_truncated_json_raises_so_the_caller_can_fail_the_record(self, roost):
        # A max_tokens cutoff mid-object: this must surface, never be papered
        # over into an empty-tag record that looks like a successful analysis.
        _reply(roost, '{"description": "A dog on a dri')
        with pytest.raises(json.JSONDecodeError):
            roost.analyze_image(b"img", "image/jpeg")

    def test_missing_fields_degrade_to_empty_not_a_crash(self, roost):
        _reply(roost, "{}")
        assert roost.analyze_image(b"img", "image/jpeg") == {
            "description": "", "tags": []}

    def test_tags_of_the_wrong_type_are_discarded(self, roost):
        # Model output is driven by attacker-controllable image content.
        _reply(roost, json.dumps({"description": "x", "tags": "dog,cat"}))
        assert roost.analyze_image(b"img", "image/jpeg")["tags"] == []

    def test_tag_flood_and_oversized_tags_are_capped(self, roost):
        _reply(roost, json.dumps({
            "description": "d",
            "tags": ["tag%d" % i for i in range(100)] + ["  ", "x" * 200],
        }))
        out = roost.analyze_image(b"img", "image/jpeg")
        assert len(out["tags"]) == 25
        assert all(len(t) <= 64 for t in out["tags"])

    def test_blank_tags_are_dropped_and_long_descriptions_truncated(self, roost):
        _reply(roost, json.dumps({"description": "z" * 5000,
                                  "tags": ["  ", "Cat  "]}))
        out = roost.analyze_image(b"img", "image/jpeg")
        assert len(out["description"]) == 2000
        assert out["tags"] == ["cat"]

    def test_the_image_is_sent_as_base64_with_its_media_type(self, roost):
        import base64
        _reply(roost, GOOD_REPLY)
        roost.analyze_image(b"rawbytes", "image/webp")
        source = roost.client.messages.calls[0]["messages"][0]["content"][0]["source"]
        assert source["media_type"] == "image/webp"
        assert base64.standard_b64decode(source["data"]) == b"rawbytes"
        assert roost.client.messages.calls[0]["model"] == roost.CLAUDE_MODEL


class TestWriteRecords:
    def test_one_image_row_plus_one_row_per_tag(self, roost):
        roost.write_records("michael", "cam1", "f.jpg", "2026-07-19T10:00:00",
                            "a dog", ["dog", "night"], "roost-frames")
        images = roost.table.items_of("IMAGE")
        tags = roost.table.items_of("TAG")
        assert len(images) == 1 and len(tags) == 2
        assert images[0]["GSI1PK"] == "USER#michael#IMAGES"
        assert images[0]["tags"] == ["dog", "night"]
        assert {t["GSI1PK"] for t in tags} == {
            "USER#michael#TAG#dog", "USER#michael#TAG#night"}
        # Every row shares the image record's sort key via GSI1SK, so a tag
        # query orders by capture time.
        assert {t["GSI1SK"] for t in tags} == {images[0]["SK"]}

    def test_the_base_partition_is_sharded_by_month(self, roost):
        roost.write_records("michael", "cam1", "f.jpg", "2026-07-19T10:00:00",
                            "d", ["dog"], "b")
        assert all(i["PK"] == "USER#michael#2026-07" for i in roost.table.puts)

    def test_a_missing_timestamp_falls_back_to_an_unsharded_partition(self, roost):
        roost.write_records("michael", "cam1", "f.jpg", "", "d", ["dog"], "b")
        assert all(i["PK"] == "USER#michael#unsharded" for i in roost.table.puts)

    def test_reanalysis_deletes_tag_rows_the_new_analysis_dropped(self, roost):
        # The model is nondeterministic, so a retry can produce a different tag
        # set; the rows it no longer produces must not linger forever.
        roost.table.existing = {"tags": ["dog", "cat", "night"]}
        roost.write_records("michael", "cam1", "f.jpg", "2026-07-19T10:00:00",
                            "d", ["dog"], "b")
        assert roost.table.deletes == [
            {"PK": "USER#michael#2026-07",
             "SK": "TAG#cat#2026-07-19T10:00:00#f.jpg"},
            {"PK": "USER#michael#2026-07",
             "SK": "TAG#night#2026-07-19T10:00:00#f.jpg"},
        ]
        assert [t["tag"] for t in roost.table.items_of("TAG")] == ["dog"]

    def test_a_first_time_image_deletes_nothing(self, roost):
        roost.write_records("michael", "cam1", "f.jpg", "2026-07-19T10:00:00",
                            "d", ["dog"], "b")
        assert roost.table.deletes == []


class TestHandlerHappyPath:
    def test_an_s3_event_writes_the_records_and_returns_200(self, roost, monkeypatch):
        monkeypatch.setattr(roost, "s3", _FakeS3(
            data=b"frame-bytes",
            last_modified=datetime.datetime(2026, 7, 19, 10, 0, 0)))
        _reply(roost, GOOD_REPLY)

        r = roost.handler(s3_event("cam1/frame+1.jpg"), None)

        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["processed"] == [
            {"key": "cam1/frame 1.jpg", "tags": ["dog", "driveway", "nighttime"]}]
        # The S3 event URL-encodes the key; the raw key is what we fetch/store.
        assert roost.s3.calls == [("roost-frames", "cam1/frame 1.jpg")]
        image = roost.table.items_of("IMAGE")[0]
        assert image["SK"] == "IMAGE#2026-07-19T10:00:00#cam1/frame 1.jpg"
        assert image["bucket"] == "roost-frames"
        assert image["camera_id"] == roost.DEFAULT_CAMERA_ID
        assert len(roost.table.items_of("TAG")) == 3

    def test_a_missing_last_modified_still_writes(self, roost):
        _reply(roost, GOOD_REPLY)
        r = roost.handler(s3_event("frame.png"), None)
        assert r["statusCode"] == 200
        assert roost.table.items_of("IMAGE")[0]["SK"] == "IMAGE##frame.png"

    def test_an_empty_event_is_a_no_op_200(self, roost):
        assert roost.handler({}, None)["statusCode"] == 200


class TestHandlerFailuresRaise:
    """Every per-record failure must fail the whole invocation.

    Lambda's async retry and DLQ are driven purely by the invocation raising.
    Returning 200 with the failure buried in the body would drop the frame
    permanently and silently - the exact behaviour this suite exists to pin.
    """

    def test_an_s3_fetch_failure_raises(self, roost, monkeypatch):
        monkeypatch.setattr(roost, "s3", _FakeS3(error=RuntimeError("access denied")))
        with pytest.raises(RuntimeError, match="1 record"):
            roost.handler(s3_event("frame.jpg"), None)
        assert roost.table.puts == []

    def test_a_claude_failure_raises(self, roost):
        roost.client.messages.error = RuntimeError("overloaded")
        with pytest.raises(RuntimeError, match="analyze"):
            roost.handler(s3_event("frame.jpg"), None)
        assert roost.table.puts == []

    def test_a_truncated_model_response_raises(self, roost):
        _reply(roost, '{"description": "trunc')
        with pytest.raises(RuntimeError, match="analyze"):
            roost.handler(s3_event("frame.jpg"), None)

    def test_a_dynamodb_write_failure_raises(self, roost, monkeypatch):
        _reply(roost, GOOD_REPLY)
        monkeypatch.setattr(roost, "table",
                            _FakeTable(write_error=RuntimeError("throttled")))
        with pytest.raises(RuntimeError, match="dynamo write"):
            roost.handler(s3_event("frame.jpg"), None)

    def test_a_get_item_failure_also_raises(self, roost, monkeypatch):
        _reply(roost, GOOD_REPLY)
        monkeypatch.setattr(roost, "table",
                            _FakeTable(get_error=RuntimeError("throttled")))
        with pytest.raises(RuntimeError, match="dynamo write"):
            roost.handler(s3_event("frame.jpg"), None)

    def test_one_bad_frame_in_a_batch_still_fails_the_invocation(
            self, roost, monkeypatch):
        # The good frame is written, but the invocation must still raise so the
        # bad one is retried. Retries are idempotent by key, so re-processing
        # the good frame is safe.
        calls = []

        class _MixedS3(_FakeS3):
            def get_object(self, Bucket, Key):
                calls.append(Key)
                if Key == "bad.jpg":
                    raise RuntimeError("gone")
                return {"Body": _FakeBody(b"ok")}

        monkeypatch.setattr(roost, "s3", _MixedS3())
        _reply(roost, GOOD_REPLY)
        with pytest.raises(RuntimeError) as excinfo:
            roost.handler(s3_event("good.jpg", "bad.jpg", "skip.txt"), None)
        assert "1 record(s) failed" in str(excinfo.value)
        assert calls == ["good.jpg", "bad.jpg"]
        assert len(roost.table.items_of("IMAGE")) == 1

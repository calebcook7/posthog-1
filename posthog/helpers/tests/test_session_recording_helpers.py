from datetime import datetime, timedelta, timezone
from typing import List, cast

import pytest
from pytest_mock import MockerFixture

from posthog.session_recordings.session_recording_helpers import (
    RecordingSegment,
    SessionRecordingEventSummary,
    SnapshotData,
    SnapshotDataTaggedWithWindowId,
    byte_size_dict,
    chunk_replay_events_by_window,
    compress_replay_events,
    decompress_chunked_snapshot_data,
    generate_inactive_segments_for_range,
    get_active_segments_from_event_list,
    get_events_summary_from_snapshot_data,
    is_active_event,
    reduce_replay_events_by_window,
    split_replay_events,
)

MILLISECOND_TIMESTAMP = round(datetime(2019, 1, 1).timestamp() * 1000)


def create_activity_data(timestamp: datetime, is_active: bool):
    return SessionRecordingEventSummary(
        timestamp=round(timestamp.timestamp() * 1000),
        type=3,
        data=dict(source=1 if is_active else -1),
    )


def mock_capture_flow(events: List[dict], max_size_bytes=512 * 1024) -> List[dict]:
    replay_events, other_events = split_replay_events(events)
    replay_events = compress_replay_events(replay_events)

    # NOTE: Legacy flow -> reducing based on the max_size for Kafka and compressing
    replay_events = reduce_replay_events_by_window(replay_events, max_size_bytes=max_size_bytes)
    replay_events = chunk_replay_events_by_window(replay_events, max_size_bytes=max_size_bytes)

    return replay_events + other_events


def test_preprocess_with_no_recordings():
    events = [{"event": "$pageview"}, {"event": "$pageleave"}]
    assert mock_capture_flow(events) == events


def test_preprocess_recording_event_groups_snapshots_split_by_session_and_window_id():
    events = [
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$snapshot_data": {"type": 2, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$snapshot_data": {"type": 1, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "5678",
                "$window_id": "1",
                "$snapshot_data": {"type": 1, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "5678",
                "$window_id": "2",
                "$snapshot_data": {"type": 1, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
    ]

    preprocessed = mock_capture_flow(events)
    assert preprocessed != events
    assert len(preprocessed) == 3
    expected_session_ids = ["1234", "5678", "5678"]
    expected_window_ids = [None, "1", "2"]
    expected_data_items = [2, 1, 1]
    for index, result in enumerate(preprocessed):
        assert result["event"] == "$snapshot"
        assert result["properties"]["$session_id"] == expected_session_ids[index]
        assert result["properties"].get("$window_id") == expected_window_ids[index]
        assert result["properties"]["distinct_id"] == "abc123"

        assert len(result["properties"]["$snapshot_data"]["data_items"]) == expected_data_items[index]
        assert result["event"] == "$snapshot"

    # it does not rechunk already chunked events
    assert mock_capture_flow(preprocessed) == preprocessed


def test_compression_and_grouping(raw_snapshot_events, mocker: MockerFixture):
    mocker.patch("posthog.models.utils.UUIDT", return_value="0178495e-8521-0000-8e1c-2652fa57099b")
    mocker.patch("time.time", return_value=0)

    assert list(mock_capture_flow(raw_snapshot_events)) == [
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$window_id": "1",
                "$snapshot_data": {
                    "compression": "gzip-base64",
                    "data_items": [
                        "H4sIAAAAAAAC//v/r5pBiaGEoZKhgCEVyLJiUGAwYtABkiDRTIZcoGgxkJUIZBVA5Q0ZTBlMGMwYjBkMgNACTEJgLQMA4qp+DFAAAAA=",
                        "H4sIAAAAAAAC//v/r5pBiaGEoZKhgCEVyLJiUGAwZtABkiDRTIZcoGgxkJUIZBVA5Q0ZTBlMGMyA6gyA0AJMQmAtAwBtJK3GUAAAAA==",
                    ],
                    "has_full_snapshot": True,
                    "events_summary": [
                        {"timestamp": MILLISECOND_TIMESTAMP, "type": 2, "data": {}},
                        {"timestamp": MILLISECOND_TIMESTAMP, "type": 3, "data": {}},
                    ],
                },
                "distinct_id": "abc123",
            },
        }
    ]


def test_decompression_results_in_same_data(raw_snapshot_events):
    # Check the encoded size so that we can choose a chunk size that will result in multiple chunks
    assert byte_size_dict(compress_replay_events(raw_snapshot_events)[0]) == 371

    assert len(list(mock_capture_flow(raw_snapshot_events, 1000))) == 1
    assert compress_decompress_and_extract(raw_snapshot_events, 1000) == [
        raw_snapshot_events[0]["properties"]["$snapshot_data"],
        raw_snapshot_events[1]["properties"]["$snapshot_data"],
    ]
    assert len(list(mock_capture_flow(raw_snapshot_events, 400))) == 2
    assert compress_decompress_and_extract(raw_snapshot_events, 400) == [
        raw_snapshot_events[0]["properties"]["$snapshot_data"],
        raw_snapshot_events[1]["properties"]["$snapshot_data"],
    ]


def test_has_full_snapshot_property(raw_snapshot_events):
    compressed = list(mock_capture_flow(raw_snapshot_events))
    assert len(compressed) == 1
    assert compressed[0]["properties"]["$snapshot_data"]["has_full_snapshot"]

    raw_snapshot_events[0]["properties"]["$snapshot_data"]["type"] = 0
    compressed = list(mock_capture_flow(raw_snapshot_events))
    assert len(compressed) == 1
    assert not compressed[0]["properties"]["$snapshot_data"]["has_full_snapshot"]


def test_decompress_uncompressed_events_returns_unmodified_events(raw_snapshot_events):
    snapshot_data_tagged_with_window_id = []
    raw_snapshot_data = []
    for event in raw_snapshot_events:
        snapshot_data_tagged_with_window_id.append(
            SnapshotDataTaggedWithWindowId(snapshot_data=event["properties"]["$snapshot_data"], window_id="1")
        )
        raw_snapshot_data.append(event["properties"]["$snapshot_data"])

    assert (
        decompress_chunked_snapshot_data(snapshot_data_tagged_with_window_id)["snapshot_data_by_window_id"]["1"]
        == raw_snapshot_data
    )


def test_decompress_ignores_if_not_enough_chunks(raw_snapshot_events):
    raw_snapshot_data = [event["properties"]["$snapshot_data"] for event in raw_snapshot_events]
    snapshot_data_list = [
        event["properties"]["$snapshot_data"] for event in mock_capture_flow(raw_snapshot_events, 100)
    ]
    window_id = "abc123"
    snapshot_list = []
    for snapshot_data in snapshot_data_list:
        snapshot_list.append(SnapshotDataTaggedWithWindowId(window_id=window_id, snapshot_data=snapshot_data))

    snapshot_list.append(
        SnapshotDataTaggedWithWindowId(
            snapshot_data={
                "chunk_id": "unique_id",
                "chunk_index": 1,
                "chunk_count": 2,
                "data": {},
                "compression": "gzip",
                "has_full_snapshot": False,
            },
            window_id=window_id,
        )
    )

    assert decompress_chunked_snapshot_data(snapshot_list)["snapshot_data_by_window_id"][window_id] == raw_snapshot_data


def test_decompress_deduplicates_if_duplicate_chunks(raw_snapshot_events):
    raw_snapshot_data = [event["properties"]["$snapshot_data"] for event in raw_snapshot_events]
    snapshot_data_list = [
        event["properties"]["$snapshot_data"] for event in mock_capture_flow(raw_snapshot_events, 10)
    ]  # makes 12 chunks
    # take the first four chunks twice, then the remainder, and then again the first four chunks twice from snapshot_data_list
    snapshot_data_list = (
        snapshot_data_list[:4]
        + snapshot_data_list[:4]
        + snapshot_data_list[4:]
        + snapshot_data_list[:4]
        + snapshot_data_list[:4]
    )

    window_id = "abc123"
    snapshot_list = []
    for snapshot_data in snapshot_data_list:
        snapshot_list.append(SnapshotDataTaggedWithWindowId(window_id=window_id, snapshot_data=snapshot_data))

    assert decompress_chunked_snapshot_data(snapshot_list)["snapshot_data_by_window_id"][window_id] == raw_snapshot_data


def test_decompress_ignores_if_too_few_chunks_even_after_deduplication(raw_snapshot_events):
    snapshot_data_list = [
        event["properties"]["$snapshot_data"] for event in mock_capture_flow(raw_snapshot_events, 20)
    ]  # makes 12 chunks

    assert len(snapshot_data_list) == 12
    # take the first four chunks four times, then not quite all the remainder
    # leaves more than 12 chunks in total, but not enough to decompress
    snapshot_data_list = (
        snapshot_data_list[:4]
        + snapshot_data_list[:4]
        + snapshot_data_list[:4]
        + snapshot_data_list[:4]
        + snapshot_data_list[8:-1]
    )

    window_id = "abc123"
    snapshot_list = []
    for snapshot_data in snapshot_data_list:
        snapshot_list.append(SnapshotDataTaggedWithWindowId(window_id=window_id, snapshot_data=snapshot_data))

    assert decompress_chunked_snapshot_data(snapshot_list)["snapshot_data_by_window_id"][window_id] == []


def test_paginate_decompression(chunked_and_compressed_snapshot_events):
    snapshot_data = [
        SnapshotDataTaggedWithWindowId(
            snapshot_data=event["properties"]["$snapshot_data"], window_id=event["properties"].get("$window_id")
        )
        for event in chunked_and_compressed_snapshot_events
    ]

    # Get the first chunk
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, 1, 0)
    assert paginated_events["has_next"] is True
    assert cast(SnapshotData, paginated_events["snapshot_data_by_window_id"][None][0])["type"] == 4
    assert len(paginated_events["snapshot_data_by_window_id"][None]) == 2  # 2 events in a chunk

    # Get the second chunk
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, 1, 1)
    assert paginated_events["has_next"] is False
    assert cast(SnapshotData, paginated_events["snapshot_data_by_window_id"]["1"][0])["type"] == 3
    assert len(paginated_events["snapshot_data_by_window_id"]["1"]) == 2  # 2 events in a chunk

    # Limit exceeds the length
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, 10, 0)
    assert paginated_events["has_next"] is False
    assert len(paginated_events["snapshot_data_by_window_id"]["1"]) == 2
    assert len(paginated_events["snapshot_data_by_window_id"][None]) == 2

    # Offset exceeds the length
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, 10, 2)
    assert paginated_events["has_next"] is False
    assert paginated_events["snapshot_data_by_window_id"] == {}

    # Non sequential snapshots
    snapshot_data = snapshot_data[-3:] + snapshot_data[0:-3]
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, 10, 0)
    assert paginated_events["has_next"] is False
    assert len(paginated_events["snapshot_data_by_window_id"]["1"]) == 2
    assert len(paginated_events["snapshot_data_by_window_id"][None]) == 2

    # No limit or offset provided
    paginated_events = decompress_chunked_snapshot_data(snapshot_data)
    assert paginated_events["has_next"] is False
    assert len(paginated_events["snapshot_data_by_window_id"]["1"]) == 2
    assert len(paginated_events["snapshot_data_by_window_id"][None]) == 2


def test_decompress_empty_list(chunked_and_compressed_snapshot_events):
    paginated_events = decompress_chunked_snapshot_data([])
    assert paginated_events["has_next"] is False
    assert paginated_events["snapshot_data_by_window_id"] == {}


def test_decompress_data_returning_only_activity_info(chunked_and_compressed_snapshot_events):
    snapshot_data = [
        SnapshotDataTaggedWithWindowId(
            snapshot_data=event["properties"]["$snapshot_data"], window_id=event["properties"].get("$window_id")
        )
        for event in chunked_and_compressed_snapshot_events
    ]
    paginated_events = decompress_chunked_snapshot_data(snapshot_data, return_only_activity_data=True)

    assert paginated_events["snapshot_data_by_window_id"] == {
        None: [
            {"timestamp": 1546300800000, "type": 4, "data": {}},
            {"timestamp": 1546300800000, "type": 2, "data": {}},
        ],
        "1": [
            {"timestamp": 1546300800000, "type": 3, "data": {}},
            {"timestamp": 1546300800000, "type": 3, "data": {"source": 2}},
        ],
    }


def test_get_active_segments_from_event_list():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    events = [
        create_activity_data(is_active=False, timestamp=(base_time + timedelta(seconds=0))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=10))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=10))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=40))),
        create_activity_data(is_active=False, timestamp=(base_time + timedelta(seconds=60))),
        create_activity_data(is_active=False, timestamp=(base_time + timedelta(seconds=100))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=110))),
        create_activity_data(is_active=False, timestamp=(base_time + timedelta(seconds=120))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=170))),
        create_activity_data(is_active=True, timestamp=(base_time + timedelta(seconds=180))),
        create_activity_data(is_active=False, timestamp=(base_time + timedelta(seconds=200))),
    ]
    active_segments = get_active_segments_from_event_list(events, window_id="1", activity_threshold_seconds=60)
    assert active_segments == [
        RecordingSegment(
            start_time=base_time + timedelta(seconds=10),
            end_time=base_time + timedelta(seconds=40),
            window_id="1",
            is_active=True,
        ),
        RecordingSegment(
            start_time=base_time + timedelta(seconds=110),
            end_time=base_time + timedelta(seconds=180),
            window_id="1",
            is_active=True,
        ),
    ]


def test_get_active_segments_from_single_event():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    events = [create_activity_data(is_active=True, timestamp=base_time)]
    active_segments = get_active_segments_from_event_list(events, window_id="1", activity_threshold_seconds=60)
    assert active_segments == [
        RecordingSegment(start_time=base_time, end_time=base_time, window_id="1", is_active=True)
    ]


def test_get_active_segments_from_no_active_events():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    events = [
        create_activity_data(is_active=False, timestamp=base_time),
        create_activity_data(is_active=False, timestamp=base_time + timedelta(seconds=110)),
    ]
    active_segments = get_active_segments_from_event_list(events, window_id="1", activity_threshold_seconds=60)
    assert active_segments == []


def test_generate_inactive_segments_for_range():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    generated_segments = generate_inactive_segments_for_range(
        base_time,
        base_time + timedelta(seconds=60),
        "2",
        {
            "1": {
                "start_time": base_time - timedelta(seconds=30),
                "end_time": base_time + timedelta(seconds=40),
                "window_id": "2",
                "is_active": False,
            },
            "2": {
                "start_time": base_time,
                "end_time": base_time + timedelta(seconds=20),
                "window_id": "2",
                "is_active": False,
            },
            "3": {
                "start_time": base_time + timedelta(seconds=35),
                "end_time": base_time + timedelta(seconds=80),
                "window_id": "2",
                "is_active": False,
            },
        },
    )
    millisecond = timedelta(milliseconds=1)
    assert generated_segments == [
        RecordingSegment(
            start_time=base_time + millisecond,
            end_time=base_time + timedelta(seconds=20),
            window_id="2",
            is_active=False,
        ),
        RecordingSegment(
            start_time=base_time + timedelta(seconds=20) + millisecond,
            end_time=base_time + timedelta(seconds=40),
            window_id="1",
            is_active=False,
        ),
        RecordingSegment(
            start_time=base_time + timedelta(seconds=40) + millisecond,
            end_time=base_time + timedelta(seconds=60) - millisecond,
            window_id="3",
            is_active=False,
        ),
    ]


def test_generate_inactive_segments_for_range_that_cannot_be_filled():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    generated_segments = generate_inactive_segments_for_range(
        base_time,
        base_time + timedelta(seconds=60),
        "2",
        {
            "2": {
                "start_time": base_time,
                "end_time": base_time + timedelta(seconds=20),
                "window_id": "2",
                "is_active": False,
            },
            "3": {
                "start_time": base_time + timedelta(seconds=35),
                "end_time": base_time + timedelta(seconds=80),
                "window_id": "2",
                "is_active": False,
            },
        },
    )
    millisecond = timedelta(milliseconds=1)
    assert generated_segments == [
        RecordingSegment(
            start_time=base_time + millisecond,
            end_time=base_time + timedelta(seconds=20),
            window_id="2",
            is_active=False,
        ),
        RecordingSegment(
            start_time=base_time + timedelta(seconds=35),
            end_time=base_time + timedelta(seconds=60) - millisecond,
            window_id="3",
            is_active=False,
        ),
    ]


def test_generate_inactive_segments_for_last_segment():
    base_time = datetime(2019, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    generated_segments = generate_inactive_segments_for_range(
        base_time,
        base_time + timedelta(seconds=60),
        "2",
        {
            "2": {
                "start_time": base_time,
                "end_time": base_time + timedelta(seconds=70),
                "window_id": "2",
                "is_active": False,
            }
        },
        is_last_segment=True,
    )
    millisecond = timedelta(milliseconds=1)
    assert generated_segments == [
        RecordingSegment(
            start_time=base_time + millisecond,
            end_time=base_time + timedelta(seconds=60),
            window_id="2",
            is_active=False,
        )
    ]


def test_get_events_summary_from_snapshot_data():
    timestamp = round(datetime.now().timestamp() * 1000)

    snapshot_events = [
        # ignore malformed events
        {"type": 2, "foo": "bar"},
        # ignore other props
        {"type": 2, "timestamp": timestamp, "foo": "bar"},
        # include standard properties
        {"type": 1, "timestamp": timestamp, "data": {"source": 3}},
        # Payload as list when we expect a dict
        {"type": 1, "timestamp": timestamp, "data": {"source": 3, "payload": [1, 2, 3]}},
        # include only allowed values
        {
            "type": 1,
            "timestamp": timestamp,
            "data": {
                # Large values we dont want
                "node": {},
                "text": "long-useless-text",
                # Standard core values we want
                "source": 3,
                "type": 1,
                # Values for initial render meta event
                "href": "https://app.posthog.com/events?foo=bar",
                "width": 2056,
                "height": 1120,
                # Special case for custom pageview events
                "tag": "$pageview",
                "plugin": "rrweb/console@1",
                "payload": {
                    "href": "https://app.posthog.com/events?eventFilter=",  # from pageview
                    "level": "log",  # from console plugin
                    # random
                    "dont-want": "this",
                    "or-this": {"foo": "bar"},
                },
            },
        },
        # payload has iso string timestamp instead of number and is out of order by timestamp sort
        # in https://posthog.sentry.io/issues/4089255349/?project=1899813&referrer=slack we saw a client
        # send this event, which caused the backend sorting to fail because we treat the rrweb timestamp
        # as if it is always a number
        {
            "type": 1,
            "timestamp": "1987-04-28T17:17:17.590Z",
            "data": {"source": 3},
        },
        # safely ignore string timestamps that aren't timestamps
        {
            "type": 1,
            "timestamp": "it was about a hundred years ago, that I remember this happening",
            "data": {"source": 3},
        },
    ]

    assert get_events_summary_from_snapshot_data(snapshot_events) == [
        {"data": {"source": 3}, "timestamp": 546628637590, "type": 1},
        {"timestamp": timestamp, "type": 2, "data": {}},
        {"timestamp": timestamp, "type": 1, "data": {"source": 3}},
        {"timestamp": timestamp, "type": 1, "data": {"source": 3}},
        {
            "timestamp": timestamp,
            "type": 1,
            "data": {
                "source": 3,
                "type": 1,
                "href": "https://app.posthog.com/events?foo=bar",
                "width": 2056,
                "height": 1120,
                "tag": "$pageview",
                "plugin": "rrweb/console@1",
                "payload": {
                    "href": "https://app.posthog.com/events?eventFilter=",
                    "level": "log",
                },
            },
        },
    ]


@pytest.fixture
def raw_snapshot_events():
    return [
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$window_id": "1",
                "$snapshot_data": {"type": 2, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$window_id": "1",
                "$snapshot_data": {"type": 3, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
    ]


@pytest.fixture
def chunked_and_compressed_snapshot_events():
    chunk_1_events = [
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$snapshot_data": {"type": 4, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$snapshot_data": {"type": 2, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
    ]
    chunk_2_events = [
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$window_id": "1",
                "$snapshot_data": {"type": 3, "timestamp": MILLISECOND_TIMESTAMP},
                "distinct_id": "abc123",
            },
        },
        {
            "event": "$snapshot",
            "properties": {
                "$session_id": "1234",
                "$window_id": "1",
                "$snapshot_data": {
                    "type": 3,
                    "timestamp": MILLISECOND_TIMESTAMP,
                    "data": {"source": 2},
                },
                "distinct_id": "abc123",
            },
        },
    ]
    return list(mock_capture_flow(chunk_1_events)) + list(mock_capture_flow(chunk_2_events))


def compress_decompress_and_extract(events, chunk_size):
    snapshot_data_list = [event["properties"]["$snapshot_data"] for event in mock_capture_flow(events, chunk_size)]
    window_id = "abc123"
    snapshot_list = []
    for snapshot_data in snapshot_data_list:
        snapshot_list.append(SnapshotDataTaggedWithWindowId(window_id=window_id, snapshot_data=snapshot_data))

    return decompress_chunked_snapshot_data(snapshot_list)["snapshot_data_by_window_id"][window_id]


# def test_get_events_summary_from_snapshot_data():
#     timestamp = round(datetime.now().timestamp() * 1000)
#     snapshot_events = [
#         {"type": 2, "foo": "bar", "timestamp": timestamp},
#         {"type": 1, "foo": "bar", "timestamp": timestamp},
#         {"type": 1, "foo": "bar", "timestamp": timestamp, "data": {"source": 3}},
#     ]

#     assert get_events_summary_from_snapshot_data(snapshot_events) == [
#         {"timestamp": timestamp, "type": 2, "data": {}},
#         {"timestamp": timestamp, "type": 1, "data": {}},
#         {"timestamp": timestamp, "type": 1, "data": {"source": 3}},
#     ]


def test_is_active_event():
    timestamp = round(datetime.now().timestamp() * 1000)
    assert is_active_event({"timestamp": timestamp, "type": 3, "data": {}}) is False
    assert is_active_event({"timestamp": timestamp, "type": 2, "data": {"source": 3}}) is False
    assert is_active_event({"timestamp": timestamp, "type": 3, "data": {"source": 3}}) is True

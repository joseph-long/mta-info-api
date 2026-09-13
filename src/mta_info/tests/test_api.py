import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mta_info.main import create_app
from mta_info.tests.test_departures import NOW_EPOCH, feed_message

FIXTURE_INDEX = {
    "fetched_at": "2026-01-01T00:00:00+00:00",
    "stations": {
        "101": {
            "id": "101",
            "name": "Test Station",
            "routes": ["A", "C"],
            "child_stops": [
                {"id": "101N", "name": "Test Station", "direction": "N"},
                {"id": "101S", "name": "Test Station", "direction": "S"},
            ],
        },
        "201": {
            "id": "201",
            "name": "Far Away Ave",
            "routes": ["A"],
            "child_stops": [
                {"id": "201N", "name": "Far Away Ave", "direction": "N"},
                {"id": "201S", "name": "Far Away Ave", "direction": "S"},
            ],
        },
    },
    "routes": {
        "A": {"id": "A", "short_name": "A", "color": "#0062CF", "text_color": "#FFFFFF"},
        "C": {"id": "C", "short_name": "C", "color": "#0062CF", "text_color": "#FFFFFF"},
    },
}


class FakeFeedCache:
    """Stands in for FeedCache: serves canned feed messages, never touches
    the network."""

    def __init__(self, messages):
        self._messages = messages

    async def messages_for(self, groups):
        return {group: self._messages.get(group) for group in groups}

    async def close(self):
        pass


@pytest.fixture()
def client(tmp_path, monkeypatch):
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    feed = feed_message(
        [
            ("A", [("101N", now_epoch + 5 * 60), ("201N", now_epoch + 25 * 60)]),
            ("A", [("101N", now_epoch + 12 * 60), ("999", now_epoch + 30 * 60)]),
            ("C", [("101S", now_epoch + 2 * 60), ("201S", now_epoch + 22 * 60)]),
        ]
    )
    gtfs_dir = tmp_path / "gtfs_static"
    gtfs_dir.mkdir()
    (gtfs_dir / "stations.json").write_text(json.dumps(FIXTURE_INDEX), encoding="utf-8")
    monkeypatch.setenv("MTA_STATE_DIR", str(tmp_path))

    app = create_app(feed_cache=FakeFeedCache({"gtfs-ace": feed}))
    with TestClient(app) as test_client:
        yield test_client


def enroll(client, device_id="board-1"):
    resp = client.post("/api/devices", json={"id": device_id})
    assert resp.status_code == 201


def configure(client, configurations, device_id="board-1"):
    return client.put(
        f"/api/devices/{device_id}/configurations",
        json={"configurations": configurations},
    )


def test_departures_requires_device_header(client):
    assert client.get("/api/departures").status_code == 401


def test_departures_rejects_unknown_device(client):
    resp = client.get("/api/departures", headers={"X-Device-ID": "nope"})
    assert resp.status_code == 404


def test_enrolled_device_with_blank_config_gets_empty_list(client):
    enroll(client)
    resp = client.get("/api/departures", headers={"X-Device-ID": "board-1"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_polling_updates_last_request_timestamp(client):
    enroll(client)
    assert client.get("/api/devices/board-1").json()["last_request_at"] is None
    client.get("/api/departures", headers={"X-Device-ID": "board-1"})
    assert client.get("/api/devices/board-1").json()["last_request_at"] is not None


def test_departures_payload_shape(client):
    enroll(client)
    resp = configure(
        client,
        [
            {
                "origin_station_id": "101",
                "service": "A",
                "direction": "uptown",
                "destination_station_id": "201",
                "destination_walk_minutes": 4,
            }
        ],
    )
    assert resp.status_code == 200

    resp = client.get("/api/departures", headers={"X-Device-ID": "board-1"})
    departures = resp.json()
    assert len(departures) == 2

    first, second = departures
    assert first["service"] == "A"
    assert first["terminus"] == "Far Away Ave"
    assert first["arrives_at"].endswith("Z")
    assert first["stops_at_destination"] is True
    reach = datetime.fromisoformat(first["reach_destination_at"].replace("Z", "+00:00"))
    arrive = datetime.fromisoformat(first["arrives_at"].replace("Z", "+00:00"))
    # 20 minutes Test Station -> Far Away Ave, plus 4 minutes of walking.
    assert (reach - arrive) == timedelta(minutes=24)

    # Second trip terminates early at unknown stop 999: flagged, no ETA.
    assert second["stops_at_destination"] is False
    assert "reach_destination_at" not in second


def test_direction_is_applied(client):
    enroll(client)
    # Both A trips in the fixture feed stop at the uptown platform (101N).
    configure(client, [{"origin_station_id": "101", "service": "A", "direction": "downtown"}])
    resp = client.get("/api/departures", headers={"X-Device-ID": "board-1"})
    assert resp.json() == []


def test_invalid_direction_rejected(client):
    enroll(client)
    resp = configure(client, [{"origin_station_id": "101", "service": "A", "direction": "up"}])
    assert resp.status_code == 422


def test_destination_keys_omitted_when_no_destination_configured(client):
    enroll(client)
    configure(client, [{"origin_station_id": "101", "service": "A", "direction": "uptown"}])
    [departure] = client.get(
        "/api/departures?max_departures=1", headers={"X-Device-ID": "board-1"}
    ).json()
    assert "stops_at_destination" not in departure
    assert "reach_destination_at" not in departure


def test_departures_unions_configurations_and_caps_at_max(client):
    enroll(client)
    configure(
        client,
        [
            {"origin_station_id": "101", "service": "A", "direction": "uptown"},
            {"origin_station_id": "101", "service": "C", "direction": "downtown"},
        ],
    )
    resp = client.get("/api/departures", headers={"X-Device-ID": "board-1"})
    departures = resp.json()
    # 3 departures total, default cap is 3: C(+2), A(+5), A(+12).
    assert [d["service"] for d in departures] == ["C", "A", "A"]

    resp = client.get(
        "/api/departures?max_departures=2", headers={"X-Device-ID": "board-1"}
    )
    assert [d["service"] for d in resp.json()] == ["C", "A"]


def test_duplicate_service_rejected_with_409(client):
    enroll(client)
    resp = configure(
        client,
        [
            {"origin_station_id": "101", "service": "A", "direction": "uptown"},
            {"origin_station_id": "201", "service": "a", "direction": "downtown"},
        ],
    )
    assert resp.status_code == 409
    assert "one configuration per service" in resp.json()["detail"]


def test_stations_endpoint_serves_loaded_index(client):
    data = client.get("/api/stations").json()
    assert data["fetched_at"] == "2026-01-01T00:00:00+00:00"
    assert [s["name"] for s in data["stations"]] == ["Far Away Ave", "Test Station"]


def test_get_device_includes_configurations(client):
    enroll(client)
    configure(client, [{"origin_station_id": "101", "service": "A", "direction": "uptown"}])
    data = client.get("/api/devices/board-1").json()
    assert data["id"] == "board-1"
    assert len(data["configurations"]) == 1
    assert data["configurations"][0]["service"] == "A"
    assert data["configurations"][0]["direction"] == "uptown"

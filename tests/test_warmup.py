"""GET /api/v1/devices/{id}/metrics/warmup-release 预热稳定放行的测试。"""

import itertools

from fastapi.testclient import TestClient
import pytest

from app.main import app


_suffix = {"v": 0}
_counter = itertools.count()


@pytest.fixture(autouse=True)
def _unique_device_suffix():
    _suffix["v"] = next(_counter)


def _dev(name="dut"):
    return f"{name}-{_suffix['v']}"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pkg(seq, measurements, key=None, sent_at=None, device=None):
    device = device or _dev()
    p = {
        "device_id": device,
        "seq": seq,
        "idempotency_key": key or f"{device}-k{seq}",
        "measurements": measurements,
    }
    if sent_at is not None:
        p["sent_at"] = sent_at
    return p


def m(value, unit="C", ts=None, metric="temperature"):
    d = {"metric": metric, "value": value}
    if unit is not None:
        d["unit"] = unit
    if ts is not None:
        d["ts"] = ts
    return d


FROM = "2026-09-23T08:00:00Z"
TO = "2026-09-23T09:00:00Z"


def release(client, **params):
    params.setdefault("metric", "temperature")
    params.setdefault("unit", "C")
    params.setdefault("from", FROM)
    params.setdefault("to", TO)
    params.setdefault("max_fluctuation", 0.5)
    params.setdefault("min_duration_seconds", 30)
    params.setdefault("min_readings", 3)
    params.setdefault("max_gap_seconds", 20)
    return client.get(
        f"/api/v1/devices/{_dev()}/metrics/warmup-release",
        params=params,
    )


# ---------------------------------------------------------------------------
# 基本放行
# ---------------------------------------------------------------------------

def test_basic_release_picks_earliest_completing_segment(client):
    # 08:00:00 起 4 条读数，间隔 10s，跨度 30s，波动 0.2
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
        m(20.15, ts="2026-09-23T08:00:30Z"),
    ]))

    resp = release(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["released"] is True
    assert body["not_released_reason"] is None
    seg = body["segment"]
    # 最早在第 3 条（08:00:20，跨度 20s 不足 30s）不放行；08:00:30 完成
    assert seg["start_time"] == "2026-09-23T08:00:00Z"
    assert seg["end_time"] == "2026-09-23T08:00:30Z"
    assert seg["duration_seconds"] == pytest.approx(30.0)
    assert seg["reading_count"] == 4
    assert seg["max_gap_seconds"] == pytest.approx(10.0)
    assert seg["actual_fluctuation"] == pytest.approx(0.2)
    assert [r["index"] for r in seg["readings"]] == [1, 2, 3, 4]
    assert [(r["seq"], r["position"]) for r in seg["readings"]] == [
        (1, 0), (1, 1), (1, 2), (1, 3)
    ]
    assert all(r["time_source"] == "ts" for r in seg["readings"])
    assert body["candidate_count"] == 4
    assert body["excluded_no_time_count"] == 0
    assert body["excluded_unit_mismatch_count"] == 0


def test_duration_too_short_not_released(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
    ]))
    body = release(client, min_duration_seconds=30).json()
    assert body["released"] is False
    assert body["segment"] is None
    assert "稳定段" in body["not_released_reason"]
    assert body["candidate_count"] == 3


def test_fluctuation_too_large_not_released(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.6, ts="2026-09-23T08:00:10Z"),
        m(20.1, ts="2026-09-23T08:00:20Z"),
        m(20.2, ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(client, max_fluctuation=0.5).json()
    # 整体波动 0.6；任何 3 条窗口要么跨度不足 30s（08:00:10 起 20s），
    # 末尾 3 条波动 0.5、跨度 20s 仍不足
    assert body["released"] is False
    assert body["segment"] is None


def test_boundary_values_pass_when_equal(client):
    # 波动恰好 0.5、跨度恰好 30s、间隔恰好 20s：边界全部合格
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.5, ts="2026-09-23T08:00:20Z"),
        m(20.2, ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(
        client,
        max_fluctuation=0.5,
        min_duration_seconds=30,
        min_readings=3,
        max_gap_seconds=20,
    ).json()
    assert body["released"] is True
    assert body["segment"]["actual_fluctuation"] == pytest.approx(0.5)


def test_gap_boundary_equal_passes_but_exceeding_resets_window(client):
    # 间隔恰好 20s 可以跨过
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:20Z"),
        m(20.2, ts="2026-09-23T08:00:40Z"),
    ]))
    body = release(client, max_gap_seconds=20, min_duration_seconds=30).json()
    assert body["released"] is True

    # 间隔 21s 超上限：前两条不能与第三条同段，单段读数不足 → 不放行
    client.post("/api/v1/packages", json=pkg(2, [
        m(20.0, ts="2026-09-23T08:10:00Z"),
        m(20.1, ts="2026-09-23T08:10:20Z"),
        m(20.2, ts="2026-09-23T08:10:41Z"),
    ], key=f"{_dev()}-k2"))
    body = release(client, max_gap_seconds=20, min_duration_seconds=30).json()
    seg = body["segment"]
    # 仅以第三条为起点无法成段；但第一条序列（pkg1）本身已合格 → 放行
    assert seg["end_time"] == "2026-09-23T08:00:40Z"


def test_gap_resets_and_later_window_releases(client):
    # 08:00 两条之后出现 60s 大间隔，断点之后才稳定成段
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:01:10Z"),
        m(20.3, ts="2026-09-23T08:01:20Z"),
        m(20.25, ts="2026-09-23T08:01:30Z"),
    ]))
    body = release(
        client, max_fluctuation=0.5, min_duration_seconds=20,
        min_readings=3, max_gap_seconds=20,
    ).json()
    assert body["released"] is True
    seg = body["segment"]
    # 完成时间最早的合格段是断点之后的 3 条（08:01:10~08:01:30）
    assert seg["start_time"] == "2026-09-23T08:01:10Z"
    assert seg["end_time"] == "2026-09-23T08:01:30Z"
    assert seg["reading_count"] == 3
    assert [r["index"] for r in seg["readings"]] == [3, 4, 5]
    assert seg["max_gap_seconds"] == pytest.approx(10.0)


def test_earliest_outlier_shrunk_off_picks_earliest_start_for_endpoint(client):
    # 首条是异常大值；到第 4 条时窗口须丢掉首条才满足波动，
    # 起点取满足约束的最早位置（第 2 条），而非更晚
    client.post("/api/v1/packages", json=pkg(1, [
        m(99.0, ts="2026-09-23T08:00:00Z"),
        m(10.0, ts="2026-09-23T08:00:10Z"),
        m(10.1, ts="2026-09-23T08:00:20Z"),
        m(10.2, ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(
        client, max_fluctuation=0.3, min_duration_seconds=20,
        min_readings=3, max_gap_seconds=60,
    ).json()
    assert body["released"] is True
    seg = body["segment"]
    assert seg["start_time"] == "2026-09-23T08:00:10Z"
    assert seg["end_time"] == "2026-09-23T08:00:30Z"
    assert seg["reading_count"] == 3
    assert seg["actual_fluctuation"] == pytest.approx(0.2)
    assert [r["index"] for r in seg["readings"]] == [2, 3, 4]


def test_min_readings_enforced(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(client, min_readings=3, min_duration_seconds=30).json()
    assert body["released"] is False
    assert body["candidate_count"] == 2


# ---------------------------------------------------------------------------
# 候选过滤与排序
# ---------------------------------------------------------------------------

def test_unit_filter_and_missing_unit_excluded(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, unit="C", ts="2026-09-23T08:00:00Z"),
        m(20.1, unit="F", ts="2026-09-23T08:00:10Z"),
        m(20.2, unit=None, ts="2026-09-23T08:00:20Z"),
        m(20.3, unit="C", ts="2026-09-23T08:00:30Z"),
        m(20.4, unit="c", ts="2026-09-23T08:00:40Z"),  # 大小写不同
    ]))
    body = release(
        client, unit="C", min_readings=2, min_duration_seconds=30,
        max_gap_seconds=60,
    ).json()
    assert body["candidate_count"] == 2
    assert body["excluded_unit_mismatch_count"] == 3
    # 两条 C 读数跨度恰好 30s、读数 2、波动 0.3 ≤ 0.5 → 放行
    assert body["released"] is True
    assert [r["index"] for r in body["segment"]["readings"]] == [1, 2]
    assert [r["unit"] for r in body["segment"]["readings"]] == ["C", "C"]


def test_no_time_readings_excluded(client):
    # 包无 sent_at：无 ts 的读数无法定位时间；有 ts 的仍参与
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts=None),
        m(20.2, ts="2026-09-23T08:00:10Z"),
        m(20.3, ts="2026-09-23T08:00:20Z"),
    ], sent_at=None))
    body = release(client, min_duration_seconds=20).json()
    assert body["candidate_count"] == 3
    assert body["excluded_no_time_count"] == 1
    assert body["released"] is True


def test_sent_at_fallback_and_ts_precedence(client):
    client.post("/api/v1/packages", json=pkg(1, [
        {"metric": "temperature", "value": 20.0, "unit": "C",
         "ts": "2026-09-23T08:00:05Z"},
        {"metric": "temperature", "value": 20.1, "unit": "C"},  # 回退 sent_at
        {"metric": "temperature", "value": 20.2, "unit": "C",
         "ts": "2026-09-23T08:00:15Z"},
    ], sent_at="2026-09-23T08:00:10Z"))
    body = release(
        client, min_duration_seconds=10, min_readings=3, max_gap_seconds=10
    ).json()
    assert body["released"] is True
    times = [(r["time"], r["time_source"]) for r in body["segment"]["readings"]]
    assert times == [
        ("2026-09-23T08:00:05Z", "ts"),
        ("2026-09-23T08:00:10Z", "sent_at"),
        ("2026-09-23T08:00:15Z", "ts"),
    ]


def test_half_open_time_range_and_ordering_by_time_seq_position(client):
    # 同时间读数跨包按 (seq, position) 排序；边界 from 含、to 不含
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.0, ts=FROM),  # 与 seq=2 的边界读数同时间：seq 小者在前
    ], key=f"{_dev()}-k1"))
    client.post("/api/v1/packages", json=pkg(2, [
        m(20.2, ts="2026-09-23T08:00:20Z"),
        m(20.0, ts=FROM),  # 恰好等于 from：含
        m(20.9, ts=TO),    # 恰好等于 to：不含
    ], key=f"{_dev()}-k2"))
    body = release(
        client, min_readings=4, min_duration_seconds=20, max_gap_seconds=20
    ).json()
    assert body["candidate_count"] == 4
    order = [(r["time"], r["seq"], r["position"]) for r in body["segment"]["readings"]]
    assert order == [
        ("2026-09-23T08:00:00Z", 1, 1),
        ("2026-09-23T08:00:00Z", 2, 1),
        ("2026-09-23T08:00:10Z", 1, 0),
        ("2026-09-23T08:00:20Z", 2, 0),
    ]
    assert body["segment"]["start_time"] == FROM
    assert body["segment"]["end_time"] == "2026-09-23T08:00:20Z"


def test_buffered_packages_do_not_participate(client):
    # seq 1 建立设备并提交；seq 3 因缺口 seq 2 留在缓冲区，不参与
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, metric="ph", ts="2026-09-23T08:00:00Z"),
    ]))
    client.post("/api/v1/packages", json=pkg(3, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
    ], key=f"{_dev()}-k3"))
    body = release(client).json()
    assert body["candidate_count"] == 0
    assert body["released"] is False
    assert "没有" in body["not_released_reason"]


def test_other_metrics_ignored(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, metric="temperature", ts="2026-09-23T08:00:00Z"),
        {"metric": "ph", "value": 7.0, "unit": "C",
         "ts": "2026-09-23T08:00:10Z"},
        m(20.1, metric="temperature", ts="2026-09-23T08:00:20Z"),
        m(20.2, metric="temperature", ts="2026-09-23T08:00:30Z"),
    ]))
    body = release(client).json()
    assert body["candidate_count"] == 3
    assert body["released"] is True


# ---------------------------------------------------------------------------
# 参数校验 / 只读
# ---------------------------------------------------------------------------

def test_unknown_device_404(client):
    resp = client.get(
        f"/api/v1/devices/{_dev('ghost')}/metrics/warmup-release",
        params={
            "metric": "temperature", "unit": "C",
            "from": FROM, "to": TO,
            "max_fluctuation": 0.5, "min_duration_seconds": 30,
            "min_readings": 3, "max_gap_seconds": 20,
        },
    )
    assert resp.status_code == 404


def test_bad_params_422(client):
    client.post("/api/v1/packages", json=pkg(1, [m(20.0)]))
    base = {
        "metric": "temperature", "unit": "C",
        "from": FROM, "to": TO,
        "max_fluctuation": 0.5, "min_duration_seconds": 30,
        "min_readings": 3, "max_gap_seconds": 20,
    }
    url = f"/api/v1/devices/{_dev()}/metrics/warmup-release"
    bad = dict(base, **{"from": TO, "to": FROM})
    assert client.get(url, params=bad).status_code == 422
    for name, value in (
        ("max_fluctuation", -1),
        ("min_duration_seconds", -1),
        ("max_gap_seconds", -1),
        ("min_readings", 0),
    ):
        assert client.get(url, params={**base, name: value}).status_code == 422
    assert client.get(url, params={**base, "unit": ""}).status_code == 422
    assert client.get(
        url, params={k: v for k, v in base.items() if k != "unit"}
    ).status_code == 422


def test_read_only_and_repeatable(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
    ]))
    first = release(client)
    committed = client.get(f"/api/v1/devices/{_dev()}/committed").json()
    second = release(client)
    assert first.json() == second.json()
    assert client.get(f"/api/v1/devices/{_dev()}/committed").json() == committed


def test_response_echoes_request_criteria(client):
    client.post("/api/v1/packages", json=pkg(1, [
        m(20.0, ts="2026-09-23T08:00:00Z"),
        m(20.1, ts="2026-09-23T08:00:10Z"),
        m(20.2, ts="2026-09-23T08:00:20Z"),
    ]))
    body = release(
        client, max_fluctuation=0.8, min_duration_seconds=15,
        min_readings=2, max_gap_seconds=25,
    ).json()
    assert body["max_fluctuation"] == pytest.approx(0.8)
    assert body["min_duration_seconds"] == pytest.approx(15.0)
    assert body["min_readings"] == 2
    assert body["max_gap_seconds"] == pytest.approx(25.0)
    assert body["from"] == FROM and body["to"] == TO
    assert body["unit"] == "C" and body["metric"] == "temperature"

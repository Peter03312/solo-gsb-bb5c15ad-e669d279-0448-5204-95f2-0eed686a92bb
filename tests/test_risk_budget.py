"""单车危险区风险预算复核测试。

POST /api/v1/trips/{trip_id}/risk-budget：复核单车一次作业 [since, until) 内的
危险区风险预算。区域与车次点集从同一已提交读视图读取；各区占用沿用既有轨迹插值、
区域生效时段与带洞边界规则；重叠区域同时占用只按最高权重计入，轨迹未覆盖或不在
指定区内的时间不计入；返回按时间排列的恒权重正时长区间（相接且权重相同者合并）、
各段及总计加权秒数、累计值首次达到预算的时刻（未达到为 null）；
无暴露返回零总计与空区间。
"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _upload(client, trip, points):
    r = client.post(
        f"/api/v1/trips/{trip}/points",
        json={"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]},
    )
    assert r.status_code == 200, r.text
    return r


def _ask(client, trip, **overrides):
    body = {
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "zone_weights": {"Z-A": 5},
        "budget": 100,
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/risk-budget", json=body)


# T1 沿 y=50 以 1m/s 横穿：A(t) = (t, 50)，t ∈ [0, 100]（10:00:00 起）
CROSS_1MS = [
    ("a", "2026-09-30T10:00:00Z", 0, 50),
    ("b", "2026-09-30T10:01:40Z", 100, 50),
]


# ---------------------------------------------------------------------
# 基本语义：最高权重计入、恒权重分段、加权秒数、预算时刻
# ---------------------------------------------------------------------

def test_overlap_counts_only_highest_weight(client):
    """Z-A[0,100]²(w5) 与 Z-B[50,150]²(w9) 重叠：重叠段只按 w9 计入，分两段。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-B", exterior=[[50, 0], [150, 0], [150, 100], [50, 100]])
    _upload(client, "T1", CROSS_1MS)

    r = _ask(client, "T1", zone_weights={"Z-A": 5, "Z-B": 9}, budget=400)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["since"] == "2026-09-30T10:00:00Z"
    assert body["until"] == "2026-09-30T10:05:00Z"
    assert body["budget"] == 400.0
    assert body["zone_weights"] == {"Z-A": 5.0, "Z-B": 9.0}
    assert body["track_version"] == 1      # 回填实际使用的轨迹版本
    assert body["point_count"] == 2
    assert "revision" not in body          # 未指定修订号：不出现
    assert body["intervals"] == [
        {"start": "2026-09-30T10:00:00Z", "end": "2026-09-30T10:00:50Z",
         "duration_sec": 50.0, "weight": 5.0, "weighted_sec": 250.0},
        {"start": "2026-09-30T10:00:50Z", "end": "2026-09-30T10:01:40Z",
         "duration_sec": 50.0, "weight": 9.0, "weighted_sec": 450.0},
    ]
    assert body["total_weighted_sec"] == 700.0
    # 首段累计 250，段内线性增长，再需 150/9 ≈ 16.666667 秒
    assert body["budget_reached_at"] == "2026-09-30T10:01:06.666667Z"


def test_weight_steps_down_when_higher_zone_ends(client):
    """高权重区先结束：超出部分按仍占用的较低权重继续计入。"""
    register_square(client, zone_id="Z-Hi", exterior=[[0, 0], [60, 0], [60, 100], [0, 100]])
    register_square(client, zone_id="Z-Lo")  # [0,100]²
    _upload(client, "T1", CROSS_1MS)

    body = _ask(client, "T1", zone_weights={"Z-Hi": 9, "Z-Lo": 5}, budget=600).json()
    assert [(i["start"], i["end"], i["weight"], i["weighted_sec"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:01:00Z", 9.0, 540.0),
        ("2026-09-30T10:01:00Z", "2026-09-30T10:01:40Z", 5.0, 200.0),
    ]
    assert body["total_weighted_sec"] == 740.0
    # 首段累计 540，再需 60/5 = 12 秒
    assert body["budget_reached_at"] == "2026-09-30T10:01:12Z"


def test_budget_reached_moment_boundaries(client):
    """预算恰为段界累计 / 落在段内 / 恰为总计 / 超过总计（空时刻）。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-B", exterior=[[50, 0], [150, 0], [150, 100], [50, 100]])
    _upload(client, "T1", CROSS_1MS)
    zw = {"Z-A": 5, "Z-B": 9}
    # 预算恰为前段累计：时刻为两段交界
    body = _ask(client, "T1", zone_weights=zw, budget=250).json()
    assert body["budget_reached_at"] == "2026-09-30T10:00:50Z"
    # 预算落在第一段内：100/5 = 20 秒
    body = _ask(client, "T1", zone_weights=zw, budget=100).json()
    assert body["budget_reached_at"] == "2026-09-30T10:00:20Z"
    # 预算恰为总计：时刻为末段终点
    body = _ask(client, "T1", zone_weights=zw, budget=700).json()
    assert body["budget_reached_at"] == "2026-09-30T10:01:40Z"
    # 超过总计：未达到，返回空时刻
    body = _ask(client, "T1", zone_weights=zw, budget=700.5).json()
    assert body["budget_reached_at"] is None
    assert body["total_weighted_sec"] == 700.0


def test_budget_equal_to_total_with_float_dust(client):
    """预算等于带浮点尾差的总计（0.7×3=2.0999…）：仍判定在段末达到。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:03Z", 100, 50),
    ])
    body = _ask(client, "T1", zone_weights={"Z-A": 0.7}, budget=2.1).json()
    assert body["total_weighted_sec"] == 2.1
    assert body["budget_reached_at"] == "2026-09-30T10:00:03Z"


def test_adjacent_same_weight_intervals_merged(client):
    """两区占用相接且权重相同：合并为一段；中间隔着未覆盖时间则不合并。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-D", exterior=[[100, 0], [200, 0], [200, 100], [100, 100]])
    register_square(client, zone_id="Z-F", exterior=[[150, 0], [250, 0], [250, 100], [150, 100]])
    _upload(client, "T1", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:03:20Z", 200, 50),  # 1m/s，t ∈ [0, 200]
    ])
    # Z-A[0,100]² 与 Z-D[100,200]² 在 x=100 处相接、权重同为 5：合并为一段
    body = _ask(client, "T1", zone_weights={"Z-A": 5, "Z-D": 5}, budget=1000).json()
    assert [(i["start"], i["end"], i["weight"], i["weighted_sec"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:03:20Z", 5.0, 1000.0),
    ]
    assert body["total_weighted_sec"] == 1000.0
    assert body["budget_reached_at"] == "2026-09-30T10:03:20Z"
    # Z-A 与 Z-F[150,250]² 之间隔着 50 秒未覆盖时间：不合并
    body = _ask(client, "T1", zone_weights={"Z-A": 5, "Z-F": 5}, budget=1000).json()
    assert [(i["start"], i["end"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:01:40Z"),
        ("2026-09-30T10:02:30Z", "2026-09-30T10:03:20Z"),
    ]
    assert body["total_weighted_sec"] == 750.0


def test_zones_not_in_map_not_counted(client):
    """只计入映射中指定的区：几何相同但未列入的区不影响结果。"""
    register_square(client, zone_id="Z-A")
    register_square(client, zone_id="Z-Other")  # 与 Z-A 几何相同，但未列入映射
    _upload(client, "T1", CROSS_1MS)
    body = _ask(client, "T1", zone_weights={"Z-A": 5}, budget=600).json()
    assert body["total_weighted_sec"] == 500.0
    assert body["budget_reached_at"] is None


# ---------------------------------------------------------------------
# 既有规则沿用：带洞边界、生效窗口、查询范围、轨迹覆盖
# ---------------------------------------------------------------------

def test_hole_rules_inherited(client):
    """带洞边界规则沿用：洞及其边界不计入，占用被洞切成两段。"""
    hole = [[[40, 40], [60, 40], [60, 60], [40, 60]]]
    register_square(client, zone_id="Z-H", holes=hole)
    _upload(client, "T1", CROSS_1MS)
    body = _ask(client, "T1", zone_weights={"Z-H": 2}, budget=100).json()
    assert [(i["start"], i["end"], i["duration_sec"], i["weighted_sec"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:40Z", 40.0, 80.0),
        ("2026-09-30T10:01:00Z", "2026-09-30T10:01:40Z", 40.0, 80.0),
    ]
    assert body["total_weighted_sec"] == 160.0
    # 首段累计 80，再需 20/2 = 10 秒
    assert body["budget_reached_at"] == "2026-09-30T10:01:10Z"


def test_zone_window_clips_exposure(client):
    """区域 UTC 半开生效窗口沿用：只统计窗口内的占用。"""
    register_square(
        client, zone_id="Z-W",
        windows=[{"start": "2026-09-30T10:00:30Z", "end": "2026-09-30T10:01:10Z"}],
    )
    _upload(client, "T1", CROSS_1MS)
    body = _ask(client, "T1", zone_weights={"Z-W": 3}, budget=120).json()
    assert [(i["start"], i["end"], i["weighted_sec"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:30Z", "2026-09-30T10:01:10Z", 120.0),
    ]
    assert body["budget_reached_at"] == "2026-09-30T10:01:10Z"


def test_query_range_and_coverage_clip_exposure(client):
    """查询范围半开裁剪；轨迹未覆盖的时间不计入。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    body = _ask(
        client, "T1",
        since="2026-09-30T10:00:20Z", until="2026-09-30T10:01:30Z",
        zone_weights={"Z-A": 5}, budget=175,
    ).json()
    assert [(i["start"], i["end"], i["weighted_sec"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:01:30Z", 350.0),
    ]
    assert body["budget_reached_at"] == "2026-09-30T10:00:55Z"
    # 查询范围宽于轨迹覆盖：覆盖范围外的时间不计入
    body = _ask(
        client, "T1",
        since="2026-09-30T09:00:00Z", until="2026-09-30T11:00:00Z",
        zone_weights={"Z-A": 5}, budget=500,
    ).json()
    assert body["total_weighted_sec"] == 500.0
    assert body["budget_reached_at"] == "2026-09-30T10:01:40Z"


def test_no_exposure_returns_zero_total_and_empty(client):
    """无暴露：零总计、空区间、空时刻；单点车次无轨迹覆盖同样合法。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", [  # 全程在区外行驶
        ("a", "2026-09-30T10:00:00Z", 0, 150),
        ("b", "2026-09-30T10:01:40Z", 100, 150),
    ])
    body = _ask(client, "T1", zone_weights={"Z-A": 5}, budget=100).json()
    assert body["intervals"] == []
    assert body["total_weighted_sec"] == 0.0
    assert body["budget_reached_at"] is None
    # 单点车次：无轨迹覆盖，请求本身合法
    _upload(client, "T2", [("solo", "2026-09-30T10:00:30Z", 50, 50)])
    body = _ask(client, "T2", zone_weights={"Z-A": 5}, budget=100).json()
    assert body["point_count"] == 1
    assert body["intervals"] == []
    assert body["total_weighted_sec"] == 0.0
    assert body["budget_reached_at"] is None


def test_all_points_revoked_trip_known_but_no_exposure(client):
    """全部点被撤销的车次仍然存在：返回零总计与空区间而非 404。"""
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    r = client.post("/api/v1/trips/T1/corrections", json={
        "base_version": 1,
        "reason": "两点均为误录",
        "items": [
            {"action": "revoke", "point_id": "a"},
            {"action": "revoke", "point_id": "b"},
        ],
    })
    assert r.status_code == 200
    body = _ask(client, "T1", zone_weights={"Z-A": 5}, budget=100).json()
    assert body["track_version"] == 2
    assert body["point_count"] == 0
    assert body["intervals"] == []
    assert body["total_weighted_sec"] == 0.0


# ---------------------------------------------------------------------
# 区域修订号与轨迹版本复现
# ---------------------------------------------------------------------

def test_pinned_revision_reproduces_zone_config(client):
    """区域修订号：按截至该号的各区最后配置核算，响应回填 revision。"""
    register_square(client, zone_id="Z-R")  # rev1: [0,100]²
    register_square(client, zone_id="Z-R",
                    exterior=[[0, 0], [30, 0], [30, 100], [0, 100]])  # rev2: 只剩 [0,30]
    _upload(client, "T1", CROSS_1MS)
    # 最新配置（rev2）：只计入 [0,30] 段
    body = _ask(client, "T1", zone_weights={"Z-R": 4}, budget=1000).json()
    assert "revision" not in body
    assert body["total_weighted_sec"] == 120.0
    assert body["budget_reached_at"] is None
    # 按 rev1 复现：全程计入
    body = _ask(client, "T1", zone_weights={"Z-R": 4}, budget=400, revision=1).json()
    assert body["revision"] == 1
    assert body["total_weighted_sec"] == 400.0
    assert body["budget_reached_at"] == "2026-09-30T10:01:40Z"


def test_pinned_track_version_reproduces_points(client):
    """轨迹版本：按该版本保留的点集核算；未指定取查询时最新已提交状态。"""
    register_square(client, zone_id="Z-V")
    _upload(client, "T1", CROSS_1MS)
    # 更正：b 改写到区外，当前版本（v2）不再穿越
    r = client.post("/api/v1/trips/T1/corrections", json={
        "base_version": 1,
        "reason": "b 点坐标录错",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T10:01:40Z", "x": -100, "y": 150}],
    })
    assert r.status_code == 200
    # 最新已提交状态：无暴露，回填当前版本 2
    body = _ask(client, "T1", zone_weights={"Z-V": 5}, budget=100).json()
    assert body["track_version"] == 2
    assert body["intervals"] == []
    assert body["total_weighted_sec"] == 0.0
    # 按版本 1 点集复现：当时的 100 秒
    body = _ask(client, "T1", zone_weights={"Z-V": 5}, budget=250, track_version=1).json()
    assert body["track_version"] == 1
    assert body["point_count"] == 2
    assert body["total_weighted_sec"] == 500.0
    assert body["budget_reached_at"] == "2026-09-30T10:00:50Z"


def test_revision_and_track_version_combine(client):
    """区域修订号与轨迹版本可同时指定，按组合复现。"""
    register_square(client, zone_id="Z-C")  # rev1: [0,100]²
    _upload(client, "T1", CROSS_1MS)        # v1
    r = client.post("/api/v1/trips/T1/corrections", json={
        "base_version": 1,
        "reason": "b 点坐标录错",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T10:01:40Z", "x": -100, "y": 150}],
    })                                      # v2：不再穿越
    assert r.status_code == 200
    register_square(client, zone_id="Z-C",
                    exterior=[[0, 0], [30, 0], [30, 100], [0, 100]])  # rev2
    body = _ask(client, "T1", zone_weights={"Z-C": 5}, budget=1000,
                revision=1, track_version=1).json()
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["total_weighted_sec"] == 500.0
    body = _ask(client, "T1", zone_weights={"Z-C": 5}, budget=1000,
                revision=2, track_version=1).json()
    assert body["revision"] == 2
    assert body["track_version"] == 1
    assert body["total_weighted_sec"] == 150.0


# ---------------------------------------------------------------------
# 错误：车次 / 区域 / 版本不存在
# ---------------------------------------------------------------------

def test_unknown_trip_rejected(client):
    register_square(client, zone_id="Z-A")
    r = _ask(client, "ghost", zone_weights={"Z-A": 5}, budget=100)
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]
    # 指定版本：车次不存在（无任何已保留的轨迹版本）
    r = _ask(client, "ghost", zone_weights={"Z-A": 5}, budget=100, track_version=1)
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_unknown_zone_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    r = _ask(client, "T1", zone_weights={"ghost-zone": 5}, budget=100)
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]
    # 映射中任一区未知即整拒
    r = _ask(client, "T1", zone_weights={"Z-A": 5, "ghost-zone": 1}, budget=100)
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_revision_out_of_range_and_zone_unregistered(client):
    register_square(client, zone_id="Z-1")  # rev1
    _upload(client, "T1", CROSS_1MS)
    r = _ask(client, "T1", zone_weights={"Z-1": 5}, budget=100, revision=999)
    assert r.status_code == 404
    assert "超出当前范围" in r.json()["detail"]
    register_square(client, zone_id="Z-2")  # rev2 才登记
    r = _ask(client, "T1", zone_weights={"Z-2": 5}, budget=100, revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_track_version_out_of_range(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    # 早于基线（新车次基线为 1）
    r = _ask(client, "T1", zone_weights={"Z-A": 5}, budget=100, track_version=0)
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]
    # 晚于当前
    r = _ask(client, "T1", zone_weights={"Z-A": 5}, budget=100, track_version=9)
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]


# ---------------------------------------------------------------------
# 错误：非法时段 / 权重 / 预算 / 请求形状
# ---------------------------------------------------------------------

def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    # since >= until
    r = _ask(client, "T1", since="2026-09-30T10:02:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    # 不带时区
    r = _ask(client, "T1", since="2026-09-30T10:00:00", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]
    # 非时间串
    r = _ask(client, "T1", since="not-a-time")
    assert r.status_code == 400


def test_invalid_zone_weights_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    # 空映射
    r = _ask(client, "T1", zone_weights={})
    assert r.status_code in (400, 422)
    # 非映射
    r = _ask(client, "T1", zone_weights=[["Z-A", 5]])
    assert r.status_code in (400, 422)
    # 非正 / 非有限 / 非数值权重
    for bad in (0, -1, "NaN", "Infinity", "-Infinity", "abc"):
        r = _ask(client, "T1", zone_weights={"Z-A": bad})
        assert r.status_code in (400, 422), bad


def test_invalid_budget_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    for bad in (0, -5, "NaN", "Infinity", "-Infinity", "abc"):
        r = _ask(client, "T1", budget=bad)
        assert r.status_code in (400, 422), bad


def test_invalid_request_shape_rejected(client):
    register_square(client, zone_id="Z-A")
    _upload(client, "T1", CROSS_1MS)
    # 缺字段
    r = client.post("/api/v1/trips/T1/risk-budget",
                    json={"since": "2026-09-30T10:00:00Z"})
    assert r.status_code == 422
    # 多余字段
    r = _ask(client, "T1", bogus=1)
    assert r.status_code == 422
    # 版本号非法
    r = _ask(client, "T1", track_version=-1)
    assert r.status_code in (400, 422)
    r = _ask(client, "T1", track_version="abc")
    assert r.status_code in (400, 422)
    r = _ask(client, "T1", revision=0)
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------
# 同一已提交读视图：并发更新不拼出不存在的组合
# ---------------------------------------------------------------------

def test_concurrent_writes_never_break_snapshot(client):
    """writer 持续更新区域与轨迹，reader 反复查询：
    结果必须结构完整；固定版本组合的复现结果确定一致。"""
    register_square(client, zone_id="Z-L")
    _upload(client, "T1", CROSS_1MS)

    stop = threading.Event()
    failures = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            size = 100 + (i % 3) * 50
            client.post("/api/v1/zones", json={
                "zone_id": "Z-L",
                "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                "holes": [],
                "windows": DAY_WINDOWS,
            })
            client.post("/api/v1/trips/T1/points", json={"points": [
                {"point_id": f"extra-{i}", "ts": f"2026-09-30T12:{i % 60:02d}:00Z",
                 "x": 10 + i, "y": 10},
            ]})
            i += 1

    def reader() -> None:
        while not stop.is_set():
            r = _ask(client, "T1", zone_weights={"Z-L": 5}, budget=250)
            if r.status_code != 200:
                failures.append(("status", r.status_code, r.text))
                return
            body = r.json()
            # 回填的版本与点数必须来自同一快照：版本单调推进、点数与版本一致
            if body["track_version"] < 1 or body["point_count"] < 2:
                failures.append(("version-mix", body))
                return
            total = 0.0
            last_end = None
            for seg in body["intervals"]:
                if seg["duration_sec"] <= 0 or seg["weight"] != 5.0:
                    failures.append(("segment", seg))
                    return
                if last_end is not None and seg["start"] < last_end:
                    failures.append(("order", body["intervals"]))
                    return
                last_end = seg["end"]
                total += seg["weighted_sec"]
            if abs(total - body["total_weighted_sec"]) > 1e-6:
                failures.append(("total", body))
                return
            # 固定组合复现：rev1 区域 × 版本 1 点集，两次结果必须一致
            pinned = dict(zone_weights={"Z-L": 5}, budget=250, revision=1, track_version=1)
            r1 = _ask(client, "T1", **pinned)
            r2 = _ask(client, "T1", **pinned)
            if r1.status_code != 200 or r1.json() != r2.json():
                failures.append(("pinned-nondeterministic", r1.status_code, r1.text))
                return
            body = r1.json()
            if (body["total_weighted_sec"] != 500.0
                    or body["budget_reached_at"] != "2026-09-30T10:00:50Z"):
                failures.append(("pinned-wrong", body))
                return

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not failures


def test_existing_endpoints_unaffected(client):
    """风险预算复核上线后，既有穿越与多车占用查询语义不变。"""
    register_square(client, zone_id="Z-K")
    _upload(client, "T1", CROSS_1MS)
    _upload(client, "T2", CROSS_1MS)
    r = client.get("/api/v1/trips/T1/crossings?zone_id=Z-K")
    assert r.status_code == 200
    assert r.json()["zones"][0]["total_duration_sec"] == 100.0
    assert "track_version" not in r.json()

    r = client.post("/api/v1/zones/Z-K/co-occupancy", json={
        "trip_ids": ["T1", "T2"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "threshold": 2,
    })
    assert r.status_code == 200
    assert len(r.json()["intervals"]) == 1

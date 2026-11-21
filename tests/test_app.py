"""端到端测试：计划导入、实发批次、等待区间、冲突报告、持久化、规模。"""
import pytest

DAY = "2026-11-20"


def sample_plan():
    return {
        "trips": [
            {"trip_id": "A", "planned": 100},
            {"trip_id": "B", "planned": 200},
            {"trip_id": "C", "planned": 140},
            {"trip_id": "D", "planned": 300},
        ],
        "connections": [
            {"pred": "A", "succ": "B", "min_interval": 30},
            {"pred": "A", "succ": "C", "min_interval": 10},
            {"pred": "B", "succ": "D", "min_interval": 20},
        ],
    }


def import_plan(client, day=DAY, plan=None):
    r = client.post(f"/days/{day}/plan", json=plan or sample_plan())
    assert r.status_code == 201, r.text
    return r


def post_actuals(client, updates, day=DAY):
    return client.post(f"/days/{day}/actuals", json={"updates": updates})


def get_trip(client, trip_id, day=DAY):
    return client.get(f"/days/{day}/trips/{trip_id}")


# ---------------------------------------------------------------- 计划导入

def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_import_plan_and_query_without_actual(client):
    import_plan(client)
    r = get_trip(client, "A")
    assert r.status_code == 200
    body = r.json()
    assert body["planned"] == 100
    assert body["actual"] is None
    assert body["wait"] is None


def test_duplicate_plan_rejected(client):
    import_plan(client)
    r = client.post(f"/days/{DAY}/plan", json=sample_plan())
    assert r.status_code == 409


def test_delete_day_and_reimport(client):
    import_plan(client)
    assert client.delete(f"/days/{DAY}").status_code == 200
    assert get_trip(client, "A").status_code == 404
    import_plan(client)
    assert get_trip(client, "A").status_code == 200


@pytest.mark.parametrize(
    "plan, fragment",
    [
        # 环
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "Y", "planned": 2}],
                "connections": [
                    {"pred": "X", "succ": "Y", "min_interval": 1},
                    {"pred": "Y", "succ": "X", "min_interval": 1},
                ],
            },
            "环",
        ),
        # 超过 4 条前序
        (
            {
                "trips": [{"trip_id": f"P{i}", "planned": 1} for i in range(5)]
                + [{"trip_id": "T", "planned": 2}],
                "connections": [
                    {"pred": f"P{i}", "succ": "T", "min_interval": 1} for i in range(5)
                ],
            },
            "4",
        ),
        # 自接续
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}],
                "connections": [{"pred": "X", "succ": "X", "min_interval": 1}],
            },
            "自身",
        ),
        # 引用不存在的班次
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}],
                "connections": [{"pred": "X", "succ": "ZZ", "min_interval": 1}],
            },
            "不存在",
        ),
        # 重复接续
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "Y", "planned": 2}],
                "connections": [
                    {"pred": "X", "succ": "Y", "min_interval": 1},
                    {"pred": "X", "succ": "Y", "min_interval": 2},
                ],
            },
            "重复",
        ),
        # 重复班次
        (
            {
                "trips": [{"trip_id": "X", "planned": 1}, {"trip_id": "X", "planned": 2}],
                "connections": [],
            },
            "重复",
        ),
        # 计划分钟越界
        (
            {"trips": [{"trip_id": "X", "planned": 2880}], "connections": []},
            "超出",
        ),
    ],
)
def test_plan_validation_errors(client, plan, fragment):
    r = client.post(f"/days/{DAY}/plan", json=plan)
    assert r.status_code == 400, r.text
    assert fragment in r.text


def test_infeasible_plan_rejected(client):
    # 接续链总长超出运营日：0 + 2000 + 2000 = 4000 > 2879
    plan = {
        "trips": [
            {"trip_id": "X", "planned": 0},
            {"trip_id": "Y", "planned": 0},
            {"trip_id": "Z", "planned": 0},
        ],
        "connections": [
            {"pred": "X", "succ": "Y", "min_interval": 2000},
            {"pred": "Y", "succ": "Z", "min_interval": 2000},
        ],
    }
    r = client.post(f"/days/{DAY}/plan", json=plan)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["total_conflicts"] >= 1
    assert detail["conflicts"][0]["kind"] == "no_feasible_completion"
    # 未写入任何数据
    assert client.get(f"/days/{DAY}").status_code == 404


# ---------------------------------------------------------------- 实发批次

def test_record_and_known_wait(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 200}])
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 1
    r = post_actuals(client, [{"trip_id": "B", "actual": 240}])
    assert r.status_code == 200, r.text
    body = get_trip(client, "B").json()
    assert body["actual"] == 240
    # 已知等待：200 + 30 - 200 = 30；A 已知，区间塌缩为 [30, 30]
    assert body["wait"] == {"known_wait": 30, "min_wait": 30, "max_wait": 30}


def test_actual_earlier_than_plan_rejected(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 90}])
    assert r.status_code == 400
    assert "早于计划" in r.text
    assert get_trip(client, "A").json()["actual"] is None


def test_actual_beyond_horizon_rejected(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "A", "actual": 2880}])
    assert r.status_code == 400


def test_unknown_trip_in_batch(client):
    import_plan(client)
    r = post_actuals(client, [{"trip_id": "ZZ", "actual": 100}])
    assert r.status_code == 400


def test_duplicate_trip_in_batch(client):
    import_plan(client)
    r = post_actuals(
        client, [{"trip_id": "A", "actual": 100}, {"trip_id": "A", "actual": 101}]
    )
    assert r.status_code == 400


def test_revoke_actual(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 150}])
    assert get_trip(client, "A").json()["actual"] == 150
    r = post_actuals(client, [{"trip_id": "A", "actual": None}])
    assert r.status_code == 200
    body = get_trip(client, "A").json()
    assert body["actual"] is None
    assert body["wait"] is None


def test_direct_conflict_reports_chain_and_rolls_back(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    r = post_actuals(client, [{"trip_id": "B", "actual": 220}])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["total_conflicts"] == 1
    conflict = detail["conflicts"][0]
    assert conflict["kind"] == "actual_before_required"
    assert conflict["trip_id"] == "B"
    assert conflict["actual"] == 220
    assert conflict["required_earliest"] == 230
    assert [n["trip_id"] for n in conflict["chain"]] == ["A", "B"]
    assert conflict["edges"] == [{"pred": "A", "succ": "B", "min_interval": 30}]
    # 回滚：原实发保留，B 仍未补录
    assert get_trip(client, "A").json()["actual"] == 200
    assert get_trip(client, "B").json()["actual"] is None


def test_indirect_chain_conflict(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 400}])
    # D 实发 310，但 A->B->D 要求 400+30+20 = 450
    r = post_actuals(client, [{"trip_id": "D", "actual": 310}])
    assert r.status_code == 409, r.text
    conflict = r.json()["detail"]["conflicts"][0]
    assert conflict["required_earliest"] == 450
    assert [n["trip_id"] for n in conflict["chain"]] == ["A", "B", "D"]
    assert conflict["chain"][1]["actual"] is None  # 中间班 B 尚无实发
    assert conflict["edges"] == [
        {"pred": "A", "succ": "B", "min_interval": 30},
        {"pred": "B", "succ": "D", "min_interval": 20},
    ]
    assert get_trip(client, "D").json()["actual"] is None


def test_conflict_beyond_horizon(client):
    import_plan(client)
    # A 实发 2879：B 需 >= 2909、C 需 >= 2889、D 需 >= 2929，均超出运营日
    r = post_actuals(client, [{"trip_id": "A", "actual": 2879}])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    kinds = {c["kind"] for c in detail["conflicts"]}
    assert kinds == {"no_feasible_completion"}
    assert {c["trip_id"] for c in detail["conflicts"]} == {"B", "C", "D"}
    assert get_trip(client, "A").json()["actual"] is None


def test_batch_atomicity(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 200}])
    # B=240 本身合法，但同批 C=145 与 A=200 矛盾（需 >= 210）→ 整批回滚
    r = post_actuals(client, [{"trip_id": "B", "actual": 240}, {"trip_id": "C", "actual": 145}])
    assert r.status_code == 409
    assert get_trip(client, "B").json()["actual"] is None
    assert get_trip(client, "C").json()["actual"] is None
    assert get_trip(client, "A").json()["actual"] == 200


# ---------------------------------------------------------------- 等待区间

def test_wait_interval_and_shared_predecessor(client):
    """共用前序 A 未知：更正/撤销 B 改变 A 的可行时刻，C 的区间随之更新。"""
    import_plan(client)
    post_actuals(
        client,
        [
            {"trip_id": "B", "actual": 230},
            {"trip_id": "C", "actual": 400},
            {"trip_id": "D", "actual": 500},
        ],
    )

    # A 未知：EF(A)=100（计划），LF(A)=min(230-30, 400-10)=200
    wait = get_trip(client, "C").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 70}  # 200+10-140
    wait = get_trip(client, "B").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 30}  # 200+30-200
    # D 的前序 B 已知(230)：230+20-300 < 0 → 等待恒为 0，区间塌缩
    wait = get_trip(client, "D").json()["wait"]
    assert wait == {"known_wait": 0, "min_wait": 0, "max_wait": 0}

    # 更正 B：LF(A) 变为 230 → C 最多解释 100 分钟
    post_actuals(client, [{"trip_id": "B", "actual": 260}])
    assert get_trip(client, "C").json()["wait"]["max_wait"] == 100
    assert get_trip(client, "B").json()["wait"]["max_wait"] == 60  # 230+30-200

    # 撤销 B：LF(A) 只受 C 约束（390）→ C 最多解释 260 分钟
    post_actuals(client, [{"trip_id": "B", "actual": None}])
    assert get_trip(client, "C").json()["wait"]["max_wait"] == 260

    # 补录 A=180：区间塌缩到已知等待 50
    post_actuals(client, [{"trip_id": "A", "actual": 180}])
    wait = get_trip(client, "C").json()["wait"]
    assert wait == {"known_wait": 50, "min_wait": 50, "max_wait": 50}


def test_list_actuals_with_known_wait(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "B", "actual": 230}, {"trip_id": "C", "actual": 400}])
    post_actuals(client, [{"trip_id": "A", "actual": 180}])
    r = client.get(f"/days/{DAY}/actuals")
    assert r.status_code == 200
    items = {i["trip_id"]: i for i in r.json()["items"]}
    assert items["A"]["known_wait"] == 0
    assert items["B"]["known_wait"] == 10  # 180+30-200
    assert items["C"]["known_wait"] == 50  # 180+10-140


def test_day_overview_endpoints(client):
    import_plan(client)
    post_actuals(client, [{"trip_id": "A", "actual": 150}])
    r = client.get("/days")
    assert r.json()["days"] == [{"day": DAY, "trips": 4, "with_actual": 1}]
    r = client.get(f"/days/{DAY}")
    assert r.json() == {"day": DAY, "trips": 4, "with_actual": 1, "connections": 3}
    assert client.get("/days/2099-01-01").status_code == 404
    assert client.get(f"/days/{DAY}/trips/ZZ").status_code == 404


# ---------------------------------------------------------------- 持久化

def test_persistence_across_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "persist.db"))
    with TestClient(app) as c1:
        import_plan(c1)
        post_actuals(c1, [{"trip_id": "A", "actual": 125}])

    # 模拟重启：全新客户端（应用本身无内存状态，全部落在 SQLite）
    with TestClient(app) as c2:
        body = c2.get(f"/days/{DAY}/trips/A").json()
        assert body["actual"] == 125
        assert body["wait"] == {"known_wait": 0, "min_wait": 0, "max_wait": 0}


# ---------------------------------------------------------------- 深度与规模

def test_deep_chain(client):
    """2000 班接续长链：深链传播不爆栈，区间计算正确。"""
    n = 2000
    trips = [{"trip_id": f"T{i}", "planned": 100} for i in range(n)]
    conns = [
        {"pred": f"T{i}", "succ": f"T{i+1}", "min_interval": 1} for i in range(n - 1)
    ]
    r = client.post(f"/days/{DAY}/plan", json={"trips": trips, "connections": conns})
    assert r.status_code == 201, r.text
    post_actuals(client, [{"trip_id": "T0", "actual": 150}])
    post_actuals(client, [{"trip_id": f"T{n-1}", "actual": 2200}])
    wait = get_trip(client, f"T{n-1}").json()["wait"]
    # EF(T1998)=150+1998=2148 → min=2148+1-100；LF(T1998)=2200-1=2199 → max=2199+1-100
    assert wait == {"known_wait": 0, "min_wait": 2049, "max_wait": 2100}


def test_scale_50k(client):
    """单日 5 万班次：导入、批录、单班查询（局部子图，无需遍历全日）。"""
    chains, length = 2500, 20
    trips, conns = [], []
    for c in range(chains):
        for i in range(length):
            trips.append({"trip_id": f"C{c}T{i}", "planned": 100})
            if i:
                conns.append(
                    {"pred": f"C{c}T{i-1}", "succ": f"C{c}T{i}", "min_interval": 1}
                )
    assert len(trips) == 50000
    r = client.post(f"/days/{DAY}/plan", json={"trips": trips, "connections": conns})
    assert r.status_code == 201, r.text

    assert post_actuals(client, [{"trip_id": "C0T0", "actual": 150}]).status_code == 200
    assert post_actuals(client, [{"trip_id": "C0T19", "actual": 200}]).status_code == 200
    wait = get_trip(client, "C0T19").json()["wait"]
    # EF(C0T18)=150+18=168 → min=168+1-100；LF(C0T18)=200-1=199 → max=199+1-100
    assert wait == {"known_wait": 0, "min_wait": 69, "max_wait": 100}

    # 与已录实发无关的另一条链：查询只走局部子图
    assert post_actuals(client, [{"trip_id": "C2499T10", "actual": 150}]).status_code == 200
    wait = get_trip(client, "C2499T10").json()["wait"]
    # EF(C2499T9)=109 → min=10；LF=150-1=149 → max=149+1-100=50
    assert wait == {"known_wait": 0, "min_wait": 10, "max_wait": 50}

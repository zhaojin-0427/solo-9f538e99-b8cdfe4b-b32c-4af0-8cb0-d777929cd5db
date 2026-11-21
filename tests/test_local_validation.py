"""局部批次校验测试：访问量上界、汇合剪枝、前序变化重核、回滚、旧库引导、
409 明细与全日校验等价（含随机对照）。
"""
import random

import pytest

from app.db import connect, init_db, read_txn, write_txn
from app.logic import (
    BatchConflict,
    DayGraph,
    _LocalView,
    _incremental_propagate,
    apply_actuals,
    check_feasibility,
    import_plan,
    incremental_check_feasible,
    wait_interval,
)

DAY = "2026-11-22"


class U:
    """实发更新请求（ActualUpdate 形状）。"""

    def __init__(self, trip_id, actual):
        self.trip_id = trip_id
        self.actual = actual


class T:
    """计划班次（TripIn 形状）。"""

    def __init__(self, trip_id, planned):
        self.trip_id = trip_id
        self.planned = planned


class _Conn:
    def __init__(self, pred, succ, min_interval):
        self.pred = pred
        self.succ = succ
        self.min_interval = min_interval


def make_db(tmp_path, name="local.db"):
    import os

    os.environ["DB_PATH"] = str(tmp_path / name)
    init_db()
    return connect()


def import_direct(conn, trips, connections, horizon=2880, day=DAY):
    import_plan(
        conn,
        day,
        [T(t, p) for t, p in trips],
        [_Conn(p, s, m) for (p, s, m) in connections],
        horizon,
    )


def commit_actuals(conn, updates, horizon=2880, day=DAY):
    """已提交状态：直接落库并全日校验（与局部路径无关的对照基准）。"""
    with write_txn(conn):
        for tid, val in updates.items():
            conn.execute(
                "UPDATE trips SET actual = ? WHERE day = ? AND trip_id = ?",
                (val, day, tid),
            )
        total, _ = check_feasibility(conn, day, horizon)
        assert total == 0


def view_access(conn, overrides, horizon=2880, day=DAY):
    """返回 (局部判定是否有冲突, 视图的班次读取数, 接续读取数)。"""
    view = _LocalView(conn, day, overrides)
    conflict = _incremental_propagate(view, horizon)
    return conflict, view.trip_reads, view.edge_reads


# ---------------------------------------------------------------- 汇合点剪枝

def merge_plan(conn, tail_length, horizon=2880):
    """短支线 S -> M（汇合点）-> L1 -> ... -> Ln 长链，外加独立长链 C*。"""
    trips = [("S", 100), ("M", 200)]
    conns = [("S", "M", 10)]
    for i in range(tail_length):
        nxt = f"L{i}"
        prev = "M" if i == 0 else f"L{i-1}"
        trips.append((nxt, 100))
        conns.append((prev, nxt, 1))
    # 与本批完全无关的另一条长链：局部校验不应读取
    for i in range(tail_length):
        cid = f"C{i}"
        trips.append((cid, 100))
        if i:
            conns.append((f"C{i-1}", cid, 1))
    import_direct(conn, trips, conns, horizon)


@pytest.mark.parametrize("tail", [10, 100, 1000])
def test_local_access_bounded_when_merge_ef_unchanged(tmp_path, tail):
    """只更正汇合点前的短支线、汇合班 EF 未变：读取量不随后续长链增长。"""
    conn = make_db(tmp_path, f"bounded-{tail}.db")
    merge_plan(conn, tail)
    commit_actuals(conn, {"S": 150})
    # 更正 S（150 -> 155）：M 的 EF = max(200, 165) = 200，未变 → 长链不读
    conflict, trip_reads, edge_reads = view_access(conn, {"S": 155})
    assert conflict is False
    assert trip_reads <= 4  # S、M 及两条前序回溯所需，不含 L*/C*
    assert edge_reads <= 4
    # 再次加长/缩短都不改变访问量
    conn.close()


def test_local_access_constant_across_chain_lengths(tmp_path):
    counts = {}
    for tail in (5, 50, 500):
        conn = make_db(tmp_path, f"const-{tail}.db")
        merge_plan(conn, tail)
        commit_actuals(conn, {"S": 150})
        _c, tr, er = view_access(conn, {"S": 155})
        counts[tail] = (tr, er)
        conn.close()
    assert counts[5] == counts[50] == counts[500]


def test_local_propagates_when_merge_ef_changes(tmp_path):
    """汇合班 EF 因本批改变时，汇合后长链必须被读取并核对。"""
    tail = 200
    conn = make_db(tmp_path, "propagate.db")
    merge_plan(conn, tail)
    commit_actuals(conn, {"S": 150})
    # S 改为 250：M 的 EF = max(200, 260) = 260，变化 → 全链传播
    conflict, trip_reads, edge_reads = view_access(conn, {"S": 250})
    assert conflict is False
    # M 与 L0..L(tail-1) 都被读取（外加支线回溯）
    assert trip_reads >= tail + 1
    assert edge_reads >= tail
    conn.close()


def test_no_full_day_preload(tmp_path):
    """局部校验绝不发出按运营日全表扫描的查询（逐班/逐边取数）。"""
    conn = make_db(tmp_path, "no-preload.db")
    merge_plan(conn, 300)
    commit_actuals(conn, {"S": 150})
    statements = []
    orig = conn.execute

    class _SpyConn:
        def execute(self, sql, params=()):
            statements.append(sql)
            return orig(sql, params)

        def __getattr__(self, name):
            return getattr(conn, name)

    spy_conn = _SpyConn()
    assert incremental_check_feasible(spy_conn, DAY, {"S": 155}, 2880) is False
    # 所有 trips 查询都带单个 trip_id 定位；所有 connections 查询都按单班
    # succ/pred 定位——不存在按 day 全表扫描的预读
    assert statements
    for sql in statements:
        compact = " ".join(sql.split())
        if "FROM trips" in compact:
            assert "trip_id = ?" in compact
        if "FROM connections" in compact:
            assert "succ = ?" in compact or "pred = ?" in compact
    conn.close()


# ---------------------------------------------------------------- 前序变化触发重核

def test_successor_rechecked_when_predecessor_requirement_changes(tmp_path):
    """某班实发没改，但其前序接续要求因本批改变，必须重新核对（级联冲突）。"""
    conn = make_db(tmp_path, "recheck.db")
    import_direct(
        conn,
        [("A", 100), ("B", 200), ("C", 300)],
        [("A", "B", 30), ("B", "C", 20)],
    )
    # 已提交合法状态：A=150，B=230，C=300
    commit_actuals(conn, {"A": 150, "B": 230, "C": 300})
    # 只改 A=260：B 的实发 230 不动，但所需最早时刻变为 290 → 类型 1 冲突
    assert incremental_check_feasible(conn, DAY, {"A": 260}, 2880) is True
    with pytest.raises(BatchConflict) as exc:
        apply_actuals(conn, DAY, [U("A", 260)], 2880)
    detail = exc.value
    assert detail.total >= 1
    kinds = {c["kind"] for c in detail.conflicts}
    assert "actual_before_required" in kinds
    # B 在冲突明细中（它没被本批改，但被重新核对）
    assert any(c["trip_id"] == "B" for c in detail.conflicts)
    # 整批回滚：仍是已提交值
    with read_txn(conn):
        assert conn.execute(
            "SELECT actual FROM trips WHERE day=? AND trip_id='A'", (DAY,)
        ).fetchone()["actual"] == 150
    conn.close()


def test_revocation_relaxes_successor_and_is_local(tmp_path):
    """撤销使下游 EF 下降（向下修正），堆式传播同样正确且局部。"""
    conn = make_db(tmp_path, "revoke.db")
    import_direct(
        conn,
        [("S", 100), ("M", 200)] + [(f"L{i}", 100) for i in range(100)],
        [("S", "M", 10)] + [
            ("M" if i == 0 else f"L{i-1}", f"L{i}", 1) for i in range(100)
        ],
    )
    commit_actuals(conn, {"S": 250})
    # 撤销 S：M 的 EF 从 260 降回 200（变化）→ 全链下降传播，但仍合法
    assert incremental_check_feasible(conn, DAY, {"S": None}, 2880) is False
    n = apply_actuals(conn, DAY, [U("S", None)], 2880)
    assert n == 1
    with read_txn(conn):
        assert conn.execute(
            "SELECT actual FROM trips WHERE day=? AND trip_id='S'", (DAY,)
        ).fetchone()["actual"] is None
    conn.close()


# ---------------------------------------------------------------- 409 明细与全日校验等价（随机对照）

def _random_dag(rng, n_nodes, max_pred=3):
    ids = [f"T{i:04d}" for i in range(n_nodes)]
    edges = []
    for j in range(1, n_nodes):
        k = rng.randint(0, min(max_pred, j))
        preds = rng.sample(range(j), k)
        for p in preds:
            edges.append((ids[p], ids[j], rng.randint(0, 12)))
    trips = [(t, rng.randint(0, 400)) for t in ids]
    return trips, edges


def _state_feasible(conn, horizon):
    total, _ = check_feasibility(conn, DAY, horizon)
    return total == 0


def test_fuzz_local_vs_full_equivalence(tmp_path):
    rng = random.Random(20261122)
    conn = make_db(tmp_path, "fuzz.db")
    horizon = 2880
    for round_no in range(6):
        trips, edges = _random_dag(rng, 60)
        day = f"2026-12-{round_no + 1:02d}"
        import_direct(conn, trips, edges, horizon, day=day)
        ids = [t for t, _ in trips]
        for step in range(40):
            chosen = rng.sample(ids, rng.randint(1, 5))
            overrides = {}
            for tid in chosen:
                if rng.random() < 0.2:
                    overrides[tid] = None
                else:
                    overrides[tid] = rng.randint(0, 2879)
            # 400 类（越界/早于计划）在逐条校验阶段拒绝，不进入可行性对照
            planned_map = dict(trips)
            if any(v is not None and v < planned_map[t] for t, v in overrides.items()):
                continue
            local = incremental_check_feasible(conn, day, overrides, horizon)
            total_full, conflicts_full = check_feasibility(
                conn, day, horizon, overrides
            )
            full = total_full > 0
            assert local == full, (day, step, overrides, local, total_full)
            if full:
                # API 路径回退全日校验，冲突明细一致
                with pytest.raises(BatchConflict) as exc:
                    apply_actuals(
                        conn, day, [U(t, v) for t, v in overrides.items()], horizon
                    )
                assert exc.value.total == total_full
                assert exc.value.conflicts == conflicts_full
                # 失败后已提交状态未被污染，仍可行
                assert _state_feasible(conn, horizon)
            else:
                before = {
                    r["trip_id"]: r["actual"]
                    for r in conn.execute(
                        "SELECT trip_id, actual FROM trips WHERE day=?", (day,)
                    )
                }
                apply_actuals(
                    conn, day, [U(t, v) for t, v in overrides.items()], horizon
                )
                after = {
                    r["trip_id"]: r["actual"]
                    for r in conn.execute(
                        "SELECT trip_id, actual FROM trips WHERE day=?", (day,)
                    )
                }
                for tid, val in overrides.items():
                    assert after[tid] == val
                for tid, val in before.items():
                    if tid not in overrides:
                        assert after[tid] == val
                assert _state_feasible(conn, horizon)
    conn.close()


def test_conflict_payload_identical_to_full_check(tmp_path):
    """README 示例型冲突：局部发现后回退全日，409 总数/顺序/明细不变。"""
    conn = make_db(tmp_path, "payload.db")
    import_direct(
        conn,
        [("A", 100), ("B", 200), ("C", 140), ("D", 300)],
        [("A", "B", 30), ("A", "C", 10), ("B", "D", 20)],
    )
    commit_actuals(conn, {"A": 200})
    total, conflicts = check_feasibility(conn, DAY, 2880, {"B": 220})
    assert total == 1
    with pytest.raises(BatchConflict) as exc:
        apply_actuals(conn, DAY, [U("B", 220)], 2880)
    assert exc.value.total == total
    assert exc.value.conflicts == conflicts
    conn.close()


def test_horizon_overflow_conflict_local(tmp_path):
    """未知班被本批已知实发逼出运营日：局部传播即能发现。"""
    conn = make_db(tmp_path, "overflow.db")
    import_direct(conn, [("A", 0), ("B", 0)], [("A", "B", 10)])
    assert incremental_check_feasible(conn, DAY, {"A": 2875}, 2880) is True
    with pytest.raises(BatchConflict):
        apply_actuals(conn, DAY, [U("A", 2875)], 2880)
    conn.close()


# ---------------------------------------------------------------- 旧库引导与 horizon 变更

def test_legacy_db_bootstrap_without_reimport(tmp_path, monkeypatch):
    """旧库没有 day_state 表/标记：首次批录自动做一次全日引导，之后走局部路径。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE days (day TEXT PRIMARY KEY);
        CREATE TABLE trips (
            day TEXT NOT NULL, trip_id TEXT NOT NULL, planned INTEGER NOT NULL,
            topo INTEGER NOT NULL, actual INTEGER,
            PRIMARY KEY (day, trip_id));
        CREATE TABLE connections (
            day TEXT NOT NULL, pred TEXT NOT NULL, succ TEXT NOT NULL,
            min_interval INTEGER NOT NULL, PRIMARY KEY (day, pred, succ));
        CREATE INDEX idx_connections_succ ON connections(day, succ);
        CREATE INDEX idx_trips_day_actual ON trips(day, actual);
        """
    )
    conn.execute("INSERT INTO days(day) VALUES (?)", (DAY,))
    conn.execute(
        "INSERT INTO trips(day, trip_id, planned, topo, actual) VALUES (?,?,?,?,?)",
        (DAY, "A", 100, 0, None),
    )
    conn.execute(
        "INSERT INTO trips(day, trip_id, planned, topo, actual) VALUES (?,?,?,?,?)",
        (DAY, "B", 200, 1, None),
    )
    conn.execute(
        "INSERT INTO connections(day, pred, succ, min_interval) VALUES (?,?,?,?)",
        (DAY, "A", "B", 30),
    )
    # 旧库无 day_state 表：正常批录应自动建表并引导
    assert apply_actuals(conn, DAY, [U("A", 250)], 2880) == 1
    row = conn.execute(
        "SELECT feasible, horizon FROM day_state WHERE day=?", (DAY,)
    ).fetchone()
    assert row["feasible"] == 1 and row["horizon"] == 2880
    # 第二次起即为局部路径：B 需 >= 250+30=280，B=200 冲突且回滚
    with pytest.raises(BatchConflict):
        apply_actuals(conn, DAY, [U("B", 200)], 2880)
    assert conn.execute(
        "SELECT actual FROM trips WHERE day=? AND trip_id='B'", (DAY,)
    ).fetchone()["actual"] is None
    # 合法批次成功
    assert apply_actuals(conn, DAY, [U("B", 280)], 2880) == 1
    conn.close()


def test_horizon_change_triggers_full_recheck(tmp_path):
    """缩小 horizon 后，已提交状态可能变为不可行：必须回退全日校验而不是盲信标记。"""
    conn = make_db(tmp_path, "horizon.db")
    import_direct(
        conn,
        [("A", 0), ("B", 1000), ("C", 1000), ("X", 100)],
        [("A", "B", 100), ("B", "C", 100)],
        horizon=2880,
    )
    commit_actuals(conn, {"A": 2000, "X": 120})
    # horizon 从 2880 缩到 1500：已提交的 A=2000 使 B 需 2100、C 需 2200，均越界。
    # 批次只更正无关班次 X（120 -> 130，自身合法），也必须由全日引导校验拦下
    with pytest.raises(BatchConflict) as exc:
        apply_actuals(conn, DAY, [U("X", 130)], 1500)
    assert exc.value.total >= 1
    # 失败不写任何更改
    assert conn.execute(
        "SELECT actual FROM trips WHERE day=? AND trip_id='X'", (DAY,)
    ).fetchone()["actual"] == 120
    # 恢复原 horizon 后同批合法，标记 horizon 一并刷新
    assert apply_actuals(conn, DAY, [U("X", 130)], 2880) == 1
    row = conn.execute("SELECT horizon FROM day_state WHERE day=?", (DAY,)).fetchone()
    assert row["horizon"] == 2880
    conn.close()


# ---------------------------------------------------------------- 失败后的后续操作

def test_failed_batch_does_not_poison_later_operations(tmp_path):
    conn = make_db(tmp_path, "poison.db")
    import_direct(
        conn,
        [("A", 100), ("B", 200)],
        [("A", "B", 30)],
    )
    apply_actuals(conn, DAY, [U("A", 250)], 2880)
    # 连续多个非法批次：B 需 >= 280 但计划为 200，以下实发全部触发冲突并回滚
    for bad in (200, 250, 270):
        with pytest.raises(BatchConflict):
            apply_actuals(conn, DAY, [U("B", bad)], 2880)
    # 查询始终基于已提交数据
    with read_txn(conn):
        g = DayGraph(conn, DAY)
        assert g.trip("A")["actual"] == 250
        assert g.trip("B")["actual"] is None
        assert wait_interval(g, "A", 2880) == {
            "known_wait": 0,
            "min_wait": 0,
            "max_wait": 0,
        }
    # 合法批次随后正常提交
    assert apply_actuals(conn, DAY, [U("B", 300)], 2880) == 1
    with read_txn(conn):
        g = DayGraph(conn, DAY)
        assert g.trip("B")["actual"] == 300
        assert wait_interval(g, "B", 2880)["known_wait"] == 80  # 250+30-200
    conn.close()


def test_empty_batch_is_noop(tmp_path):
    conn = make_db(tmp_path, "empty.db")
    import_direct(conn, [("A", 100)], [])
    assert apply_actuals(conn, DAY, [], 2880) == 0
    conn.close()


# ---------------------------------------------------------------- 规模

def test_50k_local_batch_access(tmp_path):
    """5 万班运营日：长链上的局部批次读取量与全日规模无关，且接口可用。"""
    conn = make_db(tmp_path, "scale50k.db")
    chains, length = 2500, 20
    trips, conns, ids = [], [], []
    for c in range(chains):
        for i in range(length):
            tid = f"C{c}T{i}"
            ids.append(tid)
            trips.append((tid, 100))
            if i:
                conns.append((f"C{c}T{i-1}", tid, 1))
    import_direct(conn, trips, conns)
    commit_actuals(conn, {"C0T0": 150})
    # 更正链首但保持 C0T1 的 EF 不变（计划 100 仍是主导）：后续不传播
    conflict, tr, er = view_access(conn, {"C0T0": 105})
    assert conflict is False
    # 只触及该链前两个班及其接续清单，与全日 5 万班规模无关
    assert tr < 30 and er < 60
    # 正常 API 路径也成功
    assert apply_actuals(conn, DAY, [U("C0T0", 105)], 2880) == 1
    conn.close()


# ---------------------------------------------------------------- HTTP 端到端

def _merge_payload(tail):
    trips = [
        {"trip_id": "S", "planned": 100},
        {"trip_id": "M", "planned": 200},
    ]
    conns = [{"pred": "S", "succ": "M", "min_interval": 10}]
    for i in range(tail):
        nxt = f"L{i}"
        prev = "M" if i == 0 else f"L{i-1}"
        trips.append({"trip_id": nxt, "planned": 100})
        conns.append({"pred": prev, "succ": nxt, "min_interval": 1})
    return {"trips": trips, "connections": conns}


def test_api_merge_short_branch_local_and_unchanged_409(client):
    """HTTP 路径：短支线更正局部通过；冲突时 409 明细与既有格式完全一致。"""
    tail = 200
    assert client.post(f"/days/{DAY}/plan", json=_merge_payload(tail)).status_code == 201
    # 补录 S=150
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 150}]})
    assert r.status_code == 200 and r.json()["applied"] == 1
    # 只更正汇合点前的 S（M 的 EF 仍=200 未变）：整批通过
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 155}]})
    assert r.status_code == 200
    # 让 S=250：M 需 >=260，给 M=200 触发冲突，409 格式保持原样
    client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 250}]})
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "M", "actual": 200}]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "批次与已知实发矛盾，未写入任何更改"
    conflict = detail["conflicts"][0]
    assert detail["total_conflicts"] == 1
    assert conflict["kind"] == "actual_before_required"
    assert conflict["trip_id"] == "M"
    assert conflict["required_earliest"] == 260
    assert [n["trip_id"] for n in conflict["chain"]] == ["S", "M"]
    assert conflict["edges"] == [{"pred": "S", "succ": "M", "min_interval": 10}]
    # 整批回滚：M 仍无实发，S 保持 250
    assert client.get(f"/days/{DAY}/trips/M").json()["actual"] is None
    assert client.get(f"/days/{DAY}/trips/S").json()["actual"] == 250
    # 随后的合法批录与查询照常
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "M", "actual": 260}]})
    assert r.status_code == 200
    assert client.get(f"/days/{DAY}/trips/M").json()["actual"] == 260


def test_api_revoke_via_null_and_batch_mix(client):
    """HTTP 路径：同批混合补录/更正/撤销（actual=null），冲突整体回滚。"""
    client.post(f"/days/{DAY}/plan", json=_merge_payload(5))
    client.post(
        f"/days/{DAY}/actuals",
        json={"updates": [{"trip_id": "S", "actual": 150}, {"trip_id": "L0", "actual": 300}]},
    )
    # 撤销 L0 + 更正 S，整批合法
    r = client.post(
        f"/days/{DAY}/actuals",
        json={"updates": [{"trip_id": "L0", "actual": None}, {"trip_id": "S", "actual": 160}]},
    )
    assert r.status_code == 200 and r.json()["applied"] == 2
    assert client.get(f"/days/{DAY}/trips/L0").json()["actual"] is None
    assert client.get(f"/days/{DAY}/trips/S").json()["actual"] == 160
    # 同批中一条矛盾（S=250 后 M 需 260，却给 M=200）→ 撤销 S 也一并回滚
    r = client.post(
        f"/days/{DAY}/actuals",
        json={
            "updates": [
                {"trip_id": "S", "actual": 250},
                {"trip_id": "M", "actual": 200},
            ]
        },
    )
    assert r.status_code == 409
    assert client.get(f"/days/{DAY}/trips/S").json()["actual"] == 160
    assert client.get(f"/days/{DAY}/trips/M").json()["actual"] is None


def test_api_query_after_failed_batch_reflects_committed(client):
    """批次失败后，查询与列表均以已提交计划和实发为准。"""
    client.post(f"/days/{DAY}/plan", json=_merge_payload(5))
    client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 150}]})
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "M", "actual": 200}]})
    # S=150 要求 M>=160；M=200 合法，先提交
    assert r.status_code == 200
    # 再来一个必然冲突的批次（S 改 250 后 M=200 不满足）
    bad = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 250}]})
    assert bad.status_code == 409
    # 单班查询与列表都反映已提交值
    assert client.get(f"/days/{DAY}/trips/S").json()["actual"] == 150
    items = {i["trip_id"]: i for i in client.get(f"/days/{DAY}/actuals").json()["items"]}
    assert items["S"]["actual"] == 150 and items["M"]["actual"] == 200
    overview = client.get(f"/days/{DAY}").json()
    assert overview["with_actual"] == 2


def test_api_delete_and_reimport_uses_current_validation(client):
    """删除当天再导入后，按当前计划与实发校验（旧标记随删除级联清除）。"""
    client.post(f"/days/{DAY}/plan", json=_merge_payload(5))
    client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 150}]})
    assert client.delete(f"/days/{DAY}").status_code == 200
    # 重新导入（计划微调），历史实发与标记都不应残留
    payload = _merge_payload(5)
    payload["trips"][0]["planned"] = 120
    assert client.post(f"/days/{DAY}/plan", json=payload).status_code == 201
    assert client.get(f"/days/{DAY}/trips/S").json()["actual"] is None
    r = client.post(f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 130}]})
    assert r.status_code == 200


def test_api_persistence_restart_local_path(tmp_path, monkeypatch):
    """重启后（含 day_state 持久化）局部批次路径继续生效，无需重导。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "restart.db"))
    with TestClient(app) as c1:
        c1.post(f"/days/{DAY}/plan", json=_merge_payload(10))
        assert c1.post(
            f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 150}]}
        ).status_code == 200
        assert c1.post(
            f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "M", "actual": 200}]}
        ).status_code == 200
        # S=250 会使已录实发的 M（200）所需时刻变为 260 → 冲突回滚
        assert c1.post(
            f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 250}]}
        ).status_code == 409
    # 模拟重启：全新客户端，状态全部来自 SQLite（含 day_state 标记）
    with TestClient(app) as c2:
        assert c2.get(f"/days/{DAY}/trips/S").json()["actual"] == 150
        assert c2.get(f"/days/{DAY}/trips/M").json()["actual"] == 200
        # 重启后冲突照样拦截、合法批次照样提交
        assert c2.post(
            f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 250}]}
        ).status_code == 409
        assert c2.post(
            f"/days/{DAY}/actuals", json={"updates": [{"trip_id": "S", "actual": 160}]}
        ).status_code == 200
        assert c2.get(f"/days/{DAY}/trips/S").json()["actual"] == 160


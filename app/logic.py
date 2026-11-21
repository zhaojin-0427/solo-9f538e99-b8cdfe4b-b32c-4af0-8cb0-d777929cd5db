"""核心业务逻辑。

语义定义
--------
- 已知实发等待 known_wait：各前序实发 + 各自最短间隔，最晚时刻超出本班计划的
  分钟数；无前序或未超出算 0。
- 等待区间 [min_wait, max_wait]：在所有满足"计划、接续、已知实发"的补全中，
  本班接续等待能达到的最小/最大值。
  - 某前序 u 的最早可行时刻 EF(u)：沿祖先传播 max(计划, 前序EF+间隔)，已知实发固定；
  - 某前序 u 的最晚可行时刻 LF(u)：沿子孙传播 min(实发, 后继LF-间隔)，
    无约束时取运营日末分钟 horizon-1。
  等待对每个前序实发单调不减，且"全部取最早"/"全部取最晚"各自都是合法补全，
  因此区间端点分别由 EF、LF 给出。
- 单班查询只走该班前序的祖先/子孙子图（DayGraph 按需取数），不遍历全日接续。
- 可行性校验（每次计划导入与实发批次后，在写事务内执行）：
  全日按拓扑序传播 EF、逆拓扑序传播 LF：
    1) 有实发班次：实发 < 所需最早时刻  → 矛盾（冲突时刻 + 接续链）；
    2) 无实发班次：EF > LF（已知实发把它逼到运营日之外）→ 矛盾。
  校验不过整体回滚，保证库中数据始终存在至少一个合法补全，
  因此等待区间恒有 min_wait <= max_wait。
"""
from __future__ import annotations

from collections import deque

from .db import write_txn

MAX_PREDECESSORS = 4
MAX_CONFLICTS_REPORTED = 100


class PlanError(Exception):
    """计划导入校验失败（不写入任何数据）。"""

    def __init__(self, message: str, details: dict | None = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.status = status


class BatchError(Exception):
    """批次本身非法（不写入任何数据）。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class BatchConflict(Exception):
    """批次与已知实发合看有矛盾（整体回滚）。"""

    def __init__(self, conflicts: list[dict], total: int):
        super().__init__("conflicting actuals")
        self.conflicts = conflicts
        self.total = total


# ---------------------------------------------------------------- 计划导入

def topo_order(trip_ids, edges) -> dict:
    """Kahn 拓扑排序；有环时抛 PlanError 并给出环上/环下游节点。"""
    indeg = {t: 0 for t in trip_ids}
    succ_map = {t: [] for t in trip_ids}
    for pred, succ, _m in edges:
        indeg[succ] += 1
        succ_map[pred].append(succ)
    queue = deque(sorted(t for t in trip_ids if indeg[t] == 0))
    order = {}
    while queue:
        node = queue.popleft()
        order[node] = len(order)
        for succ in succ_map[node]:
            indeg[succ] -= 1
            if indeg[succ] == 0:
                queue.append(succ)
    if len(order) != len(indeg):
        remaining = sorted(t for t, d in indeg.items() if d > 0)
        raise PlanError("接续关系存在环", {"cycle_nodes": remaining})
    return order


def import_plan(conn, day: str, trips, connections, horizon_minutes: int) -> dict:
    if conn.execute("SELECT 1 FROM days WHERE day = ?", (day,)).fetchone():
        raise PlanError("该运营日已有计划，需先删除再导入", {"day": day}, status=409)

    ids: set[str] = set()
    for t in trips:
        if t.trip_id in ids:
            raise PlanError("计划内 trip_id 重复", {"trip_id": t.trip_id})
        ids.add(t.trip_id)
        if t.planned >= horizon_minutes:
            raise PlanError(
                "计划发车分钟超出运营日范围",
                {"trip_id": t.trip_id, "planned": t.planned, "horizon": horizon_minutes},
            )

    edges: list[tuple[str, str, int]] = []
    edge_set: set[tuple[str, str]] = set()
    pred_count: dict[str, int] = {}
    for c in connections:
        if c.pred == c.succ:
            raise PlanError("不允许班次接续自身", {"trip_id": c.pred})
        if c.pred not in ids or c.succ not in ids:
            raise PlanError("接续引用了不存在的班次", {"pred": c.pred, "succ": c.succ})
        if c.min_interval >= horizon_minutes:
            raise PlanError(
                "最短接续间隔超出运营日范围",
                {"pred": c.pred, "succ": c.succ, "min_interval": c.min_interval},
            )
        if (c.pred, c.succ) in edge_set:
            raise PlanError("接续关系重复", {"pred": c.pred, "succ": c.succ})
        edge_set.add((c.pred, c.succ))
        pred_count[c.succ] = pred_count.get(c.succ, 0) + 1
        if pred_count[c.succ] > MAX_PREDECESSORS:
            raise PlanError(
                "每班至多 4 条前序接续",
                {"trip_id": c.succ, "max_predecessors": MAX_PREDECESSORS},
            )
        edges.append((c.pred, c.succ, c.min_interval))

    order = topo_order(ids, edges)

    with write_txn(conn):
        conn.execute("INSERT INTO days(day) VALUES (?)", (day,))
        conn.executemany(
            "INSERT INTO trips(day, trip_id, planned, topo) VALUES (?,?,?,?)",
            [(day, t.trip_id, t.planned, order[t.trip_id]) for t in trips],
        )
        conn.executemany(
            "INSERT INTO connections(day, pred, succ, min_interval) VALUES (?,?,?,?)",
            [(day, p, s, m) for (p, s, m) in edges],
        )
        # 计划本身必须在运营日时长内可行（尚无实发时只需满足 EF <= LF）
        total, conflicts = check_feasibility(conn, day, horizon_minutes)
        if total:
            raise PlanError(
                "计划在运营日时长内不可行（接续链超出可排范围）",
                {"total_conflicts": total, "conflicts": conflicts},
            )
    return {"trips": len(trips), "connections": len(edges)}


# ---------------------------------------------------------------- 实发批次

def apply_actuals(conn, day: str, updates, horizon_minutes: int) -> int:
    """补录/更正/撤销一批实发（actual 为 None 即撤销）。

    逐条校验 -> 写入 -> 全日可行性校验，任一步失败整体回滚，保留原实发。
    """
    seen: set[str] = set()
    for u in updates:
        if u.trip_id in seen:
            raise BatchError("批次内 trip_id 重复", {"trip_id": u.trip_id})
        seen.add(u.trip_id)

    with write_txn(conn):
        planned: dict[str, int] = {}
        for u in updates:
            row = conn.execute(
                "SELECT planned FROM trips WHERE day = ? AND trip_id = ?",
                (day, u.trip_id),
            ).fetchone()
            if row is None:
                raise BatchError("班次不存在", {"trip_id": u.trip_id})
            planned[u.trip_id] = row["planned"]
        for u in updates:
            if u.actual is None:
                continue
            if not 0 <= u.actual < horizon_minutes:
                raise BatchError(
                    "实发分钟超出运营日范围",
                    {"trip_id": u.trip_id, "actual": u.actual, "horizon": horizon_minutes},
                )
            if u.actual < planned[u.trip_id]:
                raise BatchError(
                    "实发不得早于计划",
                    {"trip_id": u.trip_id, "planned": planned[u.trip_id], "actual": u.actual},
                )
        for u in updates:
            conn.execute(
                "UPDATE trips SET actual = ? WHERE day = ? AND trip_id = ?",
                (u.actual, day, u.trip_id),
            )
        total, conflicts = check_feasibility(conn, day, horizon_minutes)
        if total:
            raise BatchConflict(conflicts, total)
    return len(updates)


def check_feasibility(conn, day: str, horizon_minutes: int) -> tuple[int, list[dict]]:
    """全日 EF/LF 双向传播，返回 (矛盾总数, 前 N 条矛盾详情)。

    矛盾只会以两种形式出现（未知班次在两者之间总能取到合法值）：
      1) actual_before_required：有实发班次的实发早于其所需最早时刻；
      2) no_feasible_completion：无实发班次被已知实发逼到 EF > LF。
    """
    trips = {}
    for r in conn.execute(
        "SELECT trip_id, planned, actual, topo FROM trips WHERE day = ?", (day,)
    ):
        trips[r["trip_id"]] = r
    preds: dict[str, list[tuple[str, int]]] = {}
    succs: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute(
        "SELECT pred, succ, min_interval FROM connections WHERE day = ?", (day,)
    ):
        preds.setdefault(r["succ"], []).append((r["pred"], r["min_interval"]))
        succs.setdefault(r["pred"], []).append((r["succ"], r["min_interval"]))
    ordered = sorted(trips.values(), key=lambda row: row["topo"])

    ef: dict[str, int] = {}
    conflicts: list[dict] = []
    for r in ordered:
        tid = r["trip_id"]
        required = r["planned"]
        for pred, m in preds.get(tid, ()):
            required = max(required, ef[pred] + m)
        actual = r["actual"]
        if actual is not None:
            if actual < required:
                ef[tid] = required  # 按所需时刻继续传播，暴露下游级联矛盾
                conflicts.append(
                    _conflict_payload("actual_before_required", tid, trips, preds, ef)
                )
            else:
                ef[tid] = actual
        else:
            ef[tid] = required

    lf: dict[str, int] = {}
    for r in reversed(ordered):
        tid = r["trip_id"]
        if r["actual"] is not None:
            lf[tid] = r["actual"]
        else:
            bound = horizon_minutes - 1
            for succ, m in succs.get(tid, ()):
                bound = min(bound, lf[succ] - m)
            lf[tid] = bound

    for r in ordered:
        tid = r["trip_id"]
        if r["actual"] is None and ef[tid] > lf[tid]:
            conflicts.append(
                _conflict_payload(
                    "no_feasible_completion", tid, trips, preds, ef, latest=lf[tid]
                )
            )

    return len(conflicts), conflicts[:MAX_CONFLICTS_REPORTED]


def _conflict_payload(kind, tid, trips, preds, ef, latest=None) -> dict:
    """从矛盾班次沿绑定边回溯，给出冲突时刻与接续链（源头 -> 本班）。"""
    nodes = [tid]
    edges = []
    cur = tid
    while True:
        binding = None
        for pred, m in preds.get(cur, ()):
            if ef[pred] + m == ef[cur]:
                binding = (pred, m)
                break
        if binding is None:  # ef[cur] 由计划或自身实发决定，到达链源头
            break
        pred, m = binding
        edges.append({"pred": pred, "succ": cur, "min_interval": m})
        nodes.append(pred)
        cur = pred
    nodes.reverse()
    edges.reverse()
    payload = {
        "kind": kind,
        "trip_id": tid,
        "actual": trips[tid]["actual"],
        "required_earliest": ef[tid],
        "chain": [
            {
                "trip_id": t,
                "planned": trips[t]["planned"],
                "actual": trips[t]["actual"],
            }
            for t in nodes
        ],
        "edges": edges,
    }
    if latest is not None:
        payload["latest_feasible"] = latest
    return payload


# ---------------------------------------------------------------- 等待区间

class DayGraph:
    """单运营日的按需缓存视图：只加载查询真正触达的节点与边。"""

    def __init__(self, conn, day: str):
        self.conn = conn
        self.day = day
        self._trips: dict[str, object] = {}
        self._preds: dict[str, list[tuple[str, int]]] = {}
        self._succs: dict[str, list[tuple[str, int]]] = {}

    def trip(self, trip_id: str):
        if trip_id not in self._trips:
            self._trips[trip_id] = self.conn.execute(
                "SELECT trip_id, planned, actual FROM trips WHERE day = ? AND trip_id = ?",
                (self.day, trip_id),
            ).fetchone()
        return self._trips[trip_id]

    def predecessors(self, trip_id: str) -> list[tuple[str, int]]:
        if trip_id not in self._preds:
            rows = self.conn.execute(
                "SELECT pred, min_interval FROM connections WHERE day = ? AND succ = ?",
                (self.day, trip_id),
            ).fetchall()
            self._preds[trip_id] = [(r["pred"], r["min_interval"]) for r in rows]
        return self._preds[trip_id]

    def successors(self, trip_id: str) -> list[tuple[str, int]]:
        if trip_id not in self._succs:
            rows = self.conn.execute(
                "SELECT succ, min_interval FROM connections WHERE day = ? AND pred = ?",
                (self.day, trip_id),
            ).fetchall()
            self._succs[trip_id] = [(r["succ"], r["min_interval"]) for r in rows]
        return self._succs[trip_id]


def _walk(graph: DayGraph, start: str, follow, combine) -> int:
    """迭代式后序遍历（深链不爆栈），返回 start 的聚合值。"""
    memo: dict[str, int] = {}
    seen: set[str] = set()
    stack = [(start, False)]
    while stack:
        node, expanded = stack.pop()
        if not expanded:
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for nxt, _m in follow(node):
                if nxt not in seen:
                    stack.append((nxt, False))
            continue
        memo[node] = combine(node, memo)
    return memo[start]


def earliest_feasible(graph: DayGraph, start: str) -> int:
    """start 的最早可行实发：已知实发固定，否则 max(计划, 各前序 EF+间隔)。"""
    def combine(node, memo):
        row = graph.trip(node)
        if row["actual"] is not None:
            return row["actual"]
        value = row["planned"]
        for pred, m in graph.predecessors(node):
            value = max(value, memo[pred] + m)
        return value

    return _walk(graph, start, graph.predecessors, combine)


def latest_feasible(graph: DayGraph, start: str, horizon_minutes: int) -> int:
    """start 的最晚可行实发：已知实发固定，否则 min(各后继 LF-间隔, 末日分钟)。"""
    def combine(node, memo):
        row = graph.trip(node)
        if row["actual"] is not None:
            return row["actual"]
        value = horizon_minutes - 1
        for succ, m in graph.successors(node):
            value = min(value, memo[succ] - m)
        return value

    return _walk(graph, start, graph.successors, combine)


def wait_interval(graph: DayGraph, trip_id: str, horizon_minutes: int) -> dict | None:
    """本班的接续等待：已知实发等待 + 所有合法补全可达的 [最小, 最大] 等待。"""
    row = graph.trip(trip_id)
    if row is None:
        return None
    planned = row["planned"]
    known = 0
    lo = 0
    hi = 0
    for pred, m in graph.predecessors(trip_id):
        p_row = graph.trip(pred)
        if p_row["actual"] is not None:
            p_lo = p_hi = p_row["actual"]
            known = max(known, p_row["actual"] + m - planned)
        else:
            p_lo = earliest_feasible(graph, pred)
            p_hi = latest_feasible(graph, pred, horizon_minutes)
        lo = max(lo, p_lo + m - planned)
        hi = max(hi, p_hi + m - planned)
    return {
        "known_wait": max(0, known),
        "min_wait": max(0, lo),
        "max_wait": max(0, hi),
    }

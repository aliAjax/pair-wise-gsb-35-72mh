"""Geodetic network adjustment service.

The service stores independent observation epochs, solves distance, angle and
height-difference observations by weighted least squares, and controls the
review/publish workflow.  It intentionally depends only on the Python standard
library.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "geodetic.db"

KINDS = {"distance", "angle", "height_difference"}
STATUS_FLOW = {"draft": {"review"}, "review": {"approved", "draft"}, "approved": {"published"}, "published": set()}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_angle(value: float) -> float:
    return (value + math.pi) % (2 * math.pi) - math.pi


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve a dense linear system using Gaussian elimination with pivoting."""
    n = len(rhs)
    if n == 0:
        return []
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-11:
            raise DomainError("观测网秩亏，缺少足够已知点或连接观测", 422)
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor == 0:
                continue
            for j in range(col, n + 1):
                a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix)
    aug = [matrix[i][:] + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-11:
            raise DomainError("法方程不可逆，无法计算精度", 422)
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                aug[r] = [aug[r][j] - factor * aug[col][j] for j in range(2 * n)]
    return [row[n:] for row in aug]


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS epochs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL,
                    submitted_by TEXT,
                    approved_by TEXT,
                    published_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS points (
                    epoch_id INTEGER NOT NULL REFERENCES epochs(id) ON DELETE CASCADE,
                    point TEXT NOT NULL,
                    x REAL, y REAL, elevation REAL,
                    x_known INTEGER NOT NULL DEFAULT 0,
                    y_known INTEGER NOT NULL DEFAULT 0,
                    elevation_known INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (epoch_id, point)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch_id INTEGER NOT NULL REFERENCES epochs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    p1 TEXT NOT NULL,
                    p2 TEXT NOT NULL,
                    p3 TEXT,
                    value REAL NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'valid',
                    residual REAL,
                    sigma REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch_id INTEGER NOT NULL REFERENCES epochs(id) ON DELETE CASCADE,
                    point TEXT NOT NULL,
                    x REAL, y REAL, elevation REAL,
                    sigma_x REAL, sigma_y REAL, sigma_elevation REAL,
                    residual_rms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch_id INTEGER,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, epoch_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(epoch_id, actor, action, details, created_at) VALUES(?,?,?,?,?)",
            (epoch_id, actor, action, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_epoch(self, name: str, actor: str) -> int:
        if not name.strip():
            raise DomainError("期次名称不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute("INSERT INTO epochs(name,created_by,created_at) VALUES(?,?,?)", (name.strip(), actor, utcnow()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("期次名称已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "epoch.created", {"name": name.strip()})
            return int(cur.lastrowid)

    def get_epoch(self, epoch_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM epochs WHERE id=?", (epoch_id,)).fetchone()
        if not row:
            raise DomainError("期次不存在", 404)
        return dict(row)

    def list_epochs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM epochs ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def set_point(self, epoch_id: int, actor: str, point: str, x: float | None = None, y: float | None = None,
                  elevation: float | None = None, x_known: bool = False, y_known: bool = False,
                  elevation_known: bool = False, role: str = "editor") -> None:
        if role != "editor":
            raise DomainError("只有编辑人员可以维护观测资料", 403)
        point = point.strip().upper()
        if not point:
            raise DomainError("点号不能为空")
        for value in (x, y, elevation):
            if value is not None and not math.isfinite(float(value)):
                raise DomainError("坐标必须是有限数值")
        with self.connect() as conn:
            epoch = conn.execute("SELECT status FROM epochs WHERE id=?", (epoch_id,)).fetchone()
            if not epoch:
                raise DomainError("期次不存在", 404)
            if epoch["status"] != "draft":
                raise DomainError("只有草稿期次可以修改点数据", 409)
            conn.execute(
                """INSERT INTO points(epoch_id,point,x,y,elevation,x_known,y_known,elevation_known)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(epoch_id,point) DO UPDATE SET x=excluded.x,y=excluded.y,elevation=excluded.elevation,
                   x_known=excluded.x_known,y_known=excluded.y_known,elevation_known=excluded.elevation_known""",
                (epoch_id, point, x, y, elevation, int(x_known), int(y_known), int(elevation_known)),
            )
            self._audit(conn, epoch_id, actor, "point.saved", {"point": point})

    def list_points(self, epoch_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM points WHERE epoch_id=? ORDER BY point", (epoch_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_observation(self, epoch_id: int, actor: str, kind: str, p1: str, p2: str,
                        value: float, weight: float = 1.0, p3: str | None = None,
                        role: str = "editor", allow_duplicate: bool = False) -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有编辑人员可以录入观测", 403)
        if kind not in KINDS:
            raise DomainError("观测类型必须是 distance、angle 或 height_difference")
        p1, p2 = p1.strip().upper(), p2.strip().upper()
        p3 = p3.strip().upper() if p3 else None
        if not p1 or not p2 or (kind == "angle" and not p3):
            raise DomainError("观测端点不完整")
        try:
            value, weight = float(value), float(weight)
        except (TypeError, ValueError) as exc:
            raise DomainError("观测值和权必须是数值") from exc
        if not math.isfinite(value) or not math.isfinite(weight) or weight <= 0:
            raise DomainError("观测值必须是有限数，权必须大于 0")
        if kind == "distance" and value <= 0:
            raise DomainError("边长观测必须大于 0")
        if kind == "angle" and not 0 < value < 360:
            raise DomainError("角度应在 0 到 360 度之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            epoch = conn.execute("SELECT status FROM epochs WHERE id=?", (epoch_id,)).fetchone()
            if not epoch:
                raise DomainError("期次不存在", 404)
            if epoch["status"] != "draft":
                raise DomainError("只有草稿期次可以录入观测", 409)
            endpoints = [p1, p2] + ([p3] if p3 else [])
            missing = [p for p in endpoints if not conn.execute("SELECT 1 FROM points WHERE epoch_id=? AND point=?", (epoch_id, p)).fetchone()]
            if missing:
                raise DomainError("以下点号未定义: " + ", ".join(missing))
            duplicate = conn.execute(
                "SELECT id,value FROM observations WHERE epoch_id=? AND kind=? AND p1=? AND p2=? AND COALESCE(p3,'')=COALESCE(?, '') AND status='valid' ORDER BY id",
                (epoch_id, kind, p1, p2, p3),
            ).fetchall()
            tolerance = {"distance": 0.01, "angle": 1e-4, "height_difference": 0.005}[kind]
            near = [dict(r) for r in duplicate if abs(float(r["value"]) - value) <= tolerance]
            if near and not allow_duplicate:
                raise DomainError("发现疑似重复观测", 409)
            cur = conn.execute(
                "INSERT INTO observations(epoch_id,kind,p1,p2,p3,value,weight,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (epoch_id, kind, p1, p2, p3, value, weight, utcnow()),
            )
            self._audit(conn, epoch_id, actor, "observation.added", {"kind": kind, "p1": p1, "p2": p2, "p3": p3, "value": value})
            return {"id": cur.lastrowid, "duplicate_suspect": bool(near)}

    def list_observations(self, epoch_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM observations WHERE epoch_id=? ORDER BY id", (epoch_id,)).fetchall()
        return [dict(row) for row in rows]

    def transition(self, epoch_id: int, actor: str, role: str, action: str) -> dict[str, Any]:
        if role not in {"editor", "reviewer"}:
            raise DomainError("没有审核权限", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM epochs WHERE id=?", (epoch_id,)).fetchone()
            if not row:
                raise DomainError("期次不存在", 404)
            status = row["status"]
            if action == "submit":
                if role != "editor" or status != "draft":
                    raise DomainError("只有草稿可由编辑提交复核", 409)
                conn.execute("UPDATE epochs SET status='review',submitted_by=? WHERE id=?", (actor, epoch_id))
            elif action == "approve":
                if role != "reviewer" or status != "review":
                    raise DomainError("只有复核中成果可由审核员批准", 409)
                if actor in {row["created_by"], row["submitted_by"]}:
                    raise DomainError("创建人或提交人不能自行批准", 403)
                conn.execute("UPDATE epochs SET status='approved',approved_by=? WHERE id=?", (actor, epoch_id))
            elif action == "reject":
                if role != "reviewer" or status != "review":
                    raise DomainError("只有复核中成果可以退回", 409)
                conn.execute("UPDATE epochs SET status='draft',submitted_by=NULL WHERE id=?", (epoch_id,))
            elif action == "publish":
                if role != "reviewer" or status != "approved":
                    raise DomainError("只有已批准成果可以发布", 409)
                conn.execute("UPDATE epochs SET status='published',published_at=? WHERE id=?", (utcnow(), epoch_id))
            else:
                raise DomainError("未知状态操作")
            self._audit(conn, epoch_id, actor, f"epoch.{action}", {})
        return self.get_epoch(epoch_id)

    def _linear_system(self, points: dict[str, dict[str, Any]], observations: list[sqlite3.Row]):
        unknown: dict[tuple[str, str], int] = {}
        for point in sorted(points):
            p = points[point]
            for comp in ("x", "y", "elevation"):
                if not p[f"{comp}_known"]:
                    unknown[(point, comp)] = len(unknown)
        n = len(unknown)
        normal = [[0.0] * n for _ in range(n)]
        rhs = [0.0] * n
        residuals: list[float] = []

        def add_index(row: dict[int, float], key: tuple[str, str], value: float) -> None:
            idx = unknown.get(key)
            if idx is not None:
                row[idx] = row.get(idx, 0.0) + value

        def residual_row(obs: sqlite3.Row):
            kind = obs["kind"]
            p1, p2 = points[obs["p1"]], points[obs["p2"]]
            row: dict[int, float] = {}
            if kind == "distance":
                dx, dy = p2["x"] - p1["x"], p2["y"] - p1["y"]
                dist = math.hypot(dx, dy)
                if dist <= 1e-12:
                    raise DomainError("两个点重合，不能计算边长")
                residual = dist - obs["value"]
                add_index(row, (obs["p1"], "x"), -dx / dist)
                add_index(row, (obs["p1"], "y"), -dy / dist)
                add_index(row, (obs["p2"], "x"), dx / dist)
                add_index(row, (obs["p2"], "y"), dy / dist)
            elif kind == "height_difference":
                residual = p2["elevation"] - p1["elevation"] - obs["value"]
                add_index(row, (obs["p1"], "elevation"), -1.0)
                add_index(row, (obs["p2"], "elevation"), 1.0)
            else:
                p3 = points[obs["p3"]]
                dx1, dy1 = p2["x"] - p1["x"], p2["y"] - p1["y"]
                dx2, dy2 = p3["x"] - p1["x"], p3["y"] - p1["y"]
                r1, r2 = math.hypot(dx1, dy1), math.hypot(dx2, dy2)
                if min(r1, r2) <= 1e-12:
                    raise DomainError("角度观测中存在重合点")
                theta1, theta2 = math.atan2(dy1, dx1), math.atan2(dy2, dx2)
                observed = math.radians(obs["value"])
                residual = _norm_angle(theta2 - theta1 - observed)
                # derivative of theta(direction p1->p); theta2 is the angle at p1 between p2 and p3.
                add_index(row, (obs["p1"], "x"), -dy1 / r1**2 + dy2 / r2**2)
                add_index(row, (obs["p1"], "y"), dx1 / r1**2 - dx2 / r2**2)
                add_index(row, (obs["p2"], "x"), dy1 / r1**2)
                add_index(row, (obs["p2"], "y"), -dx1 / r1**2)
                add_index(row, (obs["p3"], "x"), -dy2 / r2**2)
                add_index(row, (obs["p3"], "y"), dx2 / r2**2)
            return row, residual

        residuals = []
        for obs in observations:
            row, residual = residual_row(obs)
            w = float(obs["weight"])
            residuals.append(residual)
            left = [row.get(i, 0.0) * w for i in range(n)]
            for i in range(n):
                if left[i] == 0:
                    continue
                rhs[i] -= left[i] * residual
                for j in range(n):
                    normal[i][j] += left[i] * row.get(j, 0.0)
        return unknown, normal, rhs, residuals

    def adjust(self, epoch_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有编辑人员可以执行平差", 403)
        with self.connect() as conn:
            epoch = conn.execute("SELECT * FROM epochs WHERE id=?", (epoch_id,)).fetchone()
            if not epoch:
                raise DomainError("期次不存在", 404)
            if epoch["status"] != "draft":
                raise DomainError("只有草稿期次可以平差", 409)
            point_rows = conn.execute("SELECT * FROM points WHERE epoch_id=?", (epoch_id,)).fetchall()
            observations = conn.execute("SELECT * FROM observations WHERE epoch_id=? AND status='valid'", (epoch_id,)).fetchall()
            if len(observations) < 2:
                raise DomainError("至少需要两条有效观测")
            points = {r["point"]: {**dict(r), "x": r["x"], "y": r["y"], "elevation": r["elevation"]} for r in point_rows}
            for p in points.values():
                for comp in ("x", "y", "elevation"):
                    if p[comp] is None:
                        p[comp] = 0.0 if comp != "elevation" else 0.0
            iteration = 0
            residuals: list[float] = []
            unknown: dict[tuple[str, str], int] = {}
            sigma0 = 1.0
            covariance: list[list[float]] = []
            while iteration < 12:
                unknown, normal, rhs, residuals = self._linear_system(points, observations)
                if not unknown:
                    break
                delta = _solve(normal, rhs)
                for (point, comp), idx in unknown.items():
                    points[point][comp] += delta[idx]
                iteration += 1
                if max((abs(v) for v in delta), default=0.0) < 1e-7:
                    break
            if not unknown:
                _, _, _, residuals = self._linear_system(points, observations)
            redundancy = max(1, len(observations) - len(unknown))
            weighted_ss = sum(float(o["weight"]) * r * r for o, r in zip(observations, residuals))
            sigma0 = math.sqrt(weighted_ss / redundancy)
            covariance = _inverse(normal) if unknown else []
            if unknown:
                covariance = [[v * sigma0 * sigma0 for v in row] for row in covariance]
            rms = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else 0.0
            stamped = utcnow()
            conn.execute("DELETE FROM results WHERE epoch_id=?", (epoch_id,))
            by_point = {point: {"point": point, "x": p["x"], "y": p["y"], "elevation": p["elevation"],
                                "sigma_x": 0.0, "sigma_y": 0.0, "sigma_elevation": 0.0} for point, p in points.items()}
            for (point, comp), idx in unknown.items():
                by_point[point][f"sigma_{comp}"] = math.sqrt(max(0.0, covariance[idx][idx]))
            for result in by_point.values():
                conn.execute(
                    """INSERT INTO results(epoch_id,point,x,y,elevation,sigma_x,sigma_y,sigma_elevation,residual_rms,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (epoch_id, result["point"], result["x"], result["y"], result["elevation"], result["sigma_x"],
                     result["sigma_y"], result["sigma_elevation"], rms, stamped),
                )
            for obs, residual in zip(observations, residuals):
                sigma_res = abs(residual) / math.sqrt(max(float(obs["weight"]), 1e-12))
                status = "outlier" if len(observations) - len(unknown) > 0 and abs(math.sqrt(float(obs["weight"])) * residual) > 3.5 * max(sigma0, 1e-12) else "valid"
                conn.execute("UPDATE observations SET residual=?,sigma=?,status=? WHERE id=?", (residual, sigma_res, status, obs["id"]))
            self._audit(conn, epoch_id, actor, "network.adjusted", {"iterations": iteration, "rms": rms, "outliers": sum(1 for o, r in zip(observations, residuals) if abs(math.sqrt(float(o["weight"])) * r) > 3.5 * max(sigma0, 1e-12))})
            return {"epoch_id": epoch_id, "iterations": iteration, "residual_rms": rms, "sigma0": sigma0, "points": list(by_point.values())}

    def results(self, epoch_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM results WHERE epoch_id=? ORDER BY point", (epoch_id,)).fetchall()
        return [dict(row) for row in rows]

    def compare(self, base_epoch: int, target_epoch: int) -> list[dict[str, Any]]:
        base = {r["point"]: r for r in self.results(base_epoch)}
        target = {r["point"]: r for r in self.results(target_epoch)}
        if not base or not target:
            raise DomainError("要比较的期次必须先完成平差", 422)
        output = []
        for point in sorted(set(base) & set(target)):
            dx = target[point]["x"] - base[point]["x"]
            dy = target[point]["y"] - base[point]["y"]
            dz = target[point]["elevation"] - base[point]["elevation"]
            output.append({"point": point, "dx": dx, "dy": dy, "delevation": dz, "planar_shift": math.hypot(dx, dy)})
        return output

    def audit(self, epoch_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if epoch_id is None:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM audit_log WHERE epoch_id=? ORDER BY id DESC", (epoch_id,)).fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> int:
    if db.list_epochs():
        return int(db.list_epochs()[0]["id"])
    epoch = db.create_epoch("2026 基线复核第一期", "alice")
    # A and B are fixed controls. C and D are approximate coordinates to solve.
    db.set_point(epoch, "alice", "A", 0, 0, 100, True, True, True)
    db.set_point(epoch, "alice", "B", 100, 0, 100, True, True, True)
    db.set_point(epoch, "alice", "C", 50, 50, 110)
    db.set_point(epoch, "alice", "D", 150, 60, 108)
    # The combination contains two linearly independent directions and a loop;
    # values are the observations that should be recovered by the adjustment.
    observations = [
        ("distance", "A", "B", None, 100.0),
        ("distance", "B", "C", None, math.hypot(50.0, 50.0)),
        ("distance", "C", "D", None, math.hypot(100.0, 10.0)),
        ("distance", "A", "C", None, math.hypot(50.0, 50.0)),
        ("distance", "B", "D", None, math.hypot(50.0, 60.0)),
        ("angle", "B", "C", "A", 45.0),
        ("angle", "C", "B", "D", 50.7105931375),
        ("height_difference", "B", "C", None, 10.0),
        ("height_difference", "C", "D", None, -2.0),
    ]
    for kind, p1, p2, p3, value in observations:
        db.add_observation(epoch, "alice", kind, p1, p2, value, 1.0, p3, allow_duplicate=True)
    return epoch


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "GeodeticAdjustment/1.0"

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        body = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _actor(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._json({"ok": True})
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "epochs"]:
                return self._json({"epochs": self.db.list_epochs()})
            if len(parts) == 3 and parts[:2] == ["api", "epochs"]:
                return self._json(self.db.get_epoch(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "epochs"] and parts[3] == "observations":
                return self._json({"observations": self.db.list_observations(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "epochs"] and parts[3] == "points":
                return self._json({"points": self.db.list_points(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "epochs"] and parts[3] == "results":
                return self._json({"results": self.db.results(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "epochs"] and parts[3] == "audit":
                return self._json({"audit": self.db.audit(int(parts[2]))})
            if len(parts) == 5 and parts[:2] == ["api", "epochs"] and parts[3] == "compare":
                return self._json({"comparison": self.db.compare(int(parts[2]), int(parts[4]))})
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._json({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._body()
            actor, role = self._actor()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "epochs"]:
                epoch_id = self.db.create_epoch(str(body.get("name", "")), actor)
                return self._json(self.db.get_epoch(epoch_id), 201)
            if len(parts) == 5 and parts[:2] == ["api", "epochs"] and parts[3] == "transition":
                return self._json(self.db.transition(int(parts[2]), actor, role, parts[4]))
            if len(parts) == 4 and parts[:2] == ["api", "epochs"]:
                epoch_id = int(parts[2])
                if parts[3] == "points":
                    self.db.set_point(epoch_id, actor, str(body.get("point", "")), body.get("x"), body.get("y"), body.get("elevation"),
                                      bool(body.get("x_known")), bool(body.get("y_known")), bool(body.get("elevation_known")), role)
                    return self._json({"ok": True}, 201)
                if parts[3] == "observations":
                    result = self.db.add_observation(epoch_id, actor, str(body.get("kind", "")), str(body.get("p1", "")),
                                                     str(body.get("p2", "")), body.get("value"), body.get("weight", 1), body.get("p3"),
                                                     role, bool(body.get("allow_duplicate")))
                    return self._json(result, 201)
                if parts[3] == "adjust":
                    return self._json(self.db.adjust(epoch_id, actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError, TypeError) as exc:
            self._json({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[geodetic] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="大地测量网平差服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8006")))
    parser.add_argument("--db", default=os.getenv("GEODETIC_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例期次")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        epoch = seed_demo(db)
        print(f"initialized database at {args.db}; sample epoch={epoch}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"geodetic adjustment listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()

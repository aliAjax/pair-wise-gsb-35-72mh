"""异常观测判定层。

只包含与存储、HTTP 无关的纯函数和常量：限差计算、异常原因描述、
异常处理状态、提交条件以及两期平差成果的前后变化。``app.py`` 负责
持久化和接口，``static/index.html`` 负责页面，三者互不嵌套。
"""
from __future__ import annotations

import math
from typing import Any

# 标准化残差（|v| * sqrt(w) / sigma0）超过该倍数即判为异常。
OUTLIER_K = 3.5

# 异常处理状态。
OPEN = "open"                              # 待处理
REMEASURE_PLANNED = "remeasure_planned"    # 已安排复测，尚未回填
EXCLUDED = "excluded"                      # 已填依据排除
REMEASURED = "remeasured"                  # 已回填复测成果
CLEARED = "cleared"                        # 重算后残差回到限差内，自动消除

# 未处理完的状态：存在这些状态的异常时禁止提交复核。
UNRESOLVED_STATES = (OPEN, REMEASURE_PLANNED)
RESOLVED_STATES = (EXCLUDED, REMEASURED, CLEARED)

KIND_LABELS = {"distance": "边长", "angle": "角度", "height_difference": "高差"}


def standardised_residual(residual: float, weight: float) -> float:
    """标准化残差 |v| * sqrt(w)。"""
    return abs(residual) * math.sqrt(max(weight, 1e-12))


def residual_limit(weight: float, sigma0: float) -> float:
    """该观测的残差限差（与残差同单位）：K * sigma0 / sqrt(w)。"""
    return OUTLIER_K * max(sigma0, 1e-12) / math.sqrt(max(weight, 1e-12))


def is_outlier(residual: float, weight: float, sigma0: float, redundancy: int) -> bool:
    """残差超差判定；没有多余观测时不判异常。"""
    if redundancy <= 0:
        return False
    return standardised_residual(residual, weight) > OUTLIER_K * max(sigma0, 1e-12)


def to_display(kind: str, raw: float) -> float:
    """把内部残差换算成展示单位：角度弧度换角秒，距离/高差保持米。"""
    if kind == "angle":
        return raw * 180.0 / math.pi * 3600.0
    return raw


def unit(kind: str) -> str:
    return "″" if kind == "angle" else "m"


def describe(kind: str, p1: str, p2: str, p3: str | None,
             observed: float, residual: float, weight: float, sigma0: float) -> str:
    """生成给作业员看的异常原因，给出残差、限差和超标倍数。"""
    if kind == "angle":
        target = f"{p2}-{p1}-{p3}"
    else:
        target = f"{p1}-{p2}"
    ratio = standardised_residual(residual, weight) / max(sigma0, 1e-12)
    u = unit(kind)
    return (
        f"{KIND_LABELS.get(kind, kind)} {target}：标准化残差 {ratio:.2f}σ 超过限差 {OUTLIER_K:.2f}σ"
        f"（观测值 {to_display(kind, observed):.4f}{u}，残差 {to_display(kind, residual):+.4f}{u}，"
        f"限差 ±{to_display(kind, residual_limit(weight, sigma0)):.4f}{u}，σ₀={sigma0:.4f}）"
    )


def can_submit(open_states: list[str], run_count: int) -> str | None:
    """提交复核的判定：返回拒因字符串，允许时返回 None。"""
    if run_count <= 0:
        return "尚未完成平差，不能提交复核"
    unresolved = [s for s in open_states if s in UNRESOLVED_STATES]
    if unresolved:
        return f"仍有 {len(unresolved)} 条异常观测未处理完，不能提交复核"
    return None


def diff_runs(previous: dict[str, Any] | None, latest: dict[str, Any] | None) -> dict[str, Any] | None:
    """对比相邻两次成功平差，输出审核员需要的前后变化。

    入参为 ``adjust_runs`` 的批次字典（点位数据嵌在 ``snapshot`` 内）。
    """
    if not previous or not latest:
        return None
    prev_points = {p["point"]: p for p in previous.get("snapshot", {}).get("points", [])}
    latest_points = {p["point"]: p for p in latest.get("snapshot", {}).get("points", [])}
    changes = []
    for point in sorted(set(prev_points) & set(latest_points)):
        a, b = prev_points[point], latest_points[point]
        changes.append({
            "point": point,
            "dx": b["x"] - a["x"],
            "dy": b["y"] - a["y"],
            "delevation": b["elevation"] - a["elevation"],
            "dsigma_x": b.get("sigma_x", 0.0) - a.get("sigma_x", 0.0),
            "dsigma_y": b.get("sigma_y", 0.0) - a.get("sigma_y", 0.0),
            "dsigma_elevation": b.get("sigma_elevation", 0.0) - a.get("sigma_elevation", 0.0),
        })
    return {
        "previous_run_id": previous.get("run_id"),
        "latest_run_id": latest.get("run_id"),
        "sigma0_before": previous.get("sigma0"),
        "sigma0_after": latest.get("sigma0"),
        "rms_before": previous.get("residual_rms"),
        "rms_after": latest.get("residual_rms"),
        "points": changes,
    }

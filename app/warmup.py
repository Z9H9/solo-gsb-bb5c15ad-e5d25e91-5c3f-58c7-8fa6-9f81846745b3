"""仪器预热稳定放行：在已提交读数中寻找最早完成的连续稳定段。

纯只读查询：仅读取 committed 表中已提交包的原始 payload，不触碰原始包、
缓冲、重复、作废、告警与校准数据，也不改写任何已存内容。

候选读数：
- 仅取 metric 相同的 measurement；有效时间优先用 measurement 自身 ts，
  否则回退到所属包的 sent_at；两者都没有的测量不参与（计入
  excluded_no_time，仅用于诊断，不计入任何时间范围）；
- 有效时间归一化为 UTC（naive 时间按 UTC），按半开区间 [from, to) 过滤；
- 范围内单位与请求 unit 逐字相同的读数才参与（读数缺单位视为不符；
  范围内单位不符的读数计入 excluded_unit_mismatch，不参与）；
- 排序键为 (有效时间, 包 seq, 包内原始位置)：同时间先按包序号、再按
  包内位置。

稳定段（排序后候选序列上的连续子序列）须同时满足：
- 段内最大值与最小值之差不超过 max_fluctuation（边界合格）；
- 段内相邻读数的有效时间差均不超过 max_gap_seconds；
- 首末有效时间跨度达到 min_duration_seconds（边界合格）；
- 段内读数条数不少于 min_readings。

寻找"最早完成"的稳定段：按右端点（完成时间 = 段末读数的有效时间）
从早到晚扫描，对每个右端点取满足全部约束的最长可行窗口（左端点尽可能
早）——起点更早只会让时长更足、读数更多，因此该窗口一旦合格就是该
完成时间下的最优段；同一完成时间自然取到最早起点。滑窗用两个单调
双端队列维护段内最大值 / 最小值，整体 O(n)。
"""

import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, List, Optional, Tuple

from .schemas import (
    WarmupReading,
    WarmupReleaseResponse,
    WarmupStableSegment,
)

_US_PER_SECOND = 1_000_000


class WarmupError(ValueError):
    """放行评估参数非法（时间范围倒置、阈值非法等）。"""


@dataclass(frozen=True)
class _Reading:
    """一条候选读数；输出时的候选序号在排序后按位置给出（从 1 起）。"""

    seq: int
    position: int
    time: datetime
    time_source: str  # "ts" / "sent_at"
    value: float
    unit: Optional[str]


def _as_utc(dt: datetime) -> datetime:
    """naive 时间一律视为 UTC，保证比较行为确定。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_time(raw: str) -> datetime:
    return _as_utc(datetime.fromisoformat(raw))


def _delta_us(a: datetime, b: datetime) -> int:
    """两个有效测量时间的绝对差（整数微秒，规避浮点误差），a >= b。"""
    delta = a - b
    return (delta.days * 86400 + delta.seconds) * _US_PER_SECOND + delta.microseconds


def load_readings(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    unit: str,
    from_: datetime,
    to: datetime,
) -> Tuple[List[_Reading], int, int]:
    """读取一台设备在 [from, to) 内单位相符的同指标候选读数。

    仅扫描已提交包；测量时间优先取 ts、否则取包 sent_at，均无则忽略。
    返回 (候选读数, 无有效时间读数条数, 范围内单位不符读数条数)。
    """
    readings: List[_Reading] = []
    excluded_no_time = 0
    excluded_unit_mismatch = 0
    rows = conn.execute(
        "SELECT seq, payload_json FROM committed WHERE device_id = ? ORDER BY seq",
        (device_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        raw_sent_at = payload.get("sent_at")
        sent_at = _parse_time(raw_sent_at) if raw_sent_at else None
        for position, measurement in enumerate(payload.get("measurements", [])):
            if measurement.get("metric") != metric:
                continue
            raw_ts = measurement.get("ts")
            if raw_ts:
                time = _parse_time(raw_ts)
                time_source = "ts"
            elif sent_at is not None:
                time = sent_at
                time_source = "sent_at"
            else:
                excluded_no_time += 1
                continue
            if not (from_ <= time < to):
                continue
            if measurement.get("unit") != unit:
                excluded_unit_mismatch += 1
                continue
            readings.append(
                _Reading(
                    seq=row["seq"],
                    position=position,
                    time=time,
                    time_source=time_source,
                    value=float(measurement["value"]),
                    unit=measurement.get("unit"),
                )
            )
    readings.sort(key=lambda p: (p.time, p.seq, p.position))
    return readings, excluded_no_time, excluded_unit_mismatch


def find_stable_segment(
    readings: List[_Reading],
    max_fluctuation: float,
    min_duration_seconds: float,
    min_readings: int,
    max_gap_seconds: float,
) -> Optional[Tuple[int, int, float]]:
    """寻找最早完成的连续稳定段。

    返回 (左端下标, 右端下标, 实际波动)，候选下标均从 0 起；无合格段
    返回 None。约束的比较边界全部合格：波动不超过、跨度达到、相邻间隔
    不超过。
    """
    min_duration_us = round(min_duration_seconds * _US_PER_SECOND)
    max_gap_us = round(max_gap_seconds * _US_PER_SECOND)

    # 单调队列存候选下标：max_q 队首为窗口最大值，min_q 队首为最小值
    max_q: Deque[int] = deque()
    min_q: Deque[int] = deque()
    left = 0
    for right, point in enumerate(readings):
        while max_q and readings[max_q[-1]].value <= point.value:
            max_q.pop()
        max_q.append(right)
        while min_q and readings[min_q[-1]].value >= point.value:
            min_q.pop()
        min_q.append(right)

        # 窗口必须是连续候选子序列：任一相邻采样间隔超限时，
        # 任何跨过该间隔的段都不合格，左端直接移到右端点。
        gap_to_prev = _delta_us(point.time, readings[right - 1].time)
        if right > left and gap_to_prev > max_gap_us:
            left = right
            max_q.clear()
            min_q.clear()
            max_q.append(right)
            min_q.append(right)

        # 收缩左端直到波动达标；左端只进不退（更早被弹出的起点在之后
        # 任何右端点下都不可能重新满足波动约束）
        while readings[max_q[0]].value - readings[min_q[0]].value > max_fluctuation:
            if max_q[0] == left:
                max_q.popleft()
            if min_q[0] == left:
                min_q.popleft()
            left += 1

        span_us = _delta_us(point.time, readings[left].time)
        count = right - left + 1
        if span_us >= min_duration_us and count >= min_readings:
            actual = readings[max_q[0]].value - readings[min_q[0]].value
            return left, right, actual
    return None


def _reading_out(index: int, r: _Reading) -> WarmupReading:
    return WarmupReading(
        index=index,
        seq=r.seq,
        position=r.position,
        time=r.time,
        time_source=r.time_source,
        value=r.value,
        unit=r.unit,
    )


def evaluate_warmup(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    unit: str,
    from_: datetime,
    to: datetime,
    max_fluctuation: float,
    min_duration_seconds: float,
    min_readings: int,
    max_gap_seconds: float,
) -> WarmupReleaseResponse:
    """执行预热稳定放行评估。调用方需保证设备存在。"""
    from_ = _as_utc(from_)
    to = _as_utc(to)
    if from_ >= to:
        raise WarmupError("参数 from 必须早于 to")
    if max_fluctuation < 0:
        raise WarmupError("max_fluctuation 不能为负数")
    if min_duration_seconds < 0:
        raise WarmupError("min_duration_seconds 不能为负数")
    if max_gap_seconds < 0:
        raise WarmupError("max_gap_seconds 不能为负数")
    if min_readings < 1:
        raise WarmupError("min_readings 至少为 1")

    readings, no_time, unit_mismatch = load_readings(
        conn, device_id, metric, unit, from_, to
    )

    segment: Optional[WarmupStableSegment] = None
    released = False
    not_released_reason: Optional[str] = None
    found = find_stable_segment(
        readings,
        max_fluctuation,
        min_duration_seconds,
        min_readings,
        max_gap_seconds,
    )
    if found is not None:
        left, right, actual = found
        members = [
            _reading_out(k + 1, readings[k]) for k in range(left, right + 1)
        ]
        start = readings[left].time
        end = readings[right].time
        gap_values = [
            _delta_us(readings[i].time, readings[i - 1].time)
            for i in range(left + 1, right + 1)
        ]
        segment = WarmupStableSegment(
            start_time=start,
            end_time=end,
            duration_seconds=_delta_us(end, start) / _US_PER_SECOND,
            reading_count=len(members),
            max_gap_seconds=(max(gap_values) if gap_values else 0)
            / _US_PER_SECOND,
            actual_fluctuation=actual,
            readings=members,
        )
        released = True
    else:
        if not readings:
            not_released_reason = (
                f"UTC 时间范围 [{from_.isoformat()}, {to.isoformat()}) 内"
                f"没有指标 {metric}、单位 {unit} 的有效读数，无法判定稳定"
            )
        else:
            not_released_reason = (
                f"范围内 {len(readings)} 条有效读数中不存在同时满足"
                f"波动不超过 {max_fluctuation:g}、相邻间隔不超过 "
                f"{max_gap_seconds:g} 秒、首末跨度达到 {min_duration_seconds:g} 秒"
                f"且读数不少于 {min_readings} 条的连续稳定段"
            )

    return WarmupReleaseResponse(
        device_id=device_id,
        metric=metric,
        unit=unit,
        **{"from": from_},
        to=to,
        max_fluctuation=max_fluctuation,
        min_duration_seconds=min_duration_seconds,
        min_readings=min_readings,
        max_gap_seconds=max_gap_seconds,
        released=released,
        not_released_reason=not_released_reason,
        segment=segment,
        candidate_count=len(readings),
        excluded_no_time_count=no_time,
        excluded_unit_mismatch_count=unit_mismatch,
    )

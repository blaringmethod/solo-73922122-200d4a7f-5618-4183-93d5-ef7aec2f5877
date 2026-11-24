"""两片同规格晶圆之间对调同名非基准工序的明细。

一次对调只交换一道同名工序：两片晶圆该工序上的**定位点与缺陷整份互换**，编号
（定位点编号 / 缺陷号）与行列都随明细一起走，因此两份明细可以带有相同编号。

对调的全部计算都在内存里的晶圆副本上完成，数据库里的已提交状态在试算期间保持
不变——补点、改点、撤点照常进行。确认时再用试算时记下的修订号核对：任一片在
取数后被成功改动过定位点或对调过明细（修订号变化），就拒绝本次确认并提示重新
试算，避免按旧快照覆盖新数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    Defect,
    Locator,
    NoSolutionError,
    Step,
    ValidationError,
    Wafer,
)


class StaleSnapshotError(ValidationError):
    """取数后晶圆已被改动（修订号变化），试算结果失效。"""


@dataclass(frozen=True)
class StepPending:
    seq: int
    name: str
    pending_before: int
    pending_after: int
    swapped: bool


@dataclass(frozen=True)
class WaferTrial:
    wafer_id: int
    name: str
    revision: int
    grid_size: int
    pending_before: int
    pending_after: int
    schemes_before: int
    schemes_after: int
    steps: tuple[StepPending, ...]


@dataclass(frozen=True)
class SwapTrial:
    step_name: str
    step_seq: int
    wafers: tuple[WaferTrial, WaferTrial]
    # 明细互换后各片该工序上参与跨工序约束的同号定位点，用于页面核对。
    shared_locators: tuple[tuple[str, ...], tuple[str, ...]]


@dataclass
class _Detail:
    """一道工序明细的可移植副本：编号与行列随明细走，不带走原数据库主键。"""

    locators: list[tuple[str, int, int]] = field(default_factory=list)
    defects: list[tuple[str, int, int]] = field(default_factory=list)


def _step_detail(step: Step) -> _Detail:
    return _Detail(
        locators=[(loc.pid, loc.row, loc.col) for loc in step.locators],
        defects=[(d.did, d.row, d.col) for d in step.defects],
    )


def _replace_detail(step: Step, detail: _Detail) -> Step:
    return Step(
        id=step.id,
        seq=step.seq,
        name=step.name,
        locators=[
            Locator(id=-(i + 1), pid=pid, row=row, col=col)
            for i, (pid, row, col) in enumerate(detail.locators)
        ],
        defects=[
            Defect(id=-(i + 1), did=did, row=row, col=col)
            for i, (did, row, col) in enumerate(detail.defects)
        ],
    )


def _clone_with_swap(wafer: Wafer, step_seq: int, incoming: _Detail) -> Wafer:
    steps = [
        _replace_detail(step, incoming) if step.seq == step_seq else step
        for step in wafer.steps
    ]
    return Wafer(
        id=wafer.id,
        name=wafer.name,
        grid_size=wafer.grid_size,
        revision=wafer.revision,
        created_at=wafer.created_at,
        updated_at=wafer.updated_at,
        steps=steps,
    )


def _pending_per_step(wafer: Wafer) -> tuple[int, int, list[tuple[int, str, int]]]:
    """返回（可行方案数、待定缺陷总数、逐工序待定数）。"""
    from .model import analyze

    analysis = analyze(wafer)
    rows: list[tuple[int, str, int]] = []
    total = 0
    if analysis.feasibility.solvable:
        for step in wafer.steps:
            count = sum(1 for d in step.defects if analysis.result(step, d).status == "pending")
            rows.append((step.seq, step.name, count))
            total += count
    else:
        rows = [(s.seq, s.name, 0) for s in wafer.steps]
    return analysis.feasibility.scheme_count, total, rows


def _assert_same_spec(left: Wafer, right: Wafer) -> None:
    if left.id == right.id:
        raise ValidationError("请选择两片不同的晶圆，不能与自身对调。")
    if left.grid_size != right.grid_size:
        raise ValidationError(
            f"两片晶圆网格规格不同：{left.name} 为 {left.grid_size}×{left.grid_size}，"
            f"{right.name} 为 {right.grid_size}×{right.grid_size}。"
        )
    if len(left.steps) != len(right.steps):
        raise ValidationError(
            f"两片晶圆工序数不同（{len(left.steps)} 道 / {len(right.steps)} 道），不是同规格晶圆。"
        )
    pairs = list(zip(left.steps, right.steps))
    if any(a.seq != b.seq or a.name != b.name for a, b in pairs):
        raise ValidationError("两片晶圆的工序序号或名称不一致，无法按同名工序对调。")


def _find_steps(left: Wafer, right: Wafer, step_name: str) -> tuple[Step, Step, int]:
    match_l = next((s for s in left.steps if s.name == step_name), None)
    match_r = next((s for s in right.steps if s.name == step_name), None)
    if match_l is None or match_r is None:
        raise ValidationError(f"两片晶圆上都找不到同名工序“{step_name}”。")
    if match_l.seq != match_r.seq:
        raise ValidationError(f"工序“{step_name}”在两片晶圆上的序号不一致。")
    if match_l.is_baseline:
        raise ValidationError("基准工序（第一道）不参与对调，请选择非基准工序。")
    return match_l, match_r, match_l.seq


def _assert_shared_locator(wafer: Wafer, step_seq: int) -> list[str]:
    target = next(s for s in wafer.steps if s.seq == step_seq)
    other_pids: set[str] = set()
    for step in wafer.steps:
        if step.seq != step_seq:
            other_pids.update(loc.pid for loc in step.locators)
    shared = sorted(set(loc.pid for loc in target.locators) & other_pids)
    if not shared:
        raise NoSolutionError(
            f"换入 {wafer.name} 的“{target.name}”明细与该片其他工序没有任何同号定位点，"
            "无法对齐，本次对调不会保存。"
        )
    return shared


def _trial_for(
    wafer: Wafer, swapped: Wafer, step_seq: int
) -> WaferTrial:
    schemes_before, before_total, before_rows = _pending_per_step(wafer)
    schemes_after, after_total, after_rows = _pending_per_step(swapped)
    before_map = {seq: count for seq, _name, count in before_rows}
    after_map = {seq: count for seq, _name, count in after_rows}
    if schemes_after == 0:
        raise NoSolutionError(
            f"对调后 {wafer.name} 不存在任何可行方案，本次对调不会保存。"
        )
    return WaferTrial(
        wafer_id=wafer.id,
        name=wafer.name,
        revision=wafer.revision,
        grid_size=wafer.grid_size,
        pending_before=before_total,
        pending_after=after_total,
        schemes_before=schemes_before,
        schemes_after=schemes_after,
        steps=tuple(
            StepPending(
                seq=seq,
                name=name,
                pending_before=before_map.get(seq, 0),
                pending_after=after_map.get(seq, 0),
                swapped=seq == step_seq,
            )
            for seq, name, _ in before_rows
        ),
    )


def plan_swap(left: Wafer, right: Wafer, step_name: str) -> SwapTrial:
    """在内存里试算对调，返回两片晶圆换前/换后的待定缺陷数（不落库）。"""
    _assert_same_spec(left, right)
    step_l, step_r, step_seq = _find_steps(left, right, step_name)

    detail_l = _step_detail(step_l)
    detail_r = _step_detail(step_r)
    swapped_left = _clone_with_swap(left, step_seq, detail_r)
    swapped_right = _clone_with_swap(right, step_seq, detail_l)

    # “换入的明细须和该片另一道工序有同号定位点，换后两片各自仍能对齐”。
    shared_l = _assert_shared_locator(swapped_left, step_seq)
    shared_r = _assert_shared_locator(swapped_right, step_seq)

    trial_l = _trial_for(left, swapped_left, step_seq)
    trial_r = _trial_for(right, swapped_right, step_seq)
    return SwapTrial(
        step_name=step_name,
        step_seq=step_seq,
        wafers=(trial_l, trial_r),
        shared_locators=(tuple(shared_l), tuple(shared_r)),
    )

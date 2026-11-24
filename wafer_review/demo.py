"""演示数据：6 道工序、8×8 晶粒网格。

六张图之间故意安排了多种对齐关系（旋转、镜像、镜像+旋转），并且让“封装”工序
只有 C1 一个定位点、且基准图没有 C1——它必须靠与“沉积”共享的 C1 才能对齐。
因此：

* 封装 D101/D103/D105 能给出唯一坐标；
* 封装 D102/D104 在可行方案下会落到不同格子，标为待定；
* 撤去“沉积”或“封装”的 C1，封装的查询会全部变成待定；
* 把封装的 C1 更正到 (4,4)，D104 会从待定变成确定 (5,5)。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .geometry import Transform, invert_transform

GRID = 8

BASELINE_LOCATORS: dict[str, tuple[int, int]] = {
    "L1": (2, 7),
    "L2": (2, 6),
    "L3": (7, 2),
    "L4": (6, 6),
    "X1": (4, 4),
}

BASELINE_DEFECTS: dict[str, tuple[int, int]] = {
    "D101": (4, 4),
    "D102": (5, 3),
    "D103": (8, 8),
    "D104": (4, 5),
    "D105": (1, 1),
}


@dataclass(frozen=True)
class DemoStep:
    name: str
    transform: Transform
    locators: tuple[str, ...]
    defects: tuple[str, ...]
    extra_locators: tuple[tuple[str, tuple[int, int]], ...] = ()


# C1 是跨工序定位点（基准图没有它）：沉积与封装都把它放在 (4,5)。
DEMO_STEPS: tuple[DemoStep, ...] = (
    DemoStep("光刻", (False, 0), ("L1", "L2", "L3", "L4", "X1"), tuple(BASELINE_DEFECTS)),
    DemoStep("刻蚀", (False, 1), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep(
        "沉积",
        (True, 0),
        ("L1", "L2", "L3", "L4", "X1"),
        tuple(BASELINE_DEFECTS),
        (("C1", (4, 5)),),
    ),
    DemoStep("研磨", (False, 2), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep("检测", (True, 1), ("L1", "L2", "L3", "L4"), tuple(BASELINE_DEFECTS)),
    DemoStep("封装", (False, 3), (), tuple(BASELINE_DEFECTS), (("C1", (4, 5)),)),
)


def render_csv(step: DemoStep) -> str:
    """按工序的变换把基准坐标逆变换成该道工序图上的输入坐标。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["类型", "编号", "行", "列"])
    for pid in step.locators:
        row, col = invert_transform(step.transform, *BASELINE_LOCATORS[pid], GRID)
        writer.writerow(["定位点", pid, row, col])
    for pid, cell in step.extra_locators:
        writer.writerow(["定位点", pid, *cell])
    for did in step.defects:
        row, col = invert_transform(step.transform, *BASELINE_DEFECTS[did], GRID)
        writer.writerow(["缺陷", did, row, col])
    return buf.getvalue()


def demo_samples() -> list[tuple[str, str]]:
    """返回 [(工序名, CSV 文本), ...]，第一个为基准。"""
    return [(step.name, render_csv(step)) for step in DEMO_STEPS]


def expected_baseline() -> dict[str, tuple[int, int]]:
    return dict(BASELINE_DEFECTS)


def transform_of(name: str) -> Transform:
    return next(s.transform for s in DEMO_STEPS if s.name == name)


# ---------------------------------------------------------------------------
# 工序传反演示：两片同规格晶圆的“封装”CSV 在上传时传反了。
#
# 两片晶圆前五道工序完全相同；两片封装明细都只靠共享定位点对齐，且刻意做成
# 约束强弱不同，使换前/换后待定缺陷数发生肉眼可见的变化：
#
# * 标准封装（甲片本应持有）只有跨工序定位点 C1，2 套可行方案，D102/D104 待定；
# * 强约束封装（乙片本应持有）在同一变换下多一枚基准同号定位点 L1（唯一变换，
#   全部缺陷确定），且 D105 故意落在与标准封装不同的格子，演示“编号和行列随
#   明细走、同号可不同位”。
#
# 传反状态本身两片都能各自对齐（同号定位点仍能重合）；对调归位后，当前持有
# 强约束明细的甲片待定 0 -> 2，当前持有标准明细的乙片待定 2 -> 0，两片仍都有
# 可行方案。
# ---------------------------------------------------------------------------

PACK_TRANSFORM: Transform = (False, 3)  # 顺时针270°，与 DEMO_STEPS 中的封装一致
# L1 基准坐标 (2,7) 在该变换下的工序图坐标；C1 两片一致（全局 (4,4)，局部 (4,5)）。
PACK_B_L1_LOCAL = invert_transform(PACK_TRANSFORM, *BASELINE_LOCATORS["L1"], GRID)
# 乙片 D105 改到全局 (2,2)，与甲片 D105 的 (1,1) 不同，同号不同位、随明细走。
PACK_B_D105_LOCAL = invert_transform(PACK_TRANSFORM, 2, 2, GRID)


def _pack_csv_b() -> str:
    reader = csv.reader(io.StringIO(render_csv(DEMO_STEPS[-1])))
    rows = list(reader)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(rows[0])
    for row in rows[1:]:
        if row[1] == "D105":
            writer.writerow(["缺陷", "D105", *PACK_B_D105_LOCAL])
        else:
            writer.writerow(row)
    writer.writerow(["定位点", "L1", *PACK_B_L1_LOCAL])
    return buf.getvalue()


def swapped_demo_pair() -> list[tuple[str, list[tuple[str, str]]]]:
    """返回两片“封装 CSV 传反”的同规格晶圆：[(晶圆名, [(工序名, CSV), ...]), ...]。"""
    standard = demo_samples()
    pack_a = dict(standard)["封装"]
    pack_b = _pack_csv_b()

    def build(pack_csv: str) -> list[tuple[str, str]]:
        return [(name, pack_csv if name == "封装" else text) for name, text in standard]

    return [
        # 甲片自己的封装应是标准明细（2 个待定），但上传时传成了乙片的强约束明细
        # （当前 0 个待定）；乙片反之。对调归位后甲片待定 0 -> 2、乙片待定 2 -> 0。
        ("演示晶圆 甲 LOT-2407-A（封装传反）", build(pack_b)),
        ("演示晶圆 乙 LOT-2407-B（封装传反）", build(pack_a)),
    ]

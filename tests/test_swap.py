"""两片晶圆同名工序明细对调：试算、确认、修订号失效与回滚。"""
from __future__ import annotations

import pytest

from wafer_review.db import connect, init_db
from wafer_review.model import (
    NoSolutionError,
    ValidationError,
    analyze,
)
from wafer_review.service import (
    StepInput,
    add_locator,
    correct_locator,
    create_wafer,
    load_wafer,
    remove_locator,
    swap_step_details,
)
from wafer_review.swap import StaleSnapshotError, plan_swap


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "swap.sqlite3"
    init_db(path)
    return path


def _csv(locators=(), defects=()):
    lines = ["类型,编号,行,列"]
    for pid, (row, col) in locators:
        lines.append(f"定位点,{pid},{row},{col}")
    for did, (row, col) in defects:
        lines.append(f"缺陷,{did},{row},{col}")
    return "\n".join(lines) + "\n"


# 两片同规格晶圆：基准 + 两道非基准工序。定位点足以让各工序对齐。
BASE_A = _csv(
    locators=[("L1", (1, 1)), ("L2", (1, 3))],
    defects=[("D1", (2, 2)), ("D2", (3, 3))],
)
# 第二道工序：顺时针90°把 L1(1,1)->(1,8)、L2(1,3)->(3,8)；两片的该工序不同。
STEP2_A = _csv(
    locators=[("L1", (8, 1)), ("L2", (8, 3))],
    defects=[("D9", (4, 4))],
)
STEP2_B = _csv(
    locators=[("L1", (8, 1)), ("L2", (8, 3)), ("X9", (2, 2))],
    defects=[("D9", (5, 5)), ("D8", (6, 6))],
)
STEP3 = _csv(locators=[("L1", (8, 8)), ("L2", (6, 8))], defects=[("D7", (1, 2))])


def _pair(db, name_a="W-A", name_b="W-B"):
    steps_a = [StepInput("光刻", BASE_A), StepInput("刻蚀", STEP2_A), StepInput("沉积", STEP3)]
    steps_b = [StepInput("光刻", BASE_A), StepInput("刻蚀", STEP2_B), StepInput("沉积", STEP3)]
    id_a = create_wafer(db, name_a, 8, steps_a)
    id_b = create_wafer(db, name_b, 8, steps_b)
    return id_a, id_b


def _pending(wafer):
    analysis = analyze(wafer)
    return sum(
        1 for s in wafer.steps for d in s.defects if analysis.result(s, d).status == "pending"
    )


def test_plan_swap_is_read_only_and_reports_pending(db):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    rev_a_before, rev_b_before = wa.revision, wb.revision
    trial = plan_swap(wa, wb, "刻蚀")
    assert trial.step_seq == 2
    t_a, t_b = trial.wafers
    assert (t_a.pending_before, t_b.pending_before) == (0, 0)
    # 试算不写库：修订号与明细原样
    with connect(db) as conn:
        wa2, wb2 = load_wafer(conn, id_a), load_wafer(conn, id_b)
    assert (wa2.revision, wb2.revision) == (rev_a_before, rev_b_before)
    assert [d.did for d in wa2.steps[1].defects] == ["D9"]
    assert [l.pid for l in wa2.steps[1].locators] == ["L1", "L2"]


def test_commit_swaps_full_details_and_bumps_both_revisions(db):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    trial = plan_swap(wa, wb, "刻蚀")
    revs = {w.wafer_id: w.revision for w in trial.wafers}
    swap_step_details(db, id_a, id_b, "刻蚀", revs)

    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    # 编号和行列随明细走：甲片拿到乙片的整份刻蚀明细
    assert [(l.pid, l.row, l.col) for l in wa.steps[1].locators] == [
        ("L1", 8, 1), ("L2", 8, 3), ("X9", 2, 2)
    ]
    assert [(d.did, d.row, d.col) for d in wa.steps[1].defects] == [
        ("D8", 6, 6), ("D9", 5, 5)
    ]
    # 乙片拿到甲片的明细（同号 D9 但行列不同，随明细走）
    assert [(d.did, d.row, d.col) for d in wb.steps[1].defects] == [("D9", 4, 4)]
    assert [l.pid for l in wb.steps[1].locators] == ["L1", "L2"]
    # 两片修订号各 +1，且仍各自可对齐
    assert (wa.revision, wb.revision) == (1, 1)
    assert analyze(wa).solvable and analyze(wb).solvable
    # 其他工序未动
    assert [d.did for d in wa.steps[2].defects] == ["D7"]


def test_baseline_step_cannot_be_swapped(db):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    with pytest.raises(ValidationError, match="基准"):
        plan_swap(wa, wb, "光刻")


def test_same_wafer_and_different_spec_rejected(db):
    id_a, _id_b = _pair(db)
    with connect(db) as conn:
        wa = load_wafer(conn, id_a)
    with pytest.raises(ValidationError, match="两片不同"):
        plan_swap(wa, wa, "刻蚀")

    # 不同网格尺寸 = 不同规格
    id_c = create_wafer(
        db,
        "W-C",
        6,
        [StepInput("光刻", _csv(locators=[("L1", (1, 1))], defects=[("D1", (2, 2))]))],
    )
    with connect(db) as conn:
        wc = load_wafer(conn, id_c)
    with pytest.raises(ValidationError, match="网格规格"):
        plan_swap(wa, wc, "刻蚀")


def test_step_name_mismatch_rejected(db):
    id_a, _ = _pair(db)
    other = create_wafer(
        db,
        "W-D",
        8,
        [
            StepInput("光刻", BASE_A),
            StepInput("蚀刻", STEP2_A),  # 名称不同
            StepInput("沉积", STEP3),
        ],
    )
    with connect(db) as conn:
        wa, wd = load_wafer(conn, id_a), load_wafer(conn, other)
    with pytest.raises(ValidationError, match="序号或名称不一致"):
        plan_swap(wa, wd, "刻蚀")


def test_incoming_detail_without_shared_locator_rejected(db):
    # 乙片刻蚀的定位点编号在甲片其他工序里完全不存在 -> 换入后无同号定位点
    lonely = _csv(locators=[("Z1", (8, 1)), ("Z2", (8, 3))], defects=[("D9", (4, 4))])
    id_a = create_wafer(
        db, "W-A", 8,
        [StepInput("光刻", BASE_A), StepInput("刻蚀", STEP2_A), StepInput("沉积", STEP3)],
    )
    id_b = create_wafer(
        db, "W-B", 8,
        [StepInput("光刻", BASE_A), StepInput("刻蚀", lonely), StepInput("沉积", STEP3)],
    )
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    with pytest.raises(NoSolutionError, match="没有任何同号定位点"):
        plan_swap(wa, wb, "刻蚀")
    # 未通过试算，数据库完全未动
    with connect(db) as conn:
        assert load_wafer(conn, id_a).revision == 0
        assert load_wafer(conn, id_b).revision == 0


def test_stale_revision_after_locator_edit_blocks_commit(db):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    trial = plan_swap(wa, wb, "刻蚀")
    revs = {w.wafer_id: w.revision for w in trial.wafers}

    # 试算取数后，甲片成功改点（坐标相同也会计一次修订）
    loc = wa.steps[2].locators[0]
    correct_locator(db, id_a, loc.id, loc.row, loc.col)

    with pytest.raises(StaleSnapshotError):
        swap_step_details(db, id_a, id_b, "刻蚀", revs)
    # 甲片修订停在改点的 1，对调没有发生
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    assert wa.revision == 1 and wb.revision == 0
    assert [d.did for d in wa.steps[1].defects] == ["D9"]


def test_stale_after_remove_and_add_also_blocks(db):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    trial = plan_swap(wa, wb, "刻蚀")
    revs = {w.wafer_id: w.revision for w in trial.wafers}
    loc = wa.steps[1].locators[0]
    remove_locator(db, id_a, loc.id)
    with pytest.raises(StaleSnapshotError):
        swap_step_details(db, id_a, id_b, "刻蚀", revs)

    # 补点恢复后重新试算，确认成功
    add_locator(db, id_a, wa.steps[1].id, loc.pid, loc.row, loc.col)
    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    trial2 = plan_swap(wa, wb, "刻蚀")
    revs2 = {w.wafer_id: w.revision for w in trial2.wafers}
    swap_step_details(db, id_a, id_b, "刻蚀", revs2)
    with connect(db) as conn:
        wa = load_wafer(conn, id_a)
    assert [d.did for d in wa.steps[1].defects] == ["D8", "D9"]


def test_swap_is_atomic_across_both_wafers(db, monkeypatch):
    id_a, id_b = _pair(db)
    with connect(db) as conn:
        wa_before = load_wafer(conn, id_a)
        wb_before = load_wafer(conn, id_b)
    snapshot_a = [(l.pid, l.row, l.col) for l in wa_before.steps[1].locators]
    snapshot_b = [(l.pid, l.row, l.col) for l in wb_before.steps[1].locators]

    # 让第二片的提交后可行性校验失败，模拟“保存/校验失败”：单片异常必须连带回滚另一片。
    import wafer_review.service as svc

    real = svc.compute_feasibility

    def flaky(wafer):
        # 只在 service 层“写入后重算”阶段失败；试算（plan_swap 内部）不经过这个名字。
        class _Fail:
            solvable = False
        if wafer.id == id_b:
            return _Fail()
        return real(wafer)

    monkeypatch.setattr(svc, "compute_feasibility", flaky)
    with pytest.raises(NoSolutionError):
        swap_step_details(db, id_a, id_b, "刻蚀", {id_a: 0, id_b: 0})

    with connect(db) as conn:
        wa, wb = load_wafer(conn, id_a), load_wafer(conn, id_b)
    assert [(l.pid, l.row, l.col) for l in wa.steps[1].locators] == snapshot_a
    assert [(l.pid, l.row, l.col) for l in wb.steps[1].locators] == snapshot_b
    assert wa.revision == 0 and wb.revision == 0


def test_missing_wafer_raises(db):
    id_a, id_b = _pair(db)
    with pytest.raises(ValidationError):
        swap_step_details(db, id_a, 9999, "刻蚀", {id_a: 0, 9999: 0})

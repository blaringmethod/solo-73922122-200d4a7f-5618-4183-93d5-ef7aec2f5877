"""HTTP 层：对调试算页、试算、确认、修订号失效与演示数据。"""
from __future__ import annotations

import re
import threading

import pytest

from wafer_review.app import create_app
from wafer_review.db import connect
from wafer_review.model import analyze
from wafer_review.service import correct_locator, load_wafer


@pytest.fixture()
def app(tmp_path):
    return create_app({"DATABASE": str(tmp_path / "app.sqlite3"), "TESTING": True})


@pytest.fixture()
def client(app):
    return app.test_client()


def _create_demo_pair(client):
    resp = client.post("/wafers/demo-swap-pair", follow_redirects=False)
    assert resp.status_code in (302, 303)
    location = resp.headers["Location"]
    match = re.search(r"left_id=(\d+).*right_id=(\d+)", location)
    assert match is not None
    return int(match.group(1)), int(match.group(2)), location


def test_demo_pair_route_preselects_swap_form(client):
    left_id, right_id, location = _create_demo_pair(client)
    page = client.get(location).get_data(as_text=True)
    assert "同名工序明细对调" in page
    assert "封装" in page
    # 两片同规格、同修订号
    with connect(client.application.config["DATABASE"]) as conn:
        wa = load_wafer(conn, left_id)
        wb = load_wafer(conn, right_id)
    assert wa.grid_size == wb.grid_size == 8
    assert [s.name for s in wa.steps] == [s.name for s in wb.steps]


def test_trial_page_shows_before_and_after_pending(client):
    left_id, right_id, _ = _create_demo_pair(client)
    resp = client.post(
        "/wafers/swaps/trial",
        data={"left_id": left_id, "right_id": right_id, "step_name": "封装"},
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "试算结果" in body
    # 甲片（当前持有强约束明细）待定 0 -> 2；乙片（持有标准明细）2 -> 0
    assert "0 → 2" in body
    assert "2 → 0" in body
    assert "确认对调" in body
    # 试算只读：修订号仍为 0
    with connect(client.application.config["DATABASE"]) as conn:
        assert load_wafer(conn, left_id).revision == 0
        assert load_wafer(conn, right_id).revision == 0


def test_trial_api_returns_structured_trial(client):
    left_id, right_id, _ = _create_demo_pair(client)
    resp = client.post(
        "/api/wafers/swaps/trial",
        json={"left_id": left_id, "right_id": right_id, "step_name": "封装"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["step_name"] == "封装"
    by_id = {w["wafer_id"]: w for w in data["wafers"]}
    assert (by_id[left_id]["pending_before"], by_id[left_id]["pending_after"]) == (0, 2)
    assert (by_id[right_id]["pending_before"], by_id[right_id]["pending_after"]) == (2, 0)
    # 换入明细均有跨工序同号定位点
    assert all(pids for pids in data["shared_locators"])
    swapped_rows = [
        s for w in data["wafers"] for s in w["steps"] if s["swapped"]
    ]
    assert len(swapped_rows) == 2


def test_commit_persists_swap_and_updates_revisions(client, app):
    left_id, right_id, _ = _create_demo_pair(client)
    resp = client.post(
        "/wafers/swaps/commit",
        data={
            "left_id": left_id,
            "right_id": right_id,
            "step_name": "封装",
            "rev_left": "0",
            "rev_right": "0",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    page = client.get(resp.headers["Location"], follow_redirects=True).get_data(as_text=True)
    assert "对调成功" in page
    assert "待定缺陷 0 → 2" in page  # 甲片（left）先展示
    assert "待定缺陷 2 → 0" in page  # 乙片

    with connect(app.config["DATABASE"]) as conn:
        wa = load_wafer(conn, left_id)
        wb = load_wafer(conn, right_id)
    assert (wa.revision, wb.revision) == (1, 1)
    # 甲片换入标准封装：只剩 C1，D102/D104 回到待定（2 个）
    assert [l.pid for l in wa.steps[5].locators] == ["C1"]
    aa = analyze(wa)
    assert aa.feasibility.scheme_count == 2
    pending_a = [
        d.did for d in wa.steps[5].defects if aa.result(wa.steps[5], d).status == "pending"
    ]
    assert pending_a == ["D102", "D104"]
    # 乙片换入强约束封装：多了 L1，全部缺陷确定
    assert sorted(l.pid for l in wb.steps[5].locators) == ["C1", "L1"]
    ab = analyze(wb)
    assert ab.feasibility.scheme_count == 1
    assert all(
        ab.result(s, d).status != "pending" for s in wb.steps for d in s.defects
    )
    # 同号 D105 行列随明细走：两片来源坐标不同
    d105_a = next(d for d in wa.steps[5].defects if d.did == "D105")
    d105_b = next(d for d in wb.steps[5].defects if d.did == "D105")
    assert (d105_a.row, d105_a.col) != (d105_b.row, d105_b.col)


def test_stale_commit_is_rejected_and_state_kept(client, app):
    left_id, right_id, _ = _create_demo_pair(client)
    # 试算
    client.post(
        "/wafers/swaps/trial",
        data={"left_id": left_id, "right_id": right_id, "step_name": "封装"},
    )
    # 试算期间甲片成功改点（同坐标也会计修订）
    with connect(app.config["DATABASE"]) as conn:
        wafer = load_wafer(conn, left_id)
    loc = wafer.steps[2].locators[0]
    correct_locator(app.config["DATABASE"], left_id, loc.id, loc.row, loc.col)

    resp = client.post(
        "/wafers/swaps/commit",
        data={
            "left_id": left_id,
            "right_id": right_id,
            "step_name": "封装",
            "rev_left": "0",
            "rev_right": "0",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "重新试算" in page

    with connect(app.config["DATABASE"]) as conn:
        wa = load_wafer(conn, left_id)
        wb = load_wafer(conn, right_id)
    # 对调没有发生，修订号只反映那次改点
    assert wa.revision == 1 and wb.revision == 0
    # 传反状态原样保留：甲片仍是强约束封装（C1+L1），乙片仍只有 C1
    assert sorted(l.pid for l in wa.steps[5].locators) == ["C1", "L1"]
    assert [l.pid for l in wb.steps[5].locators] == ["C1"]


def test_baseline_and_self_selections_rejected(client):
    left_id, right_id, _ = _create_demo_pair(client)
    resp = client.post(
        "/api/wafers/swaps/trial",
        json={"left_id": left_id, "right_id": right_id, "step_name": "光刻"},
    )
    assert resp.status_code == 409
    assert "基准" in resp.get_json()["error"]

    resp = client.post(
        "/api/wafers/swaps/trial",
        json={"left_id": left_id, "right_id": left_id, "step_name": "封装"},
    )
    assert resp.status_code == 409
    assert "两片不同" in resp.get_json()["error"]

    resp = client.post(
        "/api/wafers/swaps/trial",
        json={"left_id": left_id, "right_id": 9999, "step_name": "封装"},
    )
    assert resp.status_code == 404


def test_form_trial_validation_errors_render_flash(client):
    left_id, right_id, _ = _create_demo_pair(client)
    resp = client.post(
        "/wafers/swaps/trial",
        data={"left_id": left_id, "right_id": right_id, "step_name": "光刻"},
    )
    body = resp.get_data(as_text=True)
    assert "试算未通过" in body and "基准" in body

    resp = client.post(
        "/wafers/swaps/trial",
        data={"left_id": "x", "right_id": right_id, "step_name": "封装"},
    )
    assert "试算未通过" in resp.get_data(as_text=True)


def test_swap_form_linked_from_index_and_detail(client):
    left_id, right_id, _ = _create_demo_pair(client)
    body = client.get("/").get_data(as_text=True)
    assert "两片晶圆对调工序" in body
    assert "一键建立对调演示" in body
    detail = client.get(f"/wafers/{left_id}").get_data(as_text=True)
    assert f"/wafers/swaps?left_id={left_id}" in detail


def test_concurrent_commits_only_one_persists(app, client):
    # 多个请求拿着同一试算修订号并发确认：只允许一个落库，其余必须要求重新试算。
    left_id, right_id, _ = _create_demo_pair(client)
    outcomes: list[str] = []
    lock = threading.Lock()

    def commit():
        local = app.test_client()
        resp = local.post(
            "/wafers/swaps/commit",
            data={
                "left_id": left_id,
                "right_id": right_id,
                "step_name": "封装",
                "rev_left": "0",
                "rev_right": "0",
            },
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        with lock:
            outcomes.append(
                "ok" if "对调成功" in body
                else "stale" if "重新试算" in body
                else "other"
            )

    threads = [threading.Thread(target=commit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("stale") == 3
    with connect(app.config["DATABASE"]) as conn:
        wa = load_wafer(conn, left_id)
        wb = load_wafer(conn, right_id)
    assert (wa.revision, wb.revision) == (1, 1)

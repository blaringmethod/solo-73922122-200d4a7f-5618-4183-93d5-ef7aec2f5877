"""发布前自检：脚本化走一遍关键操作，并核对页面/接口输出。

运行方式（容器内或本机均可）：

    python scripts/selftest.py                                  # 用临时数据库
    BASE_URL=http://127.0.0.1:8000 python scripts/selftest.py   # 打真实服务

BASE_URL 模式需要服务端已有演示数据或允许创建；脚本会新建一张名为 SELF-TEST 的晶圆，
并使用返回的 id（通过 ``/api/wafers`` 查询）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Browser:
    """极简 HTTP 客户端；本地模式走 Flask test_client，不启真实端口。"""

    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url
        self.client = None

    def _send(self, method: str, path: str, body: bytes | None, headers: dict) -> tuple[int, str, dict]:
        if self.base_url:
            url = self.base_url + path
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            # 不自动跟随重定向，才能拿到上传/改点后的 Location
            opener = urllib.request.build_opener(_NoRedirect)
            try:
                with opener.open(req, timeout=20) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
        assert self.client is not None
        resp = self.client.open(
            path,
            method=method,
            data=body,
            headers=headers,
            follow_redirects=False,
        )
        return resp.status_code, resp.get_data(as_text=True), dict(resp.headers)

    def get(self, path: str) -> tuple[int, str]:
        status, body, _ = self._send("GET", path, None, {})
        return status, body

    def post_form(self, path: str, fields: dict[str, str]) -> tuple[int, str]:
        body = urllib.parse.urlencode(fields).encode()
        status, text, _ = self._send(
            "POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"}
        )
        return status, text

    def post_files(
        self, path: str, fields: dict[str, str], files: list[tuple[str, str, bytes]]
    ) -> tuple[int, str, dict]:
        boundary = "----selftest" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        for key, filename, content in files:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
            )
            chunks.append(content + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        status, text, headers = self._send(
            "POST",
            path,
            b"".join(chunks),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return status, text, headers


def build_browser(base_url: str | None) -> Browser:
    browser = Browser(base_url)
    if base_url is None:
        from wafer_review.app import create_app

        tmp = tempfile.mkdtemp(prefix="wafer-selftest-")
        app = create_app({"DATABASE": str(Path(tmp) / "selftest.sqlite3"), "TESTING": True})
        browser.client = app.test_client()
    return browser


def demo_csvs() -> list[tuple[str, str, bytes]]:
    from wafer_review.demo import demo_samples

    return [
        (f"csv_{i}", f"{i}_{name}.csv", text.encode("utf-8"))
        for i, (name, text) in enumerate(demo_samples(), start=1)
    ]


def demo_names() -> list[str]:
    from wafer_review.demo import demo_samples

    return [name for name, _ in demo_samples()]


def api(browser: Browser, path: str):
    status, body = browser.get(path)
    if status != 200:
        raise RuntimeError(f"{path} -> HTTP {status}")
    return json.loads(body)


def main() -> int:
    base_url = os.environ.get("BASE_URL", "").rstrip("/") or None
    browser = build_browser(base_url)

    status, body = browser.get("/")
    check("首页可访问", status == 200, f"HTTP {status}")
    check("首页有新建入口", "新建复查" in body)

    fields = {"name": "SELF-TEST", "grid_size": "8"}
    for i, name in enumerate(demo_names(), start=1):
        fields[f"name_{i}"] = name
    status, _text, headers = browser.post_files("/wafers", fields, demo_csvs())
    location = headers.get("Location", "")
    match = re.search(r"/wafers/(\d+)$", location)
    check("上传 6 道工序成功", status in (302, 303) and match is not None, f"HTTP {status} {location}")
    if match is None:
        print("\n无法定位新晶圆，终止自检")
        return 1
    wafer_id = int(match.group(1))
    check("上传后跳到详情页", location.endswith(f"/wafers/{wafer_id}"), location)

    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    check("可行方案为 2 套", data["scheme_count"] == 2, str(data["scheme_count"]))
    per_step: dict[str, dict[str, dict]] = {}
    for row in data["results"]:
        per_step.setdefault(row["step"], {})[row["defect"]] = row

    expected = {"D101": [4, 4], "D103": [8, 8], "D105": [1, 1]}
    for step, defects in per_step.items():
        if step == "封装":
            continue
        ok = all(defects[d]["coordinate"] == cell for d, cell in expected.items())
        check(f"{step} 缺陷坐标与基准一致", ok)

    pack = per_step["封装"]
    check("封装 D101 得基准坐标 (4,4)", pack["D101"]["coordinate"] == [4, 4])
    check("封装 D102 标为待定", pack["D102"]["status"] == "pending")
    check("封装 D104 标为待定", pack["D104"]["status"] == "pending")

    status, page = browser.get(f"/wafers/{wafer_id}?step=6&did=D102")
    check("页面展示待定", status == 200 and "待定" in page)

    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}

    # 1) 撤去沉积的 C1：封装失去跨工序约束，相关查询变为待定
    c1_dep = _find_locator(steps_by_seq[3], "C1")
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_dep['id']}/remove", {"step": "3", "did": "D101"}
    )
    check("撤点请求成功", status in (302, 303), f"HTTP {status}")
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    check("撤点后方案数变为 8", data["scheme_count"] == 8, str(data["scheme_count"]))
    pack = {r["defect"]: r for r in data["results"] if r["step"] == "封装"}
    check("撤点后封装 D101 变为待定", pack["D101"]["status"] == "pending")

    # 2) 补回 C1：恢复 2 套方案
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators",
        {
            "step_id": str(steps_by_seq[3]["id"]),
            "step": "3",
            "did": "D101",
            "pid": "C1",
            "row": "4",
            "col": "5",
        },
    )
    check("补点请求成功", status in (302, 303), f"HTTP {status}")
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    c1_pack = _find_locator(steps_by_seq[6], "C1")

    # 3) 更正封装 C1 -> (4,4)：D104 由待定变为确定 (5,5)
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_pack['id']}/correct",
        {"step": "6", "did": "D104", "row": "4", "col": "4"},
    )
    check("更正请求成功", status in (302, 303), f"HTTP {status}")
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    pack = {r["defect"]: r for r in data["results"] if r["step"] == "封装"}
    check("更正后 D104 变为确定 (5,5)", pack["D104"]["coordinate"] == [5, 5])

    # 4) 无解更正必须整体回滚，原定位点与原结果沿用
    before = {r["defect"]: (r["status"], r["coordinate"]) for r in data["results"] if r["step"] == "封装"}
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    c1_pack = _find_locator(steps_by_seq[6], "C1")
    status, _ = browser.post_form(
        f"/wafers/{wafer_id}/locators/{c1_pack['id']}/correct",
        {"step": "6", "did": "D104", "row": "1", "col": "1"},
    )
    data = api(browser, f"/api/wafers/{wafer_id}/defects")
    after = {r["defect"]: (r["status"], r["coordinate"]) for r in data["results"] if r["step"] == "封装"}
    check("无解更正后原查询结果不变", before == after)
    check("无解更正后仍为 2 套方案", data["scheme_count"] == 2, str(data["scheme_count"]))
    locators = api(browser, f"/api/wafers/{wafer_id}/locators")
    steps_by_seq = {s["seq"]: s for s in locators["steps"]}
    kept = _find_locator(steps_by_seq[6], "C1")
    check(
        "无解更正后定位点仍为原坐标 (4,4)",
        [kept["row"], kept["col"]] == [4, 4],
        f'{kept["row"]},{kept["col"]}',
    )

    # 5) 两片晶圆同名工序明细对调：试算、修订号失效、确认、回滚校验
    run_swap_checks(browser)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    return 1 if failed else 0


def run_swap_checks(browser: Browser) -> None:
    # 5.1 一键建立两片“封装传反”的同规格晶圆
    status, _text, headers = browser._send(
        "POST", "/wafers/demo-swap-pair", b"",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    location = headers.get("Location", "")
    match = re.search(r"left_id=(\d+)&right_id=(\d+)", location)
    check("对调演示两片晶圆建立成功", status in (302, 303) and match is not None, f"HTTP {status} {location}")
    if match is None:
        return
    left_id, right_id = int(match.group(1)), int(match.group(2))

    status, page = browser.get(location)
    check("对调试算页可访问并预选工序", status == 200 and "封装" in page)

    # 5.2 只读试算：显示两片换前/换后待定缺陷数
    trial = api_post(browser, "/api/wafers/swaps/trial",
                     {"left_id": left_id, "right_id": right_id, "step_name": "封装"})
    by_id = {w["wafer_id"]: w for w in trial["wafers"]}
    check(
        "试算给出两片换前/换后待定数",
        (by_id[left_id]["pending_before"], by_id[left_id]["pending_after"]) == (0, 2)
        and (by_id[right_id]["pending_before"], by_id[right_id]["pending_after"]) == (2, 0),
        str([(w["wafer_id"], w["pending_before"], w["pending_after"]) for w in trial["wafers"]]),
    )
    check("换入明细都有同号定位点", all(trial["shared_locators"]))
    # 试算不写库：修订号仍为 0
    locs = api(browser, f"/api/wafers/{left_id}/locators")
    check("试算期间修订号不变", locs["revision"] == 0, str(locs["revision"]))

    # 5.3 试算期间照常改点；随后用旧修订号确认必须被拒绝
    locators = api(browser, f"/api/wafers/{left_id}/locators")
    c1 = _find_locator(next(s for s in locators["steps"] if s["name"] == "沉积"), "C1")
    status, _ = browser.post_form(
        f"/wafers/{left_id}/locators/{c1['id']}/correct",
        {"step": "3", "row": str(c1["row"]), "col": str(c1["col"])},
    )
    check("试算期间改点照常成功", status in (302, 303), f"HTTP {status}")
    status, _ = browser.post_form(
        "/wafers/swaps/commit",
        {
            "left_id": str(left_id), "right_id": str(right_id), "step_name": "封装",
            "rev_left": "0", "rev_right": "0",
        },
    )
    check("旧修订号确认被拒绝（302 回试算页）", status in (302, 303), f"HTTP {status}")
    locs = api(browser, f"/api/wafers/{left_id}/locators")
    check("拒绝后对调未发生（仍为强约束封装 C1+L1）",
          sorted(l["pid"] for l in locs["steps"][5]["locators"]) == ["C1", "L1"])

    # 5.4 重新试算，取新修订号后确认成功
    trial = api_post(browser, "/api/wafers/swaps/trial",
                     {"left_id": left_id, "right_id": right_id, "step_name": "封装"})
    revs = {w["wafer_id"]: w["revision"] for w in trial["wafers"]}
    status, _ = browser.post_form(
        "/wafers/swaps/commit",
        {
            "left_id": str(left_id), "right_id": str(right_id), "step_name": "封装",
            "rev_left": str(revs[left_id]), "rev_right": str(revs[right_id]),
        },
    )
    check("确认对调成功", status in (302, 303), f"HTTP {status}")

    data_l = api(browser, f"/api/wafers/{left_id}/defects")
    data_r = api(browser, f"/api/wafers/{right_id}/defects")
    pack_l = {r["defect"]: r for r in data_l["results"] if r["step"] == "封装"}
    pack_r = {r["defect"]: r for r in data_r["results"] if r["step"] == "封装"}
    check("对调后甲片封装 D102/D104 待定",
          pack_l["D102"]["status"] == "pending" and pack_l["D104"]["status"] == "pending")
    check("对调后乙片封装全部确定",
          all(r["status"] == "determined" for r in pack_r.values()))
    check("对调后两片仍可对齐（方案数 2 / 1）",
          data_l["scheme_count"] == 2 and data_r["scheme_count"] == 1,
          f'{data_l["scheme_count"]} / {data_r["scheme_count"]}')
    check("同号 D105 行列随明细走（两片来源不同）",
          pack_l["D105"]["source"] != pack_r["D105"]["source"],
          f'{pack_l["D105"]["source"]} vs {pack_r["D105"]["source"]}')
    locs_l = api(browser, f"/api/wafers/{left_id}/locators")
    locs_r = api(browser, f"/api/wafers/{right_id}/locators")
    check("两片修订号都已推进",
          locs_l["revision"] >= 2 and locs_r["revision"] >= 1,
          f'{locs_l["revision"]} / {locs_r["revision"]}')

    # 5.5 反向再对调一次，数据应回到传反前的状态
    trial = api_post(browser, "/api/wafers/swaps/trial",
                     {"left_id": left_id, "right_id": right_id, "step_name": "封装"})
    revs = {w["wafer_id"]: w["revision"] for w in trial["wafers"]}
    browser.post_form(
        "/wafers/swaps/commit",
        {
            "left_id": str(left_id), "right_id": str(right_id), "step_name": "封装",
            "rev_left": str(revs[left_id]), "rev_right": str(revs[right_id]),
        },
    )
    data_l2 = api(browser, f"/api/wafers/{left_id}/defects")
    pack_l2 = {r["defect"]: r for r in data_l2["results"] if r["step"] == "封装"}
    check("再对调一次恢复原状（甲片封装全部确定）",
          all(r["status"] == "determined" for r in pack_l2.values()))

    # 5.6 非法选择：基准工序、同片、不存在的晶圆
    bad = api_post(browser, "/api/wafers/swaps/trial",
                   {"left_id": left_id, "right_id": right_id, "step_name": "光刻"}, expect_error=True)
    check("基准工序拒绝对调", bad is not None and "基准" in str(bad))
    bad = api_post(browser, "/api/wafers/swaps/trial",
                   {"left_id": left_id, "right_id": left_id, "step_name": "封装"}, expect_error=True)
    check("同片对调被拒绝", bad is not None and "不同" in str(bad))


def api_post(browser: Browser, path: str, payload: dict, expect_error: bool = False):
    body = json.dumps(payload).encode()
    status, text = browser._send("POST", path, body, {"Content-Type": "application/json"})[:2]
    data = json.loads(text)
    if expect_error:
        return data if status >= 400 else None
    if status != 200:
        raise RuntimeError(f"{path} -> HTTP {status}: {text}")
    return data


def _find_locator(step: dict, pid: str) -> dict:
    for loc in step["locators"]:
        if loc["pid"] == pid:
            return loc
    raise RuntimeError(f"工序 {step['name']} 没有定位点 {pid}")


if __name__ == "__main__":
    sys.exit(main())

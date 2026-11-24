"""Flask 应用工厂与路由。"""
from __future__ import annotations

import os
import sqlite3

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .db import connect, init_db
from .display import mapped_grid, transform_label
from .gridview import build_grid
from .model import NoSolutionError, ValidationError, WaferError, analyze
from .overlay import compare_grid
from .service import (
    MAX_STEPS,
    StepInput,
    add_locator,
    correct_locator,
    create_wafer,
    delete_wafer,
    list_wafers,
    load_wafer,
    remove_locator,
    swap_step_details,
)
from .swap import StaleSnapshotError, plan_swap

DEFAULT_DB = "data/wafer_review.sqlite3"


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("WAFER_SECRET", "dev-secret-change-me"),
        DATABASE=os.environ.get("WAFER_DB", DEFAULT_DB),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        MAX_STEPS=MAX_STEPS,
    )
    if test_config:
        app.config.update(test_config)

    init_db(app.config["DATABASE"])

    @app.context_processor
    def _inject() -> dict:
        return {"max_steps": app.config["MAX_STEPS"]}

    @app.get("/")
    def index():
        with connect(app.config["DATABASE"]) as conn:
            wafers = list_wafers(conn)
        summaries = []
        for wafer in wafers:
            analysis = analyze(wafer)
            summaries.append(
                {
                    "wafer": wafer,
                    "solvable": analysis.solvable,
                    "scheme_count": analysis.feasibility.scheme_count,
                    "defect_total": sum(len(s.defects) for s in wafer.steps),
                }
            )
        return render_template("index.html", summaries=summaries)

    @app.get("/wafers/new")
    def new_wafer():
        return render_template("new.html", step_count=3)

    @app.post("/wafers/demo")
    def create_demo():
        from .demo import demo_samples

        steps = [StepInput(name, text) for name, text in demo_samples()]
        try:
            wafer_id = create_wafer(app.config["DATABASE"], "演示晶圆 LOT-2407-A", 8, steps)
        except (WaferError, OSError, sqlite3.Error) as exc:
            flash(f"演示数据建立失败：{exc}", "error")
            return redirect(url_for("index"))
        flash("演示晶圆已建立：6 道工序、8×8 网格", "ok")
        return redirect(url_for("wafer_detail", wafer_id=wafer_id))

    @app.post("/wafers/demo-swap-pair")
    def create_demo_swap_pair():
        """一次建立两片“封装 CSV 传反”的同规格晶圆，随后跳到对调试算页。"""
        from .demo import swapped_demo_pair

        ids: list[int] = []
        try:
            for name, samples in swapped_demo_pair():
                steps = [StepInput(step_name, text) for step_name, text in samples]
                ids.append(create_wafer(app.config["DATABASE"], name, 8, steps))
        except (WaferError, OSError, sqlite3.Error) as exc:
            for wafer_id in ids:
                delete_wafer(app.config["DATABASE"], wafer_id)
            flash(f"对调演示数据建立失败：{exc}", "error")
            return redirect(url_for("index"))
        flash("已建立两片同规格晶圆（封装工序传反），请选择工序并试算对调", "ok")
        return redirect(
            url_for("swap_form", left_id=ids[0], right_id=ids[1], step="封装")
        )

    @app.get("/samples/<name>.csv")
    def sample_csv(name: str):
        from .demo import demo_samples

        for index, (step_name, text) in enumerate(demo_samples(), start=1):
            if name in (str(index), step_name, f"{index}_{step_name}"):
                return app.response_class(
                    text,
                    mimetype="text/csv; charset=utf-8",
                    headers={
                        "Content-Disposition": f'inline; filename="{index}_{step_name}.csv"'
                    },
                )
        abort(404)

    @app.post("/wafers")
    def create():
        name = request.form.get("name", "")
        grid_text = request.form.get("grid_size", "")
        try:
            grid_size = int(grid_text)
        except ValueError:
            flash("每边格数必须是整数", "error")
            return redirect(url_for("new_wafer"))
        if not 2 <= grid_size <= 60:
            flash("每边格数必须在 2 到 60 之间", "error")
            return redirect(url_for("new_wafer"))

        step_inputs: list[StepInput] = []
        for i in range(1, app.config["MAX_STEPS"] + 1):
            uploaded = request.files.get(f"csv_{i}")
            text = request.form.get(f"csv_text_{i}", "")
            step_name = request.form.get(f"name_{i}", "").strip()
            if uploaded is not None and uploaded.filename:
                try:
                    text = uploaded.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    flash(f"第 {i} 个 CSV 不是 UTF-8 编码", "error")
                    return redirect(url_for("new_wafer"))
            if text.strip():
                step_inputs.append(StepInput(step_name or f"工序{i}", text))

        if not step_inputs:
            flash("至少上传一道工序的 CSV", "error")
            return redirect(url_for("new_wafer"))

        try:
            wafer_id = create_wafer(app.config["DATABASE"], name, grid_size, step_inputs)
        except NoSolutionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_wafer"))
        except WaferError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_wafer"))
        except (OSError, sqlite3.Error) as exc:
            flash(f"保存失败：{exc}；原定位点和原查询结果未改动", "error")
            return redirect(url_for("new_wafer"))

        flash("晶圆已建立，定位点与查询结果已生效", "ok")
        return redirect(url_for("wafer_detail", wafer_id=wafer_id))

    @app.get("/wafers/<int:wafer_id>")
    def wafer_detail(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        step_id_text = request.args.get("step")
        did = request.args.get("did", "").strip()
        selected = None
        if step_id_text:
            try:
                selected = wafer.step_by_id(int(step_id_text))
            except ValueError:
                selected = None
        if selected is None and wafer.steps:
            selected = wafer.steps[0]

        results = analysis.step_results(selected) if selected else []
        focus = next((r for r in results if r.did == did), None) if did else None

        grid = build_grid(selected, wafer.grid_size) if selected else None
        mapped = None
        mapped_label = ""
        if selected:
            transforms = analysis.transforms(selected)
            if transforms:
                mapped = mapped_grid(selected, transforms[0], wafer.grid_size)
                mapped_label = transform_label(transforms[0])

        return render_template(
            "wafer.html",
            wafer=wafer,
            feasibility=analysis.feasibility,
            selected=selected,
            results=results,
            focus=focus,
            queried_did=did,
            grid=grid,
            mapped=mapped,
            mapped_label=mapped_label,
            overlay=compare_grid(analysis),
        )

    def _back(wafer_id: int, step: str, did: str):
        params = {"wafer_id": wafer_id}
        if step:
            params["step"] = step
        if did:
            params["did"] = did
        return redirect(url_for("wafer_detail", **params))

    def _apply(
        action,
        success: str,
        failure_prefix: str,
        wafer_id: int,
        step: str,
        did: str,
    ):
        try:
            action()
        except NoSolutionError as exc:
            flash(f"未生效：{exc}", "error")
        except (WaferError, OSError, sqlite3.Error) as exc:
            flash(f"{failure_prefix}：{exc}；已沿用原定位点和原结果", "error")
        else:
            flash(success, "ok")
        return _back(wafer_id, step, did)

    @app.post("/wafers/<int:wafer_id>/locators/<int:locator_id>/correct")
    def correct(wafer_id: int, locator_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        try:
            row = int(request.form.get("row", ""))
            col = int(request.form.get("col", ""))
        except ValueError:
            flash("未生效：行列必须是整数；已沿用原定位点和原结果", "error")
            return _back(wafer_id, step, did)
        return _apply(
            lambda: correct_locator(app.config["DATABASE"], wafer_id, locator_id, row, col),
            "定位点已更正，受影响的查询结果已一并更新",
            "更正失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/locators/<int:locator_id>/remove")
    def remove(wafer_id: int, locator_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        return _apply(
            lambda: remove_locator(app.config["DATABASE"], wafer_id, locator_id),
            "定位点已撤去，受影响的查询结果已一并更新",
            "撤点失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/locators")
    def add(wafer_id: int):
        step = request.form.get("step", "")
        did = request.form.get("did", "")
        try:
            step_id = int(request.form.get("step_id", ""))
            row = int(request.form.get("row", ""))
            col = int(request.form.get("col", ""))
        except ValueError:
            flash("未生效：行列必须是整数；已沿用原定位点和原结果", "error")
            return _back(wafer_id, step, did)
        pid = request.form.get("pid", "")
        return _apply(
            lambda: add_locator(app.config["DATABASE"], wafer_id, step_id, pid, row, col),
            "定位点已补充，受影响的查询结果已一并更新",
            "补点失败",
            wafer_id,
            step,
            did,
        )

    @app.post("/wafers/<int:wafer_id>/delete")
    def delete(wafer_id: int):
        delete_wafer(app.config["DATABASE"], wafer_id)
        flash("晶圆记录已删除", "ok")
        return redirect(url_for("index"))

    def _load_two(left_id: int, right_id: int):
        with connect(app.config["DATABASE"]) as conn:
            left = load_wafer(conn, left_id)
            right = load_wafer(conn, right_id)
        if left is None or right is None:
            abort(404)
        return left, right

    def _parse_swap_form():
        try:
            left_id = int(request.form.get("left_id", ""))
            right_id = int(request.form.get("right_id", ""))
        except ValueError:
            raise ValidationError("请选择两片晶圆")
        step_name = request.form.get("step_name", "").strip()
        if not step_name:
            raise ValidationError("请选择要对调的同名非基准工序")
        return left_id, right_id, step_name

    @app.get("/wafers/swaps")
    def swap_form():
        with connect(app.config["DATABASE"]) as conn:
            wafers = list_wafers(conn)
        preselect = {
            "left_id": request.args.get("left_id", ""),
            "right_id": request.args.get("right_id", ""),
            "step": request.args.get("step", ""),
        }
        # 同规格候选：网格与工序结构一致的晶圆两两一组提示给页面。
        return render_template("swap.html", wafers=wafers, preselect=preselect, trial=None)

    @app.post("/wafers/swaps/trial")
    def swap_trial():
        raw_left = request.form.get("left_id", "")
        raw_right = request.form.get("right_id", "")
        step_name = request.form.get("step_name", "").strip()
        trial = None
        try:
            left_id, right_id, step_name = _parse_swap_form()
            left, right = _load_two(left_id, right_id)
            trial = plan_swap(left, right, step_name)
        except NoSolutionError as exc:
            flash(f"试算未通过：{exc}", "error")
        except WaferError as exc:
            flash(f"试算未通过：{exc}", "error")
        with connect(app.config["DATABASE"]) as conn:
            wafers = list_wafers(conn)
        return render_template(
            "swap.html",
            wafers=wafers,
            preselect={"left_id": raw_left, "right_id": raw_right, "step": step_name},
            trial=trial,
        )

    @app.post("/wafers/swaps/commit")
    def swap_commit():
        left_id = request.form.get("left_id", "")
        right_id = request.form.get("right_id", "")
        step_name = request.form.get("step_name", "").strip()
        try:
            left_id, right_id, step_name = _parse_swap_form()
            try:
                rev_left = int(request.form.get("rev_left", ""))
                rev_right = int(request.form.get("rev_right", ""))
            except ValueError:
                raise ValidationError("缺少试算修订号，请重新试算")
            trial = swap_step_details(
                app.config["DATABASE"],
                left_id,
                right_id,
                step_name,
                {left_id: rev_left, right_id: rev_right},
            )
        except StaleSnapshotError as exc:
            flash(f"请重新试算：{exc}", "warn")
            return redirect(
                url_for(
                    "swap_form",
                    left_id=left_id,
                    right_id=right_id,
                    step=step_name,
                )
            )
        except NoSolutionError as exc:
            flash(f"对调未生效：{exc} 两片均保留对调前状态。", "error")
            return redirect(
                url_for(
                    "swap_form",
                    left_id=left_id,
                    right_id=right_id,
                    step=step_name,
                )
            )
        except (WaferError, OSError, sqlite3.Error) as exc:
            flash(f"对调失败：{exc}；两片均保留对调前状态。", "error")
            return redirect(url_for("swap_form"))
        a, b = trial.wafers
        flash(
            "对调成功："
            f"{a.name} 待定缺陷 {a.pending_before} → {a.pending_after}；"
            f"{b.name} 待定缺陷 {b.pending_before} → {b.pending_after}。"
            "两片工序明细、修订号与坐标查询已一并更新。",
            "ok",
        )
        return redirect(url_for("wafer_detail", wafer_id=left_id))

    @app.post("/api/wafers/swaps/trial")
    def api_swap_trial():
        payload = request.get_json(silent=True) or {}
        try:
            left_id = int(payload.get("left_id"))
            right_id = int(payload.get("right_id"))
        except (TypeError, ValueError):
            return {"error": "left_id / right_id 必须是整数"}, 400
        step_name = str(payload.get("step_name", "")).strip()
        if not step_name:
            return {"error": "缺少 step_name"}, 400
        try:
            left, right = _load_two(left_id, right_id)
            trial = plan_swap(left, right, step_name)
        except WaferError as exc:
            return {"error": str(exc)}, 409
        return {
            "step_name": trial.step_name,
            "step_seq": trial.step_seq,
            "wafers": [
                {
                    "wafer_id": w.wafer_id,
                    "name": w.name,
                    "revision": w.revision,
                    "pending_before": w.pending_before,
                    "pending_after": w.pending_after,
                    "schemes_before": w.schemes_before,
                    "schemes_after": w.schemes_after,
                    "steps": [
                        {
                            "seq": s.seq,
                            "name": s.name,
                            "swapped": s.swapped,
                            "pending_before": s.pending_before,
                            "pending_after": s.pending_after,
                        }
                        for s in w.steps
                    ],
                }
                for w in trial.wafers
            ],
            "shared_locators": [list(pids) for pids in trial.shared_locators],
        }

    @app.get("/api/wafers/<int:wafer_id>/defects")
    def api_defects(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        out = []
        for step in wafer.steps:
            for result in analysis.step_results(step):
                out.append(
                    {
                        "step": step.name,
                        "defect": result.did,
                        "source": [result.source_row, result.source_col],
                        "status": result.status,
                        "coordinate": list(result.coordinate) if result.coordinate else None,
                        "candidates": [list(c) for c in result.candidates],
                    }
                )
        return {
            "wafer": wafer.name,
            "grid_size": wafer.grid_size,
            "scheme_count": analysis.feasibility.scheme_count,
            "results": out,
        }

    @app.get("/healthz")
    def healthz():
        with connect(app.config["DATABASE"]) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/wafers/<int:wafer_id>/locators")
    def api_locators(wafer_id: int):
        with connect(app.config["DATABASE"]) as conn:
            wafer = load_wafer(conn, wafer_id)
        if wafer is None:
            abort(404)
        analysis = analyze(wafer)
        return {
            "wafer": wafer.name,
            "revision": wafer.revision,
            "scheme_count": analysis.feasibility.scheme_count,
            "steps": [
                {
                    "id": step.id,
                    "seq": step.seq,
                    "name": step.name,
                    "transforms": list(f.labels) if f else [],
                    "locators": [
                        {"id": loc.id, "pid": loc.pid, "row": loc.row, "col": loc.col}
                        for loc in step.locators
                    ],
                }
                for step, f in (
                    (step, analysis.feasibility.for_step(step.id)) for step in wafer.steps
                )
            ],
        }

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("404.html"), 404

    return app

"""作废闭环：作废原子释放占用、拒绝再执行、足迹同源、并发占坑锁写。"""

import sqlite3
import threading

import pytest

from app.db import begin_write, connect, init_db
from app.services.execute import cancel_judgment, execute_judgment
from app.services.scan import run_expire_scan


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "cancel.db"
    init_db(str(path))
    return str(path)


def _open_judgment(conn):
    job = run_expire_scan(conn, "2026-11-16")
    assert job["expired_count"] >= 1
    row = conn.execute(
        "SELECT * FROM judgments WHERE status = '待执行' ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row)


def test_canceled_judgment_cannot_execute_and_leaves_footprint(db_path):
    with connect(db_path) as conn:
        j = _open_judgment(conn)
        part_before = conn.execute(
            "SELECT storage_state FROM partitions WHERE id = ?", (j["partition_id"],)
        ).fetchone()["storage_state"]
        assert part_before == "在线"

        result = cancel_judgment(conn, j["id"])
        assert result["status"] == "已作废"

        # 作废状态落库
        status = conn.execute(
            "SELECT status FROM judgments WHERE id = ?", (j["id"],)
        ).fetchone()["status"]
        assert status == "已作废"

        # 再执行必须被拒绝（409 由路由层映射），且分区未被动过
        with pytest.raises(ValueError, match="已作废"):
            execute_judgment(conn, j["id"])
        part_after = conn.execute(
            "SELECT storage_state, archive_uri FROM partitions WHERE id = ?",
            (j["partition_id"],),
        ).fetchone()
        assert part_after["storage_state"] == "在线"
        assert part_after["archive_uri"] is None

        # 足迹与作废同源：execute_runs 有一条 result_state=已作废
        run = conn.execute(
            "SELECT * FROM execute_runs WHERE judgment_id = ?", (j["id"],)
        ).fetchone()
        assert run is not None
        assert dict(run)["result_state"] == "已作废"


def test_cancel_then_rescan_opens_new_judgment_not_double_retire(db_path):
    with connect(db_path) as conn:
        j = _open_judgment(conn)
        cancel_judgment(conn, j["id"])
        # 作废后重新扫描，可对同一分区重开一单
        run_expire_scan(conn, "2026-11-16")
        reopened = conn.execute(
            "SELECT COUNT(*) AS c FROM judgments WHERE partition_id = ? AND status='待执行'",
            (j["partition_id"],),
        ).fetchone()["c"]
        assert reopened == 1
        # 原作废单依旧不可执行
        with pytest.raises(ValueError, match="已作废"):
            execute_judgment(conn, j["id"])


def test_cancel_executing_releases_occupied_partition(db_path):
    with connect(db_path) as conn:
        j = _open_judgment(conn)
        pid = j["partition_id"]
        # 模拟执行中：裁决执行中、分区裁撤中
        conn.execute(
            "UPDATE judgments SET status='执行中' WHERE id=?", (j["id"],)
        )
        conn.execute(
            "UPDATE partitions SET storage_state='裁撤中' WHERE id=?", (pid,)
        )
        conn.commit()

        result = cancel_judgment(conn, j["id"])
        assert result["released"] is True
        state = conn.execute(
            "SELECT storage_state FROM partitions WHERE id=?", (pid,)
        ).fetchone()["storage_state"]
        assert state == "在线"
        run = conn.execute(
            "SELECT result_state, detail FROM execute_runs WHERE judgment_id=?",
            (j["id"],),
        ).fetchone()
        assert run["result_state"] == "已作废"
        assert "释放" in run["detail"]
        # 作废后再执行仍被拦
        with pytest.raises(ValueError, match="已作废"):
            execute_judgment(conn, j["id"])


def test_double_cancel_conflicts(db_path):
    with connect(db_path) as conn:
        j = _open_judgment(conn)
        cancel_judgment(conn, j["id"])
        with pytest.raises(ValueError, match="已作废"):
            cancel_judgment(conn, j["id"])


def test_cancel_completed_judgment_conflicts(db_path):
    with connect(db_path) as conn:
        j = _open_judgment(conn)
        execute_judgment(conn, j["id"])
        with pytest.raises(ValueError, match="不可作废"):
            cancel_judgment(conn, j["id"])


def test_concurrent_cancel_and_execute_only_one_wins(db_path):
    """两侧并发占坑锁写：要么执行成功，要么作废成功，不允许作废单还跑出成功足迹。"""
    outcomes = {}

    def do_cancel(jid):
        try:
            with connect(db_path) as conn:
                cancel_judgment(conn, jid)
            outcomes["cancel"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcomes["cancel"] = f"err:{type(exc).__name__}"

    def do_execute(jid):
        try:
            with connect(db_path) as conn:
                execute_judgment(conn, jid)
            outcomes["execute"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcomes["execute"] = f"err:{type(exc).__name__}"

    # 跑多轮，每轮新开一张库上的待执行裁决，线程起跑用 barrier 尽量贴近
    for round_no in range(8):
        with connect(db_path) as conn:
            j = _open_judgment(conn)
            jid = j["id"]
            pid = j["partition_id"]

        barrier = threading.Barrier(2)

        def cancel_side():
            barrier.wait()
            do_cancel(jid)

        def execute_side():
            barrier.wait()
            do_execute(jid)

        t1 = threading.Thread(target=cancel_side)
        t2 = threading.Thread(target=execute_side)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with connect(db_path) as conn:
            final_j = conn.execute(
                "SELECT status FROM judgments WHERE id=?", (jid,)
            ).fetchone()["status"]
            part = conn.execute(
                "SELECT storage_state FROM partitions WHERE id=?", (pid,)
            ).fetchone()["storage_state"]
            run = conn.execute(
                "SELECT result_state, ok FROM execute_runs WHERE judgment_id=?",
                (jid,),
            ).fetchone()

        if final_j == "已作废":
            # 作废赢：执行侧必须失败，且绝不允许出现成功执行足迹 / 分区被裁
            assert outcomes["execute"].startswith("err"), (round_no, outcomes)
            assert part == "在线", (round_no, part)
            assert run is not None and run["result_state"] == "已作废"
        elif final_j == "已完成":
            # 执行赢：作废侧必须冲突
            assert outcomes["cancel"].startswith("err"), (round_no, outcomes)
            assert part in ("已归档", "已销毁"), (round_no, part)
            assert run is not None and run["ok"] == 1
        else:  # pragma: no cover — 不允许出现执行中/待执行残留
            pytest.fail(f"第{round_no}轮终态异常：{final_j} / {outcomes}")

        # 重置该分区，供下一轮再开单
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE partitions SET storage_state='在线', archive_uri=NULL, "
                "row_count=COALESCE(row_count, 1) WHERE id=?",
                (pid,),
            )
            conn.commit()


def test_begin_write_is_idempotent_within_transaction(db_path):
    conn = connect(db_path)
    begin_write(conn)
    assert conn.in_transaction
    begin_write(conn)  # 不应重复 BEGIN 报错
    conn.rollback()
    conn.close()


def test_cancel_api_409_when_already_canceled(authed):
    scan = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert scan.status_code == 200
    jid = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"][0]["id"]

    first = authed.post(f"/api/judgments/{jid}/cancel")
    assert first.status_code == 200
    assert first.json()["cancel"]["status"] == "已作废"

    # 作废后走 API 执行 → 409
    blocked = authed.post(f"/api/judgments/{jid}/execute")
    assert blocked.status_code == 409
    assert "作废" in blocked.json()["detail"]

    # 重复作废 → 409
    again = authed.post(f"/api/judgments/{jid}/cancel")
    assert again.status_code == 409

    # 执行流水中可核作废足迹
    runs = authed.get("/api/runs").json()["items"]
    mine = [r for r in runs if r["judgment_id"] == jid]
    assert mine and mine[0]["result_state"] == "已作废"

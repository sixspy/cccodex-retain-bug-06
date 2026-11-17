import sqlite3

from app.db import begin_write
from app.services.partitions import get_partition

# 允许进入执行的裁决状态。已作废不在其中：作废即终态，拒绝再执行。
EXECUTABLE_STATUSES = ("待执行", "已失败")


def execute_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """执行单条裁决：归档写入 archive_uri；销毁清空在线态。整单失败则回滚本条。

    并发安全：先 BEGIN IMMEDIATE 占住写锁，再用带状态条件的 UPDATE 抢占
    裁决（仅当状态仍可执行且未作废时才能推进到执行中）。若并发的作废事务
    已先行落库，条件更新命中 0 行，本侧直接 409 拒绝，绝不二次裁撤分区。
    """
    begin_write(conn)

    row = conn.execute(
        "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
    ).fetchone()
    if not row:
        raise ValueError("裁决不存在")
    judgment = dict(row)
    if judgment["status"] not in EXECUTABLE_STATUSES:
        if judgment["status"] == "已作废":
            raise ValueError("裁决已作废，禁止执行")
        raise ValueError(f"当前状态不可执行：{judgment['status']}")

    part = get_partition(conn, judgment["partition_id"])
    if not part:
        raise ValueError("分区不存在")
    if part["storage_state"] not in ("在线", "裁撤中"):
        raise ValueError(f"分区状态不可裁撤：{part['storage_state']}")

    # 占坑锁写：条件 UPDATE 是并发裁决的唯一事实源，rowcount=0 即已被并发作废/执行
    cur = conn.execute(
        "UPDATE judgments SET status = '执行中', fail_reason = '' "
        "WHERE id = ? AND status IN (?, ?)",
        (judgment_id, *EXECUTABLE_STATUSES),
    )
    if cur.rowcount == 0:
        now = conn.execute(
            "SELECT status FROM judgments WHERE id = ?", (judgment_id,)
        ).fetchone()
        state = now["status"] if now else "不存在"
        if state == "已作废":
            raise ValueError("裁决已作废，禁止执行")
        raise ValueError(f"当前状态不可执行：{state}")

    conn.execute(
        "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
        (part["id"],),
    )

    action = judgment["decided_action"]
    try:
        if action == "归档":
            uri = f"archive://{part['domain_code']}/{part['biz_date']}"
            conn.execute(
                "UPDATE partitions SET storage_state = '已归档', archive_uri = ? WHERE id = ?",
                (uri, part["id"]),
            )
            result_state = "已归档"
            archive_uri = uri
            detail = "分区已归档并保留可查归档地址"
        elif action == "销毁":
            conn.execute(
                "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                "row_count = 0 WHERE id = ?",
                (part["id"],),
            )
            result_state = "已销毁"
            archive_uri = None
            detail = "分区已销毁，在线行数清零"
        else:
            raise ValueError(f"未知动作：{action}")

        conn.execute(
            "UPDATE judgments SET status = '已完成' WHERE id = ?",
            (judgment_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (judgment_id, action, result_state, archive_uri, detail),
        )
    except Exception as exc:  # noqa: BLE001 — 基线失败闭：恢复在线并记失败
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL WHERE id = ?",
            (part["id"],),
        )
        conn.execute(
            "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
            (str(exc), judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, "在线", None, str(exc)),
        )
        raise

    return get_run_by_judgment(conn, judgment_id)


def cancel_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """作废裁决：原子关闭占用并留下与执行同源的足迹。

    一个事务内完成：
      1. BEGIN IMMEDIATE 占住写锁（与并发执行互斥）；
      2. 条件 UPDATE 仅在「待执行/执行中」时把裁决推进到已作废——已作废、
         已完成等终态命中 0 行，重复作废/竞态执行后作废一律 409；
      3. 若分区仍被本裁决占用（裁撤中），同事务释放回在线，关闭占用；
      4. 写 execute_runs 足迹（result_state=已作废），作废状态与执行足迹
         在同一事务同源落地，查询执行列表即可核对该单为何未落地。
    """
    begin_write(conn)

    row = conn.execute(
        "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
    ).fetchone()
    if not row:
        raise ValueError("裁决不存在")
    judgment = dict(row)
    if judgment["status"] == "已作废":
        raise ValueError("裁决已作废，不能重复作废")
    if judgment["status"] not in ("待执行", "执行中"):
        raise ValueError(f"当前状态不可作废：{judgment['status']}")

    cur = conn.execute(
        "UPDATE judgments SET status = '已作废', fail_reason = '' "
        "WHERE id = ? AND status IN ('待执行','执行中')",
        (judgment_id,),
    )
    if cur.rowcount == 0:
        now = conn.execute(
            "SELECT status FROM judgments WHERE id = ?", (judgment_id,)
        ).fetchone()
        state = now["status"] if now else "不存在"
        raise ValueError(f"当前状态不可作废：{state}")

    # 原子关闭占用：仅释放仍由本裁决占着（裁撤中）的分区，已归档/已销毁不动
    part = get_partition(conn, judgment["partition_id"])
    if part and part["storage_state"] == "裁撤中":
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL "
            "WHERE id = ? AND storage_state = '裁撤中'",
            (part["id"],),
        )

    detail = (
        "裁决已作废，执行中占用已释放，分区回到在线"
        if judgment["status"] == "执行中"
        else "裁决已作废，未执行任何裁撤动作"
    )
    conn.execute(
        "INSERT OR REPLACE INTO execute_runs "
        "(judgment_id, action, result_state, archive_uri, ok, detail) "
        "VALUES (?,?,?,NULL,0,?)",
        (judgment_id, judgment["decided_action"], "已作废", detail),
    )

    return {
        "id": judgment_id,
        "status": "已作废",
        "partition_id": judgment["partition_id"],
        "released": bool(part and part["storage_state"] == "裁撤中"),
        "detail": detail,
    }


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]

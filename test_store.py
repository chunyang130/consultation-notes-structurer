#!/usr/bin/env python3
"""store.py 的自動測試：用專案中真實執行的 v3 結果匯入 SQLite，檢查報表內容。

用法：python test_store.py
"""
import json, os, sqlite3, sys, tempfile

import store

HERE = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(HERE, "examples", "real_run_v3.json"), encoding="utf-8"))
failed = 0


def check(label, cond, detail=""):
    global failed
    print(("✓ " if cond else "✗ ") + label + ("" if cond else f"  {detail}"))
    failed += not cond


with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "leads.db")
    con = store.connect(db)

    added, updated = store.load(con, rows)
    check("第一次匯入新增 8 筆", (added, updated) == (8, 0), (added, updated))
    added, updated = store.load(con, rows)
    check("重複匯入只更新、不重複新增", (added, updated) == (0, 8) and
          con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 8)

    r = store.run_reports(con, "2026-11-10")
    week = [row[1] for row in r["本週要跟進（今天起 7 天內）"][1]]
    check("本週要跟進：王同學、黃小姐（依日期排序）", week == ["王同學", "黃小姐"], week)
    overdue = r["已逾期未跟進"][1]
    check("已逾期：林媽媽的女兒，逾期 1 天", len(overdue) == 1 and overdue[0][1] == 1 and "林" in overdue[0][2], overdue)
    nodate = [row[1] for row in r["追蹤中但沒有約跟進日期"][1]]
    check("沒約跟進日期：陳先生", nodate == ["陳先生"], nodate)
    status = dict((row[0], row[1]) for row in r["狀態分布"][1])
    check("狀態分布加總 8 人，考慮中 3 人", sum(status.values()) == 8 and status["考慮中"] == 3, status)
    goals = {row[0]: row[1:] for row in r["各目標的報價與成交"][1]}
    check("研究所英文：1 人諮詢、1 人成交", goals["研究所英文"][:3] == (1, 0, 1), goals.get("研究所英文"))

    r2 = store.run_reports(con, "2026-11-12")
    check("換基準日（11/12）：王同學變成逾期", "王同學" in [row[2] for row in r2["已逾期未跟進"][1]])

    con.execute("DELETE FROM leads WHERE student = '陳先生'")
    con.commit()
    orphan = con.execute("SELECT COUNT(*) FROM lead_concerns c LEFT JOIN leads l ON l.id = c.lead_id WHERE l.id IS NULL").fetchone()[0]
    check("刪除學員時，顧慮與目標一併刪除（外鍵 CASCADE）", orphan == 0, orphan)
    con.close()

print(f"\n{'全部通過' if not failed else f'{failed} 項失敗'}")
sys.exit(1 if failed else 0)

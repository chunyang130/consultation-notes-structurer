#!/usr/bin/env python3
"""把結構化後的諮詢紀錄存進 SQLite，並用 SQL 產生業務追蹤報表。

只用 Python 標準函式庫（sqlite3）。

用法：
  python store.py --json examples/real_run_v3.json --db output/leads.db --today 2026-11-10

會做的事：
  1. 建立資料表（leads、lead_goals、lead_concerns）
  2. 匯入 notes_to_json.py 輸出的 JSON；同一筆紀錄重複匯入不會重複寫入
  3. 執行 queries.sql 裡的每一段查詢，印出報表
"""
import argparse, json, os, re, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY,
    consult_date   TEXT NOT NULL,          -- 諮詢日期 YYYY-MM-DD
    student        TEXT,
    identity       TEXT,
    target_level   TEXT,
    target_month   TEXT,                   -- YYYY-MM
    budget         INTEGER,
    class_mode     TEXT,
    status         TEXT,                   -- 已成交／已報價／考慮中／無意願／未提及
    status_basis   TEXT,
    next_follow_up TEXT,                   -- YYYY-MM-DD
    must_handle    TEXT,                   -- 程式依業務規則判斷、需要人工處理的事項
    raw_note       TEXT NOT NULL,
    UNIQUE (consult_date, raw_note)        -- 重複匯入時不會產生重複資料
);
CREATE TABLE IF NOT EXISTS lead_goals (
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    goal    TEXT NOT NULL,
    PRIMARY KEY (lead_id, goal)
);
CREATE TABLE IF NOT EXISTS lead_concerns (
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    concern TEXT NOT NULL,
    PRIMARY KEY (lead_id, concern)
);
CREATE INDEX IF NOT EXISTS idx_leads_follow ON leads(status, next_follow_up);
"""


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def load(con, rows):
    """匯入 JSON；同一筆（諮詢日期＋原始摘要）已存在就更新，不重複新增。"""
    added = updated = 0
    with con:   # 交易：全部成功才寫入
        for r in rows:
            # 舊版輸出只有「需人工確認」一欄，新版拆成「必須處理」；兩種都能匯入
            must = r.get("必須處理", r.get("需人工確認", "")) or None
            values = (r["諮詢日期"], r.get("學員代稱"), r.get("身分"), r.get("目標分數或程度"), r.get("預計時間"),
                      r.get("預算"), r.get("上課方式"), r.get("狀態"), r.get("狀態依據"), r.get("下次跟進日期"),
                      must, r["原始摘要"])
            old = con.execute("SELECT id FROM leads WHERE consult_date = ? AND raw_note = ?",
                              (r["諮詢日期"], r["原始摘要"])).fetchone()
            if old:
                lead_id = old[0]
                con.execute("""UPDATE leads SET student=?, identity=?, target_level=?, target_month=?, budget=?,
                               class_mode=?, status=?, status_basis=?, next_follow_up=?, must_handle=?
                               WHERE id=?""", values[1:11] + (lead_id,))
                con.execute("DELETE FROM lead_goals WHERE lead_id = ?", (lead_id,))
                con.execute("DELETE FROM lead_concerns WHERE lead_id = ?", (lead_id,))
                updated += 1
            else:
                lead_id = con.execute("""INSERT INTO leads (consult_date, student, identity, target_level, target_month,
                               budget, class_mode, status, status_basis, next_follow_up, must_handle, raw_note)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", values).lastrowid
                added += 1
            con.executemany("INSERT OR IGNORE INTO lead_goals VALUES (?, ?)", [(lead_id, g) for g in r.get("目標") or []])
            con.executemany("INSERT OR IGNORE INTO lead_concerns VALUES (?, ?)", [(lead_id, c) for c in r.get("顧慮") or []])
    return added, updated


def read_queries(path):
    """queries.sql 以「-- name: 標題」分段，每段一個查詢。"""
    text = open(path, encoding="utf-8").read()
    parts = re.split(r"^-- name:\s*(.+)$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def run_reports(con, today, queries_path=os.path.join(HERE, "queries.sql")):
    results = {}
    for title, sql in read_queries(queries_path):
        cur = con.execute(sql, {"today": today})
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        results[title] = (cols, rows)
    return results


def print_table(title, cols, rows):
    print(f"\n## {title}（{len(rows)} 筆）")
    if not rows:
        print("（無）")
        return
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join("" if v is None else str(v) for v in r) + " |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="notes_to_json.py 輸出的 JSON")
    ap.add_argument("--db", default=os.path.join("output", "leads.db"))
    ap.add_argument("--today", required=True, help="報表基準日 YYYY-MM-DD")
    a = ap.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.today):
        raise SystemExit("--today 格式要是 YYYY-MM-DD")

    con = connect(a.db)
    added, updated = load(con, json.load(open(a.json, encoding="utf-8")))
    total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    print(f"匯入完成：新增 {added} 筆、更新 {updated} 筆，資料庫共 {total} 筆 → {a.db}")
    print(f"報表基準日：{a.today}")
    for title, (cols, rows) in run_reports(con, a.today).items():
        print_table(title, cols, rows)


if __name__ == "__main__":
    main()

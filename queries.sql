-- 業務追蹤報表。每段以「-- name: 標題」開頭，:today 為報表基準日（YYYY-MM-DD）。
-- 「追蹤中」的名單 = 狀態為考慮中或已報價。

-- name: 本週要跟進（今天起 7 天內）
SELECT next_follow_up AS 跟進日, student AS 學員, status AS 狀態,
       target_level AS 目標, status_basis AS 狀態依據
FROM leads
WHERE status IN ('考慮中', '已報價')
  AND next_follow_up BETWEEN :today AND date(:today, '+6 days')
ORDER BY next_follow_up, student;

-- name: 已逾期未跟進
SELECT next_follow_up AS 原定跟進日,
       CAST(julianday(:today) - julianday(next_follow_up) AS INTEGER) AS 逾期天數,
       student AS 學員, status AS 狀態
FROM leads
WHERE status IN ('考慮中', '已報價')
  AND next_follow_up < :today
ORDER BY next_follow_up;

-- name: 追蹤中但沒有約跟進日期
SELECT consult_date AS 諮詢日, student AS 學員, status AS 狀態, status_basis AS 狀態依據
FROM leads
WHERE status IN ('考慮中', '已報價')
  AND next_follow_up IS NULL
ORDER BY consult_date;

-- name: 狀態分布
SELECT status AS 狀態, COUNT(*) AS 人數,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM leads), 1) AS 百分比
FROM leads
GROUP BY status
ORDER BY 人數 DESC, 狀態;

-- name: 各目標的報價與成交
SELECT g.goal AS 目標,
       COUNT(*) AS 諮詢人數,
       SUM(l.status = '已報價') AS 已報價,
       SUM(l.status = '已成交') AS 已成交,
       SUM(l.status = '考慮中') AS 考慮中
FROM lead_goals g
JOIN leads l ON l.id = g.lead_id
GROUP BY g.goal
ORDER BY 諮詢人數 DESC, 目標;

-- name: 追蹤中名單的顧慮
SELECT c.concern AS 顧慮, l.student AS 學員, l.status AS 狀態
FROM lead_concerns c
JOIN leads l ON l.id = c.lead_id
WHERE l.status IN ('考慮中', '已報價')
ORDER BY l.status, l.student;

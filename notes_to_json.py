#!/usr/bin/env python3
"""諮詢紀錄結構化：把顧問寫的口語化諮詢摘要，透過 Claude API 轉成固定欄位，並在程式端驗證。

只用 Python 標準函式庫（Python 3.9 以上），不需安裝任何套件。

用法:
  # 不呼叫 API，用預先錄好的模型回覆示範整個流程（免費）
  python3 notes_to_json.py --input examples/sample_notes.txt --mock examples/mock_responses.json --out output/leads.csv

  # 實際呼叫 Claude API（需要 API key）
  export ANTHROPIC_API_KEY=你的金鑰
  python3 notes_to_json.py --input examples/sample_notes.txt --out output/leads.csv

設計重點：
  1. 用 tool use 強制模型照 JSON Schema 回傳，不靠「請輸出 JSON」的口頭要求
  2. 模型回傳後，程式再驗證一次（欄位、選項、日期格式）；不合格就把錯誤告訴模型重試一次
  3. 業務規則由程式把關，不只相信模型：例如「已報」是已報價，摘要沒寫「成交」就不能判成已成交
  4. 摘要沒提到的資訊一律填 null，不讓模型猜；「必須處理」由程式依業務規則判斷，模型的模糊標記只當參考
  5. 日期由程式算好對照表給模型查，並檢查「下週」等說法有沒有算錯
"""
import argparse, csv, datetime, json, os, re, sys, time, urllib.error, urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"   # 最便宜的型號就夠用；可用 --model 換

GOALS = ["多益", "托福", "雅思", "英文會話", "留學申請", "研究所英文", "其他"]
STATUS = ["已成交", "已報價", "考慮中", "無意願", "未提及"]
IDENTITY = ["高中生", "大學生", "研究生", "上班族", "家長代詢", "其他", "未提及"]
MODE = ["面授", "線上", "不限", "未提及"]

# API 規定 schema 的欄位名稱只能用英文、數字、底線，所以內部用英文鍵，輸出時再轉成中文標題
LABEL = {
    "student": "學員代稱", "identity": "身分", "goals": "目標", "target_level": "目標分數或程度",
    "target_month": "預計時間", "budget": "預算", "class_mode": "上課方式", "status": "狀態",
    "status_basis": "狀態依據", "next_follow_up": "下次跟進日期", "concerns": "顧慮", "unclear_fields": "內容模糊的欄位",
}
SCHEMA = {
    "type": "object",
    "properties": {
        "student": {"type": "string", "description": "學員代稱：照摘要原文的稱呼，例如「王同學」；只知道姓氏就寫「蔡（姓）」，不可自行加上同學、先生、小姐"},
        "identity": {"type": "string", "enum": IDENTITY, "description": "身分"},
        "goals": {"type": "array", "items": {"type": "string", "enum": GOALS}, "description": "目標，可複選"},
        "target_level": {"type": ["string", "null"], "description": "目標分數或程度，例如「多益 800」「雅思 6.5」；沒寫就 null"},
        "target_month": {"type": ["string", "null"], "description": "預計考試或出國時間，格式 YYYY-MM；只寫季節或模糊時間就 null"},
        "budget": {"type": ["integer", "null"], "description": "預算，新台幣整數；摘要沒寫明確金額就 null"},
        "class_mode": {"type": "string", "enum": MODE, "description": "上課方式"},
        "status": {"type": "string", "enum": STATUS,
                   "description": "狀態，依序判斷：寫明「成交」「已繳費」→已成交；有「已報」「報價」→已報價（即使同時說會回覆或要考慮，已報價優先）；明確拒絕→無意願；說會回覆、要考慮、要討論、約了檢測→考慮中"},
        "status_basis": {"type": "string", "description": "狀態依據：從摘要原文直接複製判斷狀態的那一小段，一字不改；狀態為未提及時留空字串"},
        "next_follow_up": {"type": ["string", "null"], "description": "下次跟進日期 YYYY-MM-DD；查使用者訊息中的日期對照表，不要自己推算；沒提到就 null"},
        "concerns": {"type": "array", "items": {"type": "string"}, "description": "顧慮：學員猶豫的原因，例如價格、時間"},
        "unclear_fields": {"type": "array", "items": {"type": "string", "enum": list(LABEL)},
                           "description": "只列「用約略說法」或「前後矛盾」的欄位英文鍵名，例如預算寫「十幾萬」、時間寫「明年」。寫得清楚的（例如「只能上線上」「預算 5 萬左右」）不要列；完全沒提到的也不要列"},
    },
    "required": list(LABEL),
}

SYSTEM = """你是補習班的業務助理，負責把顧問寫的諮詢摘要整理成固定欄位。
規則：
- 只根據摘要內容填寫，不可推測或補充摘要沒寫的資訊。
- 摘要沒提到的欄位填 null（或「未提及」），不需要另外標記；有提到但寫得模糊的，填 null 並列入 unclear_fields。
- 「已報」「報價」代表已報價，不是成交；只有寫明「成交」「已繳費」才填已成交。
- 日期一律查使用者訊息中的日期對照表，不要自己推算。一週從星期一開始：「這週三」是同一週的星期三，「下週三」是下一週的星期三；「月底」是對照表標示（月底）的那一天。
- 狀態有優先順序：已成交 > 已報價 > 考慮中。摘要提到「已報」時，即使學員說會再回覆，狀態仍是已報價。
一律呼叫 record_lead 工具回傳結果。"""


WEEKDAY = "一二三四五六日"


def weekday(d):
    return "星期" + WEEKDAY[datetime.date.fromisoformat(d).weekday()]


def calendar(d, days=21):
    """語言模型不擅長日期計算，所以由程式算好對照表給模型查（至少涵蓋到當月月底）。"""
    base = datetime.date.fromisoformat(d)
    monday = base - datetime.timedelta(days=base.weekday())
    month_end = (base.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    days = max(days, (month_end - base).days)
    lines = ["日期對照表（一週從星期一開始）："]
    for i in range(1, days + 1):
        x = base + datetime.timedelta(days=i)
        wk = (x - monday).days // 7
        tag = {0: "本週", 1: "下週", 2: "下下週"}.get(wk, "")
        end = "（月底）" if (x + datetime.timedelta(days=1)).day == 1 else ""
        lines.append(f"{x.isoformat()} {tag}{WEEKDAY[x.weekday()]}{end}")
    return "\n".join(lines)


def business_flags(rec, note):
    """業務上一定要人工處理的情況，由程式判斷，不交給模型。"""
    flags = []
    status, follow = rec.get("status"), rec.get("next_follow_up")
    if status in ("考慮中", "已報價") and not follow:
        flags.append("缺下次跟進日期")
    if status == "未提及":
        flags.append("狀態不明")
    if status in ("考慮中", "已報價", "未提及") and not follow and re.search(r"下週|下禮拜|週[一二三四五六日]|星期|月底|明天|後天|\d+/\d+", note["text"]):
        flags.append("摘要有提到時間，但沒有算出跟進日期")
    return flags


def read_notes(path):
    """讀取諮詢摘要。格式：每筆以「## 諮詢日期 YYYY-MM-DD」開頭，下一行起為摘要內容。"""
    text = open(path, encoding="utf-8").read()
    notes = []
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        m = re.search(r"\d{4}-\d{2}-\d{2}", head)
        if m and body.strip():
            notes.append({"date": m.group(0), "text": body.strip()})
    return notes


def call_claude(note, model, api_key, feedback=None):
    """呼叫 Claude Messages API，用 tool_choice 強制回傳符合 SCHEMA 的 JSON。"""
    content = f"諮詢日期：{note['date']}（{weekday(note['date'])}）\n{calendar(note['date'])}\n\n諮詢摘要：\n{note['text']}"
    if feedback:
        content += f"\n\n上一次的結果有問題，請修正：\n{feedback}"
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": SYSTEM,
        "tools": [{"name": "record_lead", "description": "記錄一筆結構化的諮詢資料", "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": "record_lead"},
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    for attempt in range(3):   # 網路或限流錯誤最多重試 3 次
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 529) and attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            raise SystemExit(f"API 錯誤 {e.code}：{e.read().decode('utf-8', 'ignore')[:300]}")
    tool = next(b for b in resp["content"] if b["type"] == "tool_use")
    return tool["input"], resp.get("usage", {})


def validate(rec, note):
    """程式端驗證：格式、選項、業務規則。回傳問題清單。"""
    errs = []
    for f in SCHEMA["required"]:
        if f not in rec:
            errs.append(f"缺少欄位「{LABEL[f]}」")
    if rec.get("identity") not in IDENTITY:
        errs.append(f"身分「{rec.get('identity')}」不在選項內")
    if rec.get("class_mode") not in MODE:
        errs.append(f"上課方式「{rec.get('class_mode')}」不在選項內")
    if rec.get("status") not in STATUS:
        errs.append(f"狀態「{rec.get('status')}」不在選項內")
    bad_goals = [g for g in rec.get("goals", []) if g not in GOALS]
    if bad_goals:
        errs.append(f"目標 {bad_goals} 不在選項內")
    if rec.get("target_month") is not None and not re.fullmatch(r"\d{4}-\d{2}", str(rec["target_month"])):
        errs.append("預計時間要是 YYYY-MM 或 null")
    d = rec.get("next_follow_up")
    if d is not None:
        try:
            if datetime.date.fromisoformat(d) < datetime.date.fromisoformat(note["date"]):
                errs.append("下次跟進日期早於諮詢日期")
        except (ValueError, TypeError):
            errs.append("下次跟進日期要是 YYYY-MM-DD 或 null")
    if rec.get("budget") is not None and not isinstance(rec["budget"], int):
        errs.append("預算要是整數或 null")
    # 業務規則：不只相信模型
    if rec.get("status") == "已成交" and not re.search(r"成交|已繳費|繳費完成", note["text"]):
        errs.append("摘要沒有寫成交或繳費，不能判為已成交（「已報」是已報價）")
    if rec.get("status") in ("考慮中", "未提及") and re.search(r"已報|報價", note["text"]):
        errs.append("摘要有「已報」或「報價」，狀態應為已報價（已報價優先於考慮中）")
    basis = rec.get("status_basis", "")
    if basis and basis not in note["text"]:
        errs.append("狀態依據必須從摘要原文直接複製，一字不改")
    if not basis and rec.get("status") not in ("未提及", None):
        errs.append("有判斷狀態就必須附上狀態依據")
    # 日期規則：摘要寫「下週」，跟進日期就不能落在諮詢當週
    if d and re.search(r"下週|下禮拜", note["text"]):
        try:
            base, f = datetime.date.fromisoformat(note["date"]), datetime.date.fromisoformat(d)
            if (f - (base - datetime.timedelta(days=base.weekday()))).days < 7:
                errs.append(f"摘要寫「下週」，但跟進日期 {d} 仍在諮詢當週，請查日期對照表")
        except ValueError:
            pass
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mock", help="用預先錄好的模型回覆（JSON）示範，不呼叫 API")
    a = ap.parse_args()

    notes = read_notes(a.input)
    mock = json.load(open(a.mock, encoding="utf-8")) if a.mock else None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not mock and not key:
        raise SystemExit("找不到 ANTHROPIC_API_KEY。要免費示範請加 --mock examples/mock_responses.json")

    rows, tokens = [], {"input_tokens": 0, "output_tokens": 0}
    for i, note in enumerate(notes):
        if mock:
            tries = mock[i] if isinstance(mock[i], list) else [mock[i]]
            rec, errs = tries[0], validate(tries[0], note)
            if errs and len(tries) > 1:       # 模擬「把錯誤回饋給模型重試」
                print(f"第 {i + 1} 筆驗證未過，回饋給模型重試：{errs}")
                rec, errs = tries[1], validate(tries[1], note)
        else:
            rec, usage = call_claude(note, a.model, key)
            errs = validate(rec, note)
            if errs:
                print(f"第 {i + 1} 筆驗證未過，回饋給模型重試：{errs}")
                rec, usage2 = call_claude(note, a.model, key, feedback="\n".join(errs))
                errs = validate(rec, note)
                for k in tokens:
                    tokens[k] += usage2.get(k, 0)
            for k in tokens:
                tokens[k] += usage.get(k, 0)
        must = business_flags(rec, note) + errs            # 程式依業務規則判斷：一定要處理
        hint = [LABEL.get(k, k) for k in rec.get("unclear_fields", [])]   # 模型認為模糊：僅供參考
        rows.append({"諮詢日期": note["date"], **{LABEL[k]: rec.get(k) for k in LABEL if k != "unclear_fields"},
                     "必須處理": "；".join(must), "參考（模型認為模糊）": "、".join(hint), "原始摘要": note["text"]})

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig：Excel 直接開中文不亂碼
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: "、".join(v) if isinstance(v, list) else ("" if v is None else v) for k, v in r.items()})
    with open(os.path.splitext(a.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    must = sum(1 for r in rows if r["必須處理"])
    hint = sum(1 for r in rows if r["參考（模型認為模糊）"])
    print(f"完成 {len(rows)} 筆：{must} 筆必須處理，{hint} 筆有模型標記的模糊欄位（參考）→ {a.out}")
    if not mock:
        print(f"Token 用量：輸入 {tokens['input_tokens']}、輸出 {tokens['output_tokens']}")


if __name__ == "__main__":
    main()

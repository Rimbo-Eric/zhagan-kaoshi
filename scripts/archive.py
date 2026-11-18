#!/usr/bin/env python3
"""错题档案工具：建档、查知识点、写入新错题、记录重做结果、列出待复习的题、生成跨考试报告。

只依赖 Python 标准库。错题档案是一个 UTF-8 编码的 JSON 文件（默认名"错题档案.json"），
格式见 references/archive.md；知识点 ID 来自 references/knowledge-map/<科目>.md。

用法：
  python archive.py init      --archive 错题档案.json [--stage 高中] [--grade 高三] [--textbook 数学=人教A版 ...]
  python archive.py kp-search --subject 数学 [--stage 高中] --q 导数 [--q 零点 ...]
  python archive.py merge     --archive 错题档案.json --new 本次.json [--today YYYY-MM-DD]
  python archive.py update    --archive 错题档案.json --id <id> --result pass|fail [--today YYYY-MM-DD]
  python archive.py due       --archive 错题档案.json [--subject 数学] [--today YYYY-MM-DD]
  python archive.py report    --archive 错题档案.json [--subject 数学]
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DIR = os.path.normpath(os.path.join(HERE, "..", "references", "knowledge-map"))

SUBJECT_CODES = {
    "数学": "SX", "物理": "WL", "化学": "HX", "生物": "SW", "语文": "YW",
    "英语": "YY", "政治": "ZZ", "历史": "LS", "地理": "DL",
}
CODE_SUBJECTS = {v: k for k, v in SUBJECT_CODES.items()}
SUBJECT_ALIASES = {"道德与法治": "政治", "思想政治": "政治", "思政": "政治", "生物学": "生物"}
STAGE_CODES = {"初中": "CZ", "高中": "GZ"}
CATEGORIES = ["基础漏洞", "思路盲区", "习惯失误", "时间与策略"]
TODO, SECOND, MASTERED = "待重做", "二刷", "已掌握"
INTERVAL_AFTER_FAIL, INTERVAL_AFTER_FIRST_PASS, INTERVAL_AFTER_MASTERED = 1, 7, 30

MISTAKE_INPUT_FIELDS = [
    "exam_date", "exam_name", "subject", "question", "kp_id", "kp_name", "related_kp_ids",
    "category", "detail", "stuck_point", "points_lost", "source",
]
ROW_RE = re.compile(r"^\|\s*((?:CZ|GZ)-[A-Z]{2}-\d{2}-\d{2})\s*\|(.*)\|\s*$")

_map_cache = {}
_map_status = {}


# ---------- 通用 ----------

def parse_day(text):
    try:
        return date.fromisoformat(str(text))
    except (TypeError, ValueError):
        raise SystemExit(f"日期格式不对：{text!r}，应为 YYYY-MM-DD")


def norm_subject(name):
    name = (name or "").strip()
    return SUBJECT_ALIASES.get(name, name)


def to_points(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fmt_points(x):
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def unit_of(kp_id):
    return "-".join(kp_id.split("-")[:3]) if kp_id else ""


# ---------- 知识点地图 ----------

def load_map(subject):
    subject = norm_subject(subject)
    if subject in _map_cache:
        return _map_cache[subject]
    table = {}
    path = os.path.join(MAP_DIR, f"{subject}.md")
    _map_status[subject] = "缺失"
    if os.path.exists(path):
        _map_status[subject] = "未注明"
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "状态：" in line and _map_status[subject] == "未注明":
                    _map_status[subject] = "草稿" if "草稿" in line else ("已核对" if "已核对" in line else "未注明")
                m = ROW_RE.match(line.strip())
                if not m:
                    continue
                cells = [c.strip() for c in m.group(2).split("|")]
                cells += [""] * (3 - len(cells))
                table[m.group(1)] = {"unit": cells[0], "name": cells[1], "location": cells[2]}
    _map_cache[subject] = table
    return table


def lookup_kp(kp_id):
    if not kp_id:
        return None
    parts = kp_id.split("-")
    if len(parts) != 4 or parts[1] not in CODE_SUBJECTS:
        return None
    return load_map(CODE_SUBJECTS[parts[1]]).get(kp_id)


def unit_name(unit_id):
    parts = unit_id.split("-")
    if len(parts) != 3 or parts[1] not in CODE_SUBJECTS:
        return ""
    for kp_id, info in load_map(CODE_SUBJECTS[parts[1]]).items():
        if kp_id.startswith(unit_id + "-"):
            return info["unit"]
    return ""


# ---------- 档案读写 ----------

def empty_archive():
    return {"version": 1, "profile": {}, "exams": [], "mistakes": []}


def load_archive(path, must_exist=True):
    if not os.path.exists(path):
        if must_exist:
            raise SystemExit(f"找不到错题档案：{path}（先用 init 新建）")
        return empty_archive()
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    for key, default in empty_archive().items():
        data.setdefault(key, default)
    return data


def save_archive(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------- 命令 ----------

def cmd_init(args):
    data = load_archive(args.archive, must_exist=False)
    profile = data["profile"]
    if args.stage:
        if args.stage not in STAGE_CODES:
            raise SystemExit("学段只能是：初中、高中")
        profile["stage"] = args.stage
    if args.grade:
        profile["grade"] = args.grade
    books = profile.setdefault("textbooks", {})
    for item in args.textbook or []:
        if "=" not in item:
            raise SystemExit(f"教材版本格式应为 科目=版本，例如 数学=人教A版：{item!r}")
        subj, book = item.split("=", 1)
        books[norm_subject(subj)] = book.strip()
    profile["updated"] = args.today
    save_archive(args.archive, data)
    print(f"错题档案已就绪：{args.archive}")
    print(json.dumps(profile, ensure_ascii=False))


def cmd_kp_search(args):
    subject = norm_subject(args.subject)
    table = load_map(subject)
    if not table:
        raise SystemExit(f"没有找到「{subject}」的知识点地图：{os.path.join(MAP_DIR, subject + '.md')}。"
                         "这一科的错题只写知识点名称、不填 ID。")
    if _map_status.get(subject) != "已核对":
        print(f"注意：「{subject}」知识点地图的状态是「{_map_status.get(subject)}」，ID 可以照常使用，"
              "但教材位置没有对照官方原文核对过，报告里不要当成事实引用。\n")
    prefix = STAGE_CODES.get(args.stage or "", "")
    terms = [t for t in (args.q or []) if t.strip()]
    scored = []
    for kp_id, info in table.items():
        if prefix and not kp_id.startswith(prefix + "-"):
            continue
        if "已停用" in info["name"]:
            continue
        text = f"{info['unit']} {info['name']} {info['location']}"
        score = sum(1 for t in terms if t in text)
        if terms and score == 0:
            continue
        scored.append((-score, kp_id, info))
    scored.sort()
    if not scored:
        print("没有匹配的知识点。换个关键词（用教材里的说法，比如章节名），或者去掉学段限制再试。")
        return
    print("| id | 单元 | 知识点 | 教材位置 |")
    print("|---|---|---|---|")
    for _, kp_id, info in scored[: args.limit]:
        print(f"| {kp_id} | {info['unit']} | {info['name']} | {info['location']} |")
    if len(scored) > args.limit:
        print(f"\n（还有 {len(scored) - args.limit} 条，加关键词缩小范围）")


def cmd_merge(args):
    today = parse_day(args.today)
    data = load_archive(args.archive, must_exist=False)
    with open(args.new, encoding="utf-8-sig") as f:
        incoming = json.load(f)
    exam = None
    if isinstance(incoming, dict):
        exam = incoming.get("exam")
        items = incoming.get("mistakes", [])
    else:
        items = incoming
    if not isinstance(items, list):
        raise SystemExit("新错题应为 JSON 数组，或包含 exam 和 mistakes 的对象")

    notes = []
    if exam:
        exam = dict(exam)
        exam["subject"] = norm_subject(exam.get("subject", ""))
        parse_day(exam.get("date"))
        key = (exam.get("date"), exam.get("name"), exam["subject"])
        if any((e.get("date"), e.get("name"), e.get("subject")) == key for e in data["exams"]):
            notes.append(f"考试 {exam.get('date')} {exam.get('name')} {exam['subject']} 已在档案中，未重复记录")
        else:
            data["exams"].append(exam)

    existing = {m["id"] for m in data["mistakes"]}
    added, skipped = 0, []
    for i, item in enumerate(items, 1):
        r = {k: item.get(k) for k in MISTAKE_INPUT_FIELDS}
        for k in MISTAKE_INPUT_FIELDS:
            if k != "related_kp_ids":
                r[k] = "" if r[k] is None else str(r[k]).strip()
        r["subject"] = norm_subject(r["subject"])
        missing = [k for k in ("exam_date", "subject", "question") if not r[k]]
        if missing:
            notes.append(f"第 {i} 条缺少 {', '.join(missing)}，已跳过")
            continue
        parse_day(r["exam_date"])
        r["id"] = f"{r['exam_date']}-{r['subject']}-{r['question']}"
        if r["id"] in existing:
            skipped.append(r["id"])
            continue
        if r["category"] and r["category"] not in CATEGORIES:
            notes.append(f"第 {i} 条错因类别 {r['category']!r} 不在 {CATEGORIES} 中，请核对")
        if r["points_lost"]:
            try:
                float(r["points_lost"])
            except ValueError:
                notes.append(f"第 {i} 条丢分 {r['points_lost']!r} 不是数字，已清空")
                r["points_lost"] = ""
        if r["kp_id"]:
            info = lookup_kp(r["kp_id"])
            if info is None:
                notes.append(f"第 {i} 条知识点 ID {r['kp_id']} 不在知识点地图里，已清空 ID，只保留名称；请用 kp-search 重新查")
                r["kp_id"] = ""
            else:
                if "已停用" in info["name"]:
                    notes.append(f"第 {i} 条知识点 {r['kp_id']} 已停用，请用 kp-search 换一个现行条目")
                if not r["kp_name"]:
                    r["kp_name"] = info["name"]
        elif r["kp_name"]:
            notes.append(f"第 {i} 条只有知识点名称「{r['kp_name']}」、没有 ID，跨考试统计只能按名称匹配，不够可靠")
        related = item.get("related_kp_ids") or []
        if isinstance(related, str):
            related = [x.strip() for x in related.split(",") if x.strip()]
        valid_related = []
        for kp in related:
            if lookup_kp(kp):
                valid_related.append(kp)
            else:
                notes.append(f"第 {i} 条相关知识点 {kp} 不在地图里，已忽略")
        r["related_kp_ids"] = valid_related
        r["source"] = r["source"] or "学生"
        r.update(status=TODO, correct_count=0, last_review="", next_review=today.isoformat())
        data["mistakes"].append(r)
        existing.add(r["id"])
        added += 1

    save_archive(args.archive, data)
    print(f"已写入 {added} 道错题，档案中共 {len(data['mistakes'])} 道错题、{len(data['exams'])} 次考试：{args.archive}")
    if skipped:
        print(f"跳过 {len(skipped)} 条已存在的记录：{', '.join(skipped)}")
    for n in notes:
        print(f"注意：{n}")


def cmd_update(args):
    today = parse_day(args.today)
    data = load_archive(args.archive)
    target = next((m for m in data["mistakes"] if m["id"] == args.id), None)
    if target is None:
        raise SystemExit(f"找不到 id 为 {args.id!r} 的错题")
    count = int(target.get("correct_count") or 0)
    if args.result == "pass":
        count += 1
        if target["status"] == MASTERED:
            target["next_review"] = ""
        elif count >= 2:
            target["status"] = MASTERED
            target["next_review"] = (today + timedelta(days=INTERVAL_AFTER_MASTERED)).isoformat()
        else:
            target["status"] = SECOND
            target["next_review"] = (today + timedelta(days=INTERVAL_AFTER_FIRST_PASS)).isoformat()
    else:
        count = 0  # 需要重新连续做对两次
        target["status"] = TODO
        target["next_review"] = (today + timedelta(days=INTERVAL_AFTER_FAIL)).isoformat()
    target["correct_count"] = count
    target["last_review"] = today.isoformat()
    save_archive(args.archive, data)
    nxt = target["next_review"] or "无（已完成）"
    print(f"{args.id}：状态 {target['status']}，连续做对 {count} 次，下次复习 {nxt}")


def cmd_due(args):
    today = parse_day(args.today)
    data = load_archive(args.archive)
    rows = [m for m in data["mistakes"] if m.get("next_review") and parse_day(m["next_review"]) <= today]
    if args.subject:
        rows = [m for m in rows if m["subject"] == norm_subject(args.subject)]
    if not rows:
        print("今天没有需要复习的题。")
        return
    rows.sort(key=lambda m: (m["next_review"], m["subject"], m["exam_date"]))
    print(f"今天（{today.isoformat()}）需要复习 {len(rows)} 道题：\n")
    print("| id | 科目 | 题号 | 知识点 | 状态 | 应复习日期 |")
    print("|---|---|---|---|---|---|")
    for m in rows:
        print(f"| {m['id']} | {m['subject']} | {m['question']} | {m.get('kp_name', '')} | {m['status']} | {m['next_review']} |")


def exam_label(m):
    return f"{m['exam_date']} {m.get('exam_name', '')}".strip()


def cmd_report(args):
    data = load_archive(args.archive)
    mistakes = data["mistakes"]
    exams = data["exams"]
    if args.subject:
        subj = norm_subject(args.subject)
        mistakes = [m for m in mistakes if m["subject"] == subj]
        exams = [e for e in exams if e.get("subject") == subj]
    if not mistakes:
        print("档案中还没有错题（或没有该科目的记录）。")
        return

    title = f"错题档案报告（{norm_subject(args.subject)}）" if args.subject else "错题档案报告"
    exam_labels = sorted({(m["exam_date"], exam_label(m)) for m in mistakes})
    latest = max(m["exam_date"] for m in mistakes)
    print(f"# {title}\n")
    profile = data.get("profile") or {}
    if profile:
        books = "、".join(f"{k}{v}" for k, v in (profile.get("textbooks") or {}).items())
        print(f"学生：{profile.get('stage', '')}{profile.get('grade', '')}" + (f"；教材：{books}" if books else "") + "\n")
    print(f"共 {len(exam_labels)} 次考试、{len(mistakes)} 道错题。最近一次考试：{latest}。\n")

    if exams:
        print("## 考试记录\n")
        print("| 日期 | 考试 | 科目 | 得分 |")
        print("|---|---|---|---|")
        for e in sorted(exams, key=lambda e: e.get("date", "")):
            score = f"{e.get('score', '')}/{e.get('full', '')}" if e.get("score") not in (None, "") else ""
            print(f"| {e.get('date', '')} | {e.get('name', '')} | {e.get('subject', '')} | {score} |")
        print()

    print("## 每次考试各类错因丢分\n")
    print("| 考试 | " + " | ".join(CATEGORIES) + " | 其他 | 合计 |")
    print("|---|" + "---|" * (len(CATEGORIES) + 2))
    by_exam = defaultdict(lambda: defaultdict(float))
    for m in mistakes:
        cat = m["category"] if m["category"] in CATEGORIES else "其他"
        by_exam[exam_label(m)][cat] += to_points(m["points_lost"])
    for _, label in exam_labels:
        cells = [fmt_points(by_exam[label][c]) for c in CATEGORIES + ["其他"]]
        print(f"| {label} | " + " | ".join(cells) + f" | {fmt_points(sum(by_exam[label].values()))} |")
    print()

    # 知识点归类：主知识点 + 相关知识点
    groups = defaultdict(lambda: {"exams": set(), "dates": set(), "primary": [], "related": [], "points": 0.0, "open": 0})
    for m in mistakes:
        keys = []
        if m.get("kp_id"):
            keys.append((m["kp_id"], "primary"))
        elif m.get("kp_name"):
            keys.append(("名称:" + m["kp_name"], "primary"))
        for kp in m.get("related_kp_ids") or []:
            keys.append((kp, "related"))
        for key, role in keys:
            g = groups[key]
            g["exams"].add(exam_label(m))
            g["dates"].add(m["exam_date"])
            g[role].append(f"{exam_label(m)} 第{m['question']}题")
            if role == "primary":
                g["points"] += to_points(m["points_lost"])
                if m["status"] != MASTERED:
                    g["open"] += 1

    def describe(key):
        if key.startswith("名称:"):
            return key[3:], "（未匹配知识点地图，按名称归类）", ""
        info = lookup_kp(key) or {}
        return info.get("name", key), info.get("unit", ""), info.get("location", "")

    print("## 可能没掌握的知识点（在 2 次及以上考试中出错）\n")
    weak = [k for k, g in groups.items() if len(g["exams"]) >= 2]
    if weak:
        weak.sort(key=lambda k: (-len(groups[k]["exams"]), -groups[k]["points"]))
        print("| 知识点 | id | 单元 | 回到教材 | 出现次数 | 证据 | 累计丢分 | 未掌握题数 | 本次出现 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for k in weak:
            g = groups[k]
            name, unit, loc = describe(k)
            evidence = "；".join(g["primary"] + [s + "（相关）" for s in g["related"]])
            kp_id = "" if k.startswith("名称:") else k
            print(f"| {name} | {kp_id} | {unit} | {loc} | {len(g['exams'])} | {evidence} | {fmt_points(g['points'])} | {g['open']} | {'是' if latest in g['dates'] else ''} |")
    else:
        print("暂时没有在两次及以上考试中重复出错的知识点。")
    print()

    print("## 反复出错的单元（2 次及以上考试、3 道及以上错题）\n")
    units = defaultdict(lambda: {"exams": set(), "count": 0, "kps": set()})
    for m in mistakes:
        u = unit_of(m.get("kp_id"))
        if not u:
            continue
        units[u]["exams"].add(exam_label(m))
        units[u]["count"] += 1
        units[u]["kps"].add(m.get("kp_name") or m["kp_id"])
    weak_units = [u for u, v in units.items() if len(v["exams"]) >= 2 and v["count"] >= 3]
    if weak_units:
        print("| 单元 | 单元 id | 考试次数 | 错题数 | 涉及知识点 |")
        print("|---|---|---|---|---|")
        for u in sorted(weak_units, key=lambda u: -units[u]["count"]):
            v = units[u]
            print(f"| {unit_name(u)} | {u} | {len(v['exams'])} | {v['count']} | {'、'.join(sorted(v['kps']))} |")
    else:
        print("暂时没有反复出错的单元。")
    print()

    print("## 最近一次考试涉及的知识点\n")
    latest_rows = [m for m in mistakes if m["exam_date"] == latest]
    print("| 题号 | 知识点 | id | 回到教材 | 以前出错次数 |")
    print("|---|---|---|---|---|")
    for m in latest_rows:
        key = m.get("kp_id") or ("名称:" + m.get("kp_name", ""))
        before = len([d for d in groups[key]["dates"] if d < latest]) if key in groups else 0
        loc = (lookup_kp(m.get("kp_id")) or {}).get("location", "")
        print(f"| {m['question']} | {m.get('kp_name', '')} | {m.get('kp_id', '')} | {loc} | {before} |")
    print()

    print("## 还没掌握的题\n")
    open_by_subject = defaultdict(lambda: defaultdict(int))
    for m in mistakes:
        if m["status"] != MASTERED:
            open_by_subject[m["subject"]][m["status"] or TODO] += 1
    if open_by_subject:
        print("| 科目 | 待重做 | 二刷 | 合计 |")
        print("|---|---|---|---|")
        for subj in sorted(open_by_subject):
            d = open_by_subject[subj]
            print(f"| {subj} | {d[TODO]} | {d[SECOND]} | {d[TODO] + d[SECOND]} |")
    else:
        print("所有错题都已掌握。")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    today = date.today().isoformat()
    parser = argparse.ArgumentParser(description="错题档案工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="新建档案或更新学生信息")
    p.add_argument("--archive", required=True)
    p.add_argument("--stage", help="初中 / 高中")
    p.add_argument("--grade", help="例如 高三")
    p.add_argument("--textbook", action="append", help="科目=教材版本，可重复")
    p.add_argument("--today", default=today)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("kp-search", help="在知识点地图里查知识点 ID")
    p.add_argument("--subject", required=True)
    p.add_argument("--stage", help="初中 / 高中")
    p.add_argument("--q", action="append", help="关键词，可重复")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_kp_search)

    p = sub.add_parser("merge", help="写入本次考试和错题")
    p.add_argument("--archive", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--today", default=today)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("update", help="记录一次闭卷重做的结果")
    p.add_argument("--archive", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--result", required=True, choices=["pass", "fail"])
    p.add_argument("--today", default=today)
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("due", help="列出到期需要复习的题")
    p.add_argument("--archive", required=True)
    p.add_argument("--subject")
    p.add_argument("--today", default=today)
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("report", help="跨考试报告")
    p.add_argument("--archive", required=True)
    p.add_argument("--subject")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

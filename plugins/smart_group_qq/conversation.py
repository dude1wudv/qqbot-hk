"""Consent-owned expression cards and unfinished topics in character_items.

No passive profiling or background sending. Context reads do not spend cooldown;
only a successfully delivered answer that actually mentions an item does.
"""

from __future__ import annotations

import json
import re
import time


KINDS = {"表达库": "expression", "学表达": "expression", "忘表达": "expression",
         "待续": "thread", "话题": "thread", "结束话题": "thread", "忘话题": "thread"}
SERIOUS = re.compile(r"认真|严肃|别开玩笑|不要玩梗|别玩梗|排障|报错|急救|危险|不要追问|别再问")
SCENES = {
    "庆祝": re.compile(r"成功|搞定|做完|完成|通过|太好了"),
    "吐槽": re.compile(r"翻车|又挂|崩了|无语|离谱|烦死|失败"),
    "鼓励": re.compile(r"加油|坚持|好难|没信心|不敢|再试"),
    "感谢": re.compile(r"谢谢|感谢|多谢"),
    "晚安": re.compile(r"晚安|睡了|睡觉"),
}
_STOP = {"这个", "那个", "问题", "事情", "我们", "你们", "一下", "还是", "怎么", "今天", "昨天", "还没", "没有", "已经"}
_NEGATED_SUCCESS = re.compile(r"(?:没有|还没|尚未|并未|不能|不算|没|未|不)(?:能|有)?(?:成功|搞定|做完|完成|通过)")


def terms(text):
    # Overlapping CJK bigrams allow a changed sentence boundary to retain relevance.
    words = set(re.findall(r"[a-z0-9_]{2,}", text.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words - _STOP


def related(query, topic):
    common = terms(query) & terms(topic)
    return len(common) >= 2 or any(len(word) >= 4 and word.isascii() for word in common)


def metadata(row):
    try:
        value = json.loads(row["evidence"])
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def select(rows, owner, query, *, now=None, cooldown=3600):
    now = time.time() if now is None else now
    if not query or re.search(r"不要追问|别再问|别提旧事", query):
        return {"expressions": [], "open_threads": []}
    expressions, threads = [], []
    for row in rows:
        if row["status"] != "open":
            continue
        info = metadata(row)
        if now - info.get("last_used", 0) < cooldown:
            continue
        if row["kind"] == "expression" and not SERIOUS.search(query):
            scene = info.get("scene", "")
            scene_query = _NEGATED_SUCCESS.sub("", query) if scene == "庆祝" else query
            matches = bool(SCENES[scene].search(scene_query)) if scene in SCENES else related(query, scene)
            if matches:
                expressions.append((info.get("uses", 0), row["created"], {"scene": scene, "text": row["text"]}))
        elif row["kind"] == "thread" and row["owner"] == owner and related(query, row["text"]):
            threads.append({"topic": row["text"], "created": row["created"]})
    expressions.sort(key=lambda item: (item[0], -item[1]))
    return {"expressions": [x[2] for x in expressions[:1]], "open_threads": threads[:1]}


def command(character, db, scope, owner, name, arg):
    """Execute within the caller's command/receipt transaction."""
    from .character import safe_text

    kind = KINDS[name]
    rows = character._rows(db, scope, kind)
    if name in {"表达库", "话题"} or (name == "待续" and not arg):
        visible = [r for r in rows if kind == "expression" or r["owner"] == owner]
        return "\n".join(
            f"{r['id']} [{r['status']}] "
            + (f"{metadata(r).get('scene', '')} → " if kind == "expression" else "") + r["text"]
            for r in visible[:20]
        ) or "暂时还没有。用 /学表达 场景 | 短表达 或 /待续 话题内容 添加。"
    if name in {"学表达", "待续"}:
        if name == "学表达":
            scene, separator, phrase = arg.partition("|")
            if not separator or not scene.strip() or not phrase.strip() or len(scene.strip()) > 60 or len(phrase.strip()) > 80:
                return "用法：/学表达 场景 | 短表达。场景最多60字，表达最多80字。例如：/学表达 庆祝 | 土豆起飞了"
            scene, phrase = safe_text(scene, 60), safe_text(phrase, 80)
            info = {"scene": scene, "last_used": 0, "uses": 0}
            payload = phrase
        else:
            if not arg.strip() or len(arg.strip()) > 300:
                return "用法：/待续 话题内容（最多300字）。只在你再次聊到相关内容时续接。"
            payload = safe_text(arg, 300)
            info = {"last_used": 0, "uses": 0}
        # Drop expired cards before insertion so re-adding one gets a fresh
        # creation time and cannot be immediately evicted by the bounded store.
        db.execute("DELETE FROM character_items WHERE scope=? AND kind=? AND expires<=?", (scope, kind, time.time()))
        item = character._add(db, scope, kind, owner, payload, json.dumps(info, ensure_ascii=False), days=90 if kind == "expression" else 7)
        # An explicit new request may reopen/update an existing card, but never
        # resets its cooldown or usage. Expired items receive a fresh lifetime.
        prior = next((r for r in rows if r["id"] == item), None)
        if prior:
            old = metadata(prior)
            info.update(last_used=old.get("last_used", 0), uses=old.get("uses", 0))
        db.execute("UPDATE character_items SET status='open', evidence=?, expires=? WHERE scope=? AND id=? AND owner=?",
                   (json.dumps(info, ensure_ascii=False), time.time() + (90 if kind == "expression" else 7) * 86400, scope, item, owner))
        return f"记下了：{item}。" + ("相关场景才会使用，可用 /忘表达 ID 删除。" if kind == "expression" else "不会定时催问；用 /结束话题 ID 收起，或 /忘话题 ID 删除。")
    matches = [r for r in rows if r["owner"] == owner and (r["id"] == arg or r["text"] == arg)]
    if len(matches) != 1:
        return "没有唯一匹配的本人条目，请用 /表达库 或 /话题 查看后填写 ID。"
    item = matches[0]["id"]
    if name == "结束话题":
        db.execute("UPDATE character_items SET status='done' WHERE scope=? AND id=? AND owner=?", (scope, item, owner))
    else:
        db.execute("DELETE FROM character_items WHERE scope=? AND id=? AND owner=?", (scope, item, owner))
    character.store._advance_memory_epoch(scope)
    return "已处理。"


def record_usage(character, db, scope, owner, query, answer):
    rows = character._rows(db, scope, "expression") + character._rows(db, scope, "thread")
    selected = select(rows, owner, query, cooldown=character.recall_cooldown)
    for row in rows:
        used = (row["kind"] == "expression" and row["text"] in answer
                and any(item["text"] == row["text"] for item in selected["expressions"])) or (
            row["kind"] == "thread" and row["owner"] == owner and related(answer, row["text"])
            and any(item["topic"] == row["text"] for item in selected["open_threads"]))
        if used:
            info = metadata(row)
            info.update(last_used=time.time(), uses=info.get("uses", 0) + 1)
            db.execute("UPDATE character_items SET evidence=? WHERE scope=? AND id=?",
                       (json.dumps(info, ensure_ascii=False), scope, row["id"]))

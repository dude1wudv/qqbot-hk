"""A bounded branching adventure; models narrate but never change game state."""

from __future__ import annotations

import copy
import uuid


def new_story(premise, owner):
    return {
        "version": 2, "id": uuid.uuid4().hex, "premise": premise,
        "owner": owner, "chapter": 0, "node": "entrance", "votes": {},
        "inventory": [], "companions": [], "history": [], "ending": "",
        "text": "入口处，一扇亮着灯的门与通往林间的脚印引向不同方向。",
        "options": ["推门寻找线索", "沿脚印观察周围"],
    }


def advance(story, choice):
    """Return a fresh state, retaining legacy choices when upgrading a saved game."""
    result = copy.deepcopy(story)
    chosen = result["options"][choice - 1]
    if result.get("version") != 2:
        migrated = new_story(result["premise"], result.get("owner", ""))
        migrated["chapter"] = result["chapter"]
        migrated["node"] = "crossroads"
        migrated["text"] = result["text"]
        result = migrated
    result["history"].append({"chapter": result["chapter"], "choice": chosen})
    result["history"] = result["history"][-8:]
    result["chapter"] += 1
    result["votes"] = {}
    result.pop("narration", None)
    node = result["node"]
    bag, friends = result["inventory"], result["companions"]
    if node == "entrance":
        if choice == 1:
            bag.append("旧地图")
            result.update(node="workshop", text="门后是废弃工坊。你们找到一张旧地图，以及一台断电的向导机器人。",
                          options=["修好向导机器人", "带上备用电池离开"])
        else:
            bag.append("发光种子")
            result.update(node="grove", text="脚印尽头，一只小动物被藤蔓困住。路旁的发光种子照亮了岔路。",
                          options=["解救小动物", "沿发光的溪流探路"])
    elif node in {"workshop", "grove"}:
        if node == "workshop":
            (friends if choice == 1 else bag).append("向导机器人" if choice == 1 else "备用电池")
        else:
            (friends if choice == 1 else bag).append("发光小动物" if choice == 1 else "溪流石")
        result.update(node="crossroads", text="你们抵达断桥。刚才的选择留下了新的帮手或物资；对岸的观测塔正在发出微弱信号。",
                      options=["借助伙伴和物资修复断桥", "沿河寻找安全的绕行路线"])
    elif node == "crossroads":
        if choice == 1:
            helper = friends[0] if friends else (bag[0] if bag else "收集的木料")
            bag.append("桥梁徽记")
            consequence = f"借助{helper}，你们让断桥重新连通，获得桥梁徽记。"
        else:
            bag.append("隐秘通道")
            consequence = "绕行花了一些时间，却让你们发现通往观测塔地下的隐秘通道。"
        result.update(node="tower", text=consequence + "塔中有一座熄灭的信标与一间封存的档案室。",
                      options=["尝试点亮信标", "进入档案室寻找真相"])
    elif node == "tower":
        if choice == 1:
            resource = next((x for x in ("备用电池", "发光种子", "溪流石") if x in bag), None)
            if resource:
                bag.remove(resource)
                ending = f"你们用{resource}点亮了灯塔，远处的旅人找到了归途。"
            elif "向导机器人" in friends:
                ending = "向导机器人接通了塔内的旧线路，灯塔重新亮起。曾经伸出的援手带来了回响。"
            else:
                ending = "能源不足，灯塔没能点亮；你们留下清晰的路标，安全带回了维修线索。"
            result["ending"] = "归途之光"
        else:
            key = "隐秘通道" if "隐秘通道" in bag else "桥梁徽记" if "桥梁徽记" in bag else "旧地图"
            ending = f"借助{key}留下的线索，你们读懂了档案，找回这片土地被遗忘的名字。"
            result["ending"] = "失落的名字"
        result.update(node="ended", text=ending, options=[])
    return result


def public_state(story):
    """Never place voter identities, ownership or raw platform IDs in a prompt."""
    if not story:
        return None
    return {key: story[key] for key in (
        "premise", "chapter", "text", "options", "inventory", "companions", "history", "ending"
    ) if key in story}


def render(story):
    lines = [f"【虚构冒险】{story['premise']}", f"第 {story['chapter']} 幕 · {story['text']}"]
    if story.get("narration"):
        lines.append("旁白：" + story["narration"])
    if story.get("inventory"):
        lines.append("行囊：" + "、".join(story["inventory"]))
    if story.get("companions"):
        lines.append("同行：" + "、".join(story["companions"]))
    if story.get("ending"):
        lines.append("结局：" + story["ending"] + "。可用 /结束剧情 收起本次冒险，再开始新剧情。")
    else:
        lines.extend(f"{i}. {option}" for i, option in enumerate(story["options"], 1))
        lines.append("/投票 1 或 2（每人一票，两票且形成多数后推进）")
    return "\n".join(lines)

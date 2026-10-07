"""Deterministic, group-scoped general polls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PollCommand:
    action: str
    poll_id: str = ""
    option_no: int = 0
    question: str = ""
    options: tuple[str, ...] = ()


def parse_poll_command(value: Any) -> PollCommand | None:
    text = str(value or "").replace("／", "/").strip()
    if not text.startswith("/群投票"):
        return None
    rest = text[len("/群投票"):].strip()
    if not rest:
        return PollCommand("show")
    if rest == "查看":
        return PollCommand("show")
    if rest.startswith("创建"):
        payload = rest[len("创建"):].strip()
        pieces = tuple(item.strip() for item in payload.split("|"))
        if len(pieces) < 3:
            return PollCommand("invalid")
        return PollCommand("create", question=pieces[0], options=pieces[1:])
    pieces = rest.split()
    if len(pieces) == 3 and pieces[0] == "选择":
        try:
            option_no = int(pieces[2])
        except ValueError:
            return PollCommand("invalid")
        return PollCommand("vote", poll_id=pieces[1], option_no=option_no)
    if len(pieces) == 2 and pieces[0] == "结束":
        return PollCommand("close", poll_id=pieces[1])
    return PollCommand("invalid")


class PollService:
    def __init__(self, store):
        self.store = store

    def execute(self, group_id: str, member_id: str, command: PollCommand) -> str:
        if command.action == "invalid":
            return (
                "群投票用法：/群投票 创建 题目 | 选项A | 选项B；"
                "/群投票 查看，/群投票 选择 POLL_ID OPTION_NO，/群投票 结束 POLL_ID"
            )
        result = self.store.apply_group_poll(
            group_id, member_id, command.action,
            question=command.question, options=command.options,
            poll_id=command.poll_id, option_no=command.option_no,
        )
        if result.get("error"):
            return str(result["error"])
        return format_poll(result)


def format_poll(poll: dict[str, Any]) -> str:
    status = {"open": "进行中", "closed": "已结束", "expired": "已过期"}.get(
        str(poll.get("status")), str(poll.get("status"))
    )
    lines = [f"【群投票 {poll['poll_id']}｜{status}】", str(poll["question"])]
    total = 0
    for option in poll.get("options", ()):
        votes = int(option.get("votes", 0))
        total += votes
        lines.append(f"{option['option_no']}. {option['text']}（{votes}票）")
    lines.append(f"总票数：{total}")
    if status == "进行中":
        lines.append(f"投票：/群投票 选择 {poll['poll_id']} 选项编号")
    return "\n".join(lines)


__all__ = ["PollCommand", "PollService", "format_poll", "parse_poll_command"]

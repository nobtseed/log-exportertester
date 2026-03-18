"""qq-chat-exporter JSON -> 灰山城日志生成脚本

读取 QQChatExporter 导出的群聊 JSON（v5.x），生成两份文本：
- 灰山城行程日志-<mmddhhmm-mmddhhmm>.txt（剔除OOC整段 + 删除括号内OOC片段）
- 灰山城完整记录-<mmddhhmm-mmddhhmm>.txt（原始文本）

用法示例：
  python tools/qq_chat_exporter_to_log.py d:/Downloads/group_xxx.json --out data/digital_ghost/logs

说明：
- 识别规则对齐 Napbot 插件的 log_manager：
  . 开头：指令； # 开头：动作； 全角（）整段：场外； "" 包裹：分身； 【】 包裹：患者；默认：对话
- Bot 自身消息：sender.uin == chatInfo.selfUin 或 message.system==true
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False


class MessageType(Enum):
    GHOST = "分身"
    PC = "患者"
    ACTION = "动作"
    OOC = "场外"
    COMMAND = "指令"
    SYSTEM = "系统"
    DIALOGUE = "对话"


@dataclass
class LogLine:
    timestamp: datetime
    user_id: str
    username: str
    msg_type: MessageType
    content: str
    raw_content: str

    def is_plot_content(self) -> bool:
        if self.msg_type in {MessageType.OOC, MessageType.COMMAND, MessageType.SYSTEM}:
            return False
        return bool(self.content and self.content.strip())

    def fmt_plot(self) -> str:
        t = self.timestamp.strftime("%H:%M")
        return f"[{t}] [{self.msg_type.value}] {self.username}\n{self.content}\n"

    def fmt_full(self) -> str:
        t = self.timestamp.strftime("%H:%M")
        return f"[{t}] [{self.msg_type.value}] {self.username}\n{self.raw_content}\n"


def _parse_time(msg: dict) -> datetime:
    # QQChatExporter 通常提供 "time": "YYYY-MM-DD HH:MM:SS"
    time_str = msg.get("time")
    if isinstance(time_str, str) and time_str.strip():
        try:
            return datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    ts = msg.get("timestamp")
    if isinstance(ts, (int, float)):
        # timestamp 是毫秒
        return datetime.fromtimestamp(ts / 1000.0)

    return datetime.now()


def _extract_sender(msg: dict) -> Tuple[str, str]:
    sender = msg.get("sender") or {}
    # uin 更像 QQ 号；uid 是 exporter 内部 uid
    user_id = str(sender.get("uin") or sender.get("uid") or "未知")
    username = str(sender.get("name") or "未知")
    return user_id, username


def _extract_text(msg: dict) -> str:
    content = msg.get("content") or {}
    text = content.get("text")
    if isinstance(text, str):
        return text.strip()

    # 兜底：拼 elements
    elements = content.get("elements") or []
    parts: List[str] = []
    if isinstance(elements, list):
        for el in elements:
            if not isinstance(el, dict):
                continue
            t = el.get("type")
            data = el.get("data") or {}
            if t == "text" and isinstance(data.get("text"), str):
                parts.append(data["text"])
            elif t == "at":
                name = data.get("name")
                parts.append(f"@{name}" if name else "@某人")
            elif t == "image" and isinstance(data.get("filename"), str):
                parts.append(f"[图片: {data['filename']}]"
                )
            elif t == "reply" and isinstance(data.get("content"), str):
                # reply 的 content 往往已经是可读字符串
                parts.append(data["content"])

    return "".join(parts).strip()


def _classify(content: str, *, sender_uin: str, bot_uin: Optional[str], is_system_msg: bool) -> MessageType:
    if is_system_msg:
        return MessageType.SYSTEM
    if bot_uin and sender_uin == bot_uin:
        return MessageType.SYSTEM

    stripped = (content or "").strip()
    if not stripped:
        return MessageType.DIALOGUE

    if stripped.startswith("."):
        return MessageType.COMMAND
    if stripped.startswith("（") and stripped.endswith("）"):
        return MessageType.OOC
    if stripped.startswith('"') and stripped.endswith('"'):
        return MessageType.GHOST
    if stripped.startswith("【") and stripped.endswith("】"):
        return MessageType.PC
    if stripped.startswith("#"):
        return MessageType.ACTION
    return MessageType.DIALOGUE


_OOC_INLINE_RE = re.compile(r"（[^）]*）")


def _process_for_plot(content: str, msg_type: MessageType) -> str:
    if msg_type == MessageType.OOC:
        return ""

    processed = _OOC_INLINE_RE.sub("", content or "")
    if msg_type == MessageType.ACTION and processed.strip().startswith("#"):
        processed = processed.strip()[1:]
    return processed.strip()


def _time_range_safe(start: datetime, end: datetime) -> str:
    # 对齐插件：mm/dd/hh:mm-mm/dd/hh:mm -> 去掉 / :
    start_str = start.strftime("%m/%d/%H:%M")
    end_str = end.strftime("%m/%d/%H:%M")
    return f"{start_str}-{end_str}".replace("/", "").replace(":", "")


def _render_plot(lines: List[LogLine], start: datetime, end: datetime) -> str:
    plot_lines = [ln for ln in lines if ln.is_plot_content()]
    participants = sorted({ln.username for ln in lines if ln.msg_type != MessageType.SYSTEM})

    header = [
        "=== 灰山城系统自动生成 ===",
        f"记录时段：{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%H:%M')}",
        f"参与信号：{', '.join(participants)}" if participants else "参与信号：无",
        f"信号总数：{len(plot_lines)}",
        "===================================",
        "",
    ]
    body = [ln.fmt_plot() for ln in plot_lines]
    footer = [
        "===================================",
        f"记录结束 | 共 {len(plot_lines)} 条消息",
    ]
    return "\n".join(header + body + footer)


def _render_full(lines: List[LogLine], start: datetime, end: datetime) -> str:
    participants = sorted({ln.username for ln in lines if ln.msg_type != MessageType.SYSTEM})
    command_count = sum(1 for ln in lines if ln.msg_type == MessageType.COMMAND)

    header = [
        "=== 灰山城系统自动生成（完整档案）===",
        f"记录时段：{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%H:%M')}",
        f"参与信号：{', '.join(participants)}" if participants else "参与信号：无",
        f"总消息数：{len(lines)}（含指令 {command_count} 条）",
        "===================================",
        "",
    ]
    body = [ln.fmt_full() for ln in lines]
    footer = [
        "===================================",
        f"记录结束 | 总计 {len(lines)} 条消息",
    ]
    return "\n".join(header + body + footer)


def main() -> int:
    ap = argparse.ArgumentParser(description="QQChatExporter JSON 转灰山城日志")
    ap.add_argument("json_path", nargs="?", default=None, help="QQChatExporter 导出的 .json 文件路径")
    ap.add_argument("--out", default="data/digital_ghost/logs", help="输出目录（默认：data/digital_ghost/logs）")
    ap.add_argument("--prefix", default="灰山城", help="文件名前缀（默认：灰山城）")
    ap.add_argument("--include-system", action="store_true", help="在完整记录中也保留 exporter 标记的 system 消息")
    ap.add_argument("--bot-uin", default=None, help="Bot QQ 号（默认从 chatInfo.selfUin 读取）")
    args = ap.parse_args()

    # 如果未提供 json_path，尝试打开文件选择窗口
    json_path_str = args.json_path
    if not json_path_str:
        if HAS_TKINTER:
            try:
                root = tk.Tk()
                root.withdraw()  # 隐藏主窗口
                root.attributes('-topmost', True)  # 置顶
                json_path_str = filedialog.askopenfilename(
                    title="选择 QQChatExporter 导出的 JSON 文件",
                    filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
                    initialdir="."
                )
                root.destroy()
                if not json_path_str:
                    print("未选择文件，已取消")
                    return 1
            except Exception as e:
                print(f"文件选择窗口失败: {e}")
                print("使用方法: python tools/qq_chat_exporter_to_log.py <json_path>")
                return 1
        else:
            print("错误: 未提供 JSON 文件路径")
            print("使用方法: python tools/qq_chat_exporter_to_log.py <json_path>")
            print("  或: python tools/qq_chat_exporter_to_log.py d:/Downloads/group_xxx.json --out data/digital_ghost/logs")
            return 1

    json_path = Path(json_path_str)
    if not json_path.exists():
        print(f"错误: 文件不存在 - {json_path}")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    root = json.loads(json_path.read_text(encoding="utf-8"))

    chat_info = root.get("chatInfo") or {}
    bot_uin = args.bot_uin or (str(chat_info.get("selfUin")) if chat_info.get("selfUin") is not None else None)

    msgs = root.get("messages")
    if not isinstance(msgs, list) or not msgs:
        print("JSON 中未找到 messages 数组或为空")
        return 1


    lines: List[LogLine] = []

    for m in msgs:
        if not isinstance(m, dict):
            continue

        is_system_msg = bool(m.get("system"))
        if is_system_msg and (not args.include_system):
            # 默认跳过 exporter 自己塞的 system 消息（例如 [17] 之类）
            continue

        ts = _parse_time(m)
        sender_uin, sender_name = _extract_sender(m)
        text = _extract_text(m)
        msg_type = _classify(text, sender_uin=sender_uin, bot_uin=bot_uin, is_system_msg=is_system_msg)
        plot_text = _process_for_plot(text, msg_type)

        lines.append(
            LogLine(
                timestamp=ts,
                user_id=sender_uin,
                username=sender_name,
                msg_type=msg_type,
                content=plot_text,
                raw_content=text,
            )
        )

    if not lines:
        raise SystemExit("过滤后无可用消息")
        print("过滤后无可用消息")
        return 1

    lines.sort(key=lambda x: x.timestamp)
    start = lines[0].timestamp
    end = lines[-1].timestamp

    time_range = _time_range_safe(start, end)
    plot_name = f"{args.prefix}行程日志-{time_range}.txt"
    full_name = f"{args.prefix}完整记录-{time_range}.txt"

    plot_path = out_dir / plot_name
    full_path = out_dir / full_name

    plot_path.write_text(_render_plot(lines, start, end), encoding="utf-8")
    full_path.write_text(_render_full(lines, start, end), encoding="utf-8")

    print(f"OK: {plot_path}")
    print(f"OK: {full_path}")
    return 0


if __name__ == "__main__":
    import sys
    ret = main()
    if ret == 0:
        print("保存完成，按任意键退出...", end="", flush=True)
    else:
        print("程序出错，按任意键退出...", end="", flush=True)

    # 跨平台等待按键
    try:
        import msvcrt
        msvcrt.getch()          # Windows 无回显等待任意键
        print()                  # 输出换行
    except ImportError:
        input()                  # 其他系统等待回车键

    sys.exit(ret)

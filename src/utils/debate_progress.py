"""Debate进度管理模块"""

from typing import Optional, List, Dict
from dataclasses import dataclass
from enum import Enum


class DebateStage(Enum):
    """Debate阶段枚举"""
    EXTRACTING_TEXT = "提取文本"
    GENERATING_SUMMARY = "生成总结"
    DEBATE_QUESTION = "诘问"
    DEBATE_RESPONSE = "回应"


@dataclass
class StageInfo:
    """阶段信息"""
    name: str
    status: str = "pending"
    current_agent: str = ""
    stream_content: str = ""


class DebateProgress:
    """Debate进度管理器"""

    def __init__(self, paper_name: str, debate_rounds: int, debate_agents: List[str], summary_api: str):
        """
        初始化Debate进度管理器

        Args:
            paper_name: 论文名称
            debate_rounds: debate轮数
            debate_agents: debate agent列表
            summary_api: 总结API名称
        """
        self.paper_name = paper_name
        self.debate_rounds = debate_rounds
        self.debate_agents = debate_agents
        self.summary_api = summary_api
        self.current_round = 0
        self.current_stage: DebateStage = DebateStage.EXTRACTING_TEXT
        self.stages: Dict[DebateStage, StageInfo] = {
            DebateStage.EXTRACTING_TEXT: StageInfo(name="提取文本"),
            DebateStage.GENERATING_SUMMARY: StageInfo(name="生成总结"),
            DebateStage.DEBATE_QUESTION: StageInfo(name="诘问"),
            DebateStage.DEBATE_RESPONSE: StageInfo(name="回应"),
        }
        self.debate_history: List[str] = []

    def set_stage(self, stage: DebateStage, status: str = "active", agent: str = ""):
        """
        设置当前阶段

        Args:
            stage: 阶段枚举
            status: 状态 (pending/active/completed/failed)
            agent: 当前agent名称
        """
        self.current_stage = stage
        self.stages[stage].status = status
        self.stages[stage].current_agent = agent

    def update_stream(self, content_piece: str):
        """
        更新流式内容

        Args:
            content_piece: 新增的内容片段
        """
        self.stages[self.current_stage].stream_content += content_piece

    def complete_stage(self):
        """完成当前阶段"""
        self.stages[self.current_stage].status = "completed"

    def fail_stage(self):
        """标记当前阶段失败"""
        self.stages[self.current_stage].status = "failed"

    def next_round(self):
        """进入下一轮"""
        self.current_round += 1

    def get_progress_summary(self) -> str:
        """
        获取进度摘要

        Returns:
            进度摘要字符串
        """
        summary_lines = [
            f"处理文件: {self.paper_name}",
            f"当前阶段: {self.stages[self.current_stage].name}",
        ]

        if self.current_round > 0:
            summary_lines.append(f"Debate轮次: {self.current_round}/{self.debate_rounds}")

        if self.stages[self.current_stage].current_agent:
            summary_lines.append(f"当前Agent: {self.stages[self.current_stage].current_agent}")

        return " | ".join(summary_lines)

    def format_stage_display(self, stage: DebateStage) -> str:
        """
        格式化阶段显示

        Args:
            stage: 阶段枚举

        Returns:
            阶段显示字符串
        """
        info = self.stages[stage]
        status_icons = {
            "pending": "○",
            "active": "◐",
            "completed": "●",
            "failed": "✗"
        }
        icon = status_icons.get(info.status, "○")

        display = f"{icon} {info.name}"
        if info.current_agent:
            display += f" ({info.current_agent})"
        if info.status == "active" and info.stream_content:
            preview = info.stream_content[-50:] if len(info.stream_content) > 50 else info.stream_content
            display += f": ...{preview}"
        return display

    def get_full_display(self) -> List[str]:
        """
        获取完整的显示信息

        Returns:
            显示信息列表
        """
        lines = [
            f"╔══ 处理: {self.paper_name} ══╗",
        ]

        for stage in DebateStage:
            lines.append(self.format_stage_display(stage))

        if self.current_round > 0:
            lines.append(f"Debate进度: 第{self.current_round}轮/{self.debate_rounds}轮")

        lines.append("╚" + "═" * 30 + "╝")
        return lines

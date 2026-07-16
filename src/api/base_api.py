"""基础API接口类 - 定义统一的API接口规范"""

from abc import ABC, abstractmethod
from typing import Optional
from src.prompts.prompt_templates import AnalysisMode, prompt_manager


class BaseAPI(ABC):
    """基础API接口类
    
    所有AI API类都应该继承此类，实现统一接口
    """
    
    def __init__(self, api_key: str = None, mode: AnalysisMode = AnalysisMode.PAPER_SUMMARY):
        """
        初始化基础API
        
        Args:
            api_key: API密钥
            mode: 分析模式，默认为论文总结模式
        """
        self.api_key = api_key
        self.mode = mode
        self.prompt_template = prompt_manager.get_template(mode)
    
    def set_mode(self, mode: AnalysisMode):
        """
        设置分析模式
        
        Args:
            mode: 分析模式
        """
        self.mode = mode
        self.prompt_template = prompt_manager.get_template(mode)
    
    @abstractmethod
    def analyze_content(self, content: str, title: str = "", pbar=None) -> str:
        """
        分析内容
        
        Args:
            content: 文本内容
            title: 标题
            pbar: tqdm进度条对象
            
        Returns:
            分析结果
        """
        pass
    
    @abstractmethod
    def debate_content(self, title: str, analysis: str, previous_debate: str = None, pbar=None) -> str:
        """
        对分析内容进行质询
        
        Args:
            title: 标题
            analysis: 分析内容
            previous_debate: 之前的辩论内容
            pbar: tqdm进度条对象
            
        Returns:
            质询结果
        """
        pass
    
    @abstractmethod
    def respond_to_debate(self, title: str, analysis: str, debate_content: str, pbar=None) -> str:
        """
        回应质询
        
        Args:
            title: 标题
            analysis: 分析内容
            debate_content: 质询内容
            pbar: tqdm进度条对象
            
        Returns:
            回应结果
        """
        pass
    
    def analyze_paper(self, paper_text: str, paper_title: str = "", pbar=None) -> str:
        """
        分析论文（兼容旧接口）
        
        Args:
            paper_text: 论文文本
            paper_title: 论文标题
            pbar: tqdm进度条对象
            
        Returns:
            分析结果
        """
        return self.analyze_content(paper_text, paper_title, pbar)
    
    def debate_paper_summary(self, paper_title: str, paper_summary: str, previous_debate: str = None, pbar=None) -> str:
        """
        对论文总结进行质询（兼容旧接口）
        
        Args:
            paper_title: 论文标题
            paper_summary: 论文总结
            previous_debate: 之前的辩论内容
            pbar: tqdm进度条对象
            
        Returns:
            质询结果
        """
        return self.debate_content(paper_title, paper_summary, previous_debate, pbar)

"""API工厂类 - 用于创建和管理不同的AI API实例"""

from src.api.deepseek_api import DeepSeekAPI
from src.api.qianwen_api import QianWenAPI
from src.api.glm_api import GLMAPI
from src.prompts.prompt_templates import AnalysisMode


class APIFactory:
    """API工厂类"""
    
    @staticmethod
    def create_api(provider, **kwargs):
        """
        创建API实例
        
        Args:
            provider: API提供商 (deepseek, qianwen, glm)
            **kwargs: 其他参数
                - api_key: API密钥
                - api_mode: DeepSeek API模式 (chat 或 reasoning)
                - analysis_mode: 分析模式 (AnalysisMode枚举)
                
        Returns:
            API实例
        """
        analysis_mode = kwargs.get('analysis_mode', AnalysisMode.PAPER_SUMMARY)
        
        if provider == "deepseek":
            return DeepSeekAPI(
                api_key=kwargs.get('api_key'),
                api_mode=kwargs.get('api_mode', 'reasoning'),
                analysis_mode=analysis_mode
            )
        elif provider == "qianwen":
            return QianWenAPI(
                api_key=kwargs.get('api_key'),
                analysis_mode=analysis_mode
            )
        elif provider == "glm":
            return GLMAPI(
                api_key=kwargs.get('api_key'),
                analysis_mode=analysis_mode
            )
        else:
            raise ValueError(f"不支持的API提供商: {provider}")
    
    @staticmethod
    def get_analysis_mode(mode_str: str) -> AnalysisMode:
        """
        根据字符串获取分析模式枚举
        
        Args:
            mode_str: 模式字符串
            
        Returns:
            AnalysisMode枚举值
        """
        mode_map = {
            'paper_summary': AnalysisMode.PAPER_SUMMARY,
            'research_proposal': AnalysisMode.RESEARCH_PROPOSAL,
            'experiment_design': AnalysisMode.EXPERIMENT_DESIGN
        }
        
        if mode_str not in mode_map:
            raise ValueError(f"不支持的分析模式: {mode_str}，支持的选项: {list(mode_map.keys())}")
        
        return mode_map[mode_str]

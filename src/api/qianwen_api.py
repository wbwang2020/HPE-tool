"""千问API调用类"""

import time
from openai import OpenAI
from config import QIANWEN_API_KEY, MAX_RETRIES, RETRY_DELAY, DEFAULT_QIANWEN_MODEL, QIANWEN_BASE_URL, QIANWEN_MAX_TOKENS, QIANWEN_TEMPERATURE, QIANWEN_TOP_P, QIANWEN_REPETITION_PENALTY, QIANWEN_PRESENCE_PENALTY
from src.utils.file_utils import FileUtils
from src.api.base_api import BaseAPI
from src.prompts.prompt_templates import AnalysisMode


class QianWenAPI(BaseAPI):
    """千问API调用类"""

    def __init__(self, api_key=None, analysis_mode=AnalysisMode.PAPER_SUMMARY, model=None):
        """
        初始化千问API客户端

        Args:
            api_key: 千问API密钥
            analysis_mode: 分析模式，默认为论文总结模式
            model: 模型名称，默认使用配置文件中的DEFAULT_QIANWEN_MODEL
        """
        super().__init__(api_key or QIANWEN_API_KEY, analysis_mode)
        self.model = model or DEFAULT_QIANWEN_MODEL
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=QIANWEN_BASE_URL
        )

    def test_api(self, prompt: str) -> str:
        """测试千问API访问"""
        messages = [
            {"role": "system", "content": "你是一个智能助手，负责回答用户的问题。"},
            {"role": "user", "content": prompt}
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=QIANWEN_MAX_TOKENS,
            temperature=QIANWEN_TEMPERATURE,
            top_p=QIANWEN_TOP_P,
            presence_penalty=QIANWEN_PRESENCE_PENALTY
        )

        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content
        else:
            return "API响应中没有choices字段"

    def analyze_content(self, content: str, title: str = "", pbar=None) -> str:
        """分析内容"""
        FileUtils.write_message(f"开始使用千问分析: {title}", pbar)
        FileUtils.write_message(f"文本长度: {len(content)} 字符", pbar)

        prompt = self.prompt_template.analyze_prompt(content, title)

        messages = [
            {"role": "system", "content": "你是一位专业的学术分析专家，擅长对各种学术内容进行深入分析和总结。"},
            {"role": "user", "content": prompt}
        ]

        return self._call_api_stream(messages, pbar, "分析", max_tokens=QIANWEN_MAX_TOKENS)

    def debate_content(self, title: str, analysis: str, previous_debate: str = None, pbar=None) -> str:
        """对分析内容进行质询"""
        FileUtils.write_message(f"开始使用千问对 {title} 进行质询...", pbar)

        prompt = self.prompt_template.debate_prompt(title, analysis, previous_debate)

        messages = [
            {"role": "system", "content": "你是一位严谨的学术评审专家，擅长对学术内容进行深入的质询和分析。"},
            {"role": "user", "content": prompt}
        ]

        return self._call_api_stream(messages, pbar, "质询", max_tokens=QIANWEN_MAX_TOKENS)

    def respond_to_debate(self, title: str, analysis: str, debate_content: str, pbar=None) -> str:
        """回应质询"""
        FileUtils.write_message(f"开始使用千问回应 {title} 的质询...", pbar)

        prompt = self.prompt_template.respond_prompt(title, analysis, debate_content)

        messages = [
            {"role": "system", "content": "你是一位专业的学术分析专家，擅长对学术内容进行深入分析和回应。"},
            {"role": "user", "content": prompt}
        ]

        return self._call_api_stream(messages, pbar, "回应", max_tokens=QIANWEN_MAX_TOKENS)

    def _call_api_stream(self, messages: list, pbar, action_name: str, max_tokens: int = QIANWEN_MAX_TOKENS, temperature: float = QIANWEN_TEMPERATURE, top_p: float = QIANWEN_TOP_P, presence_penalty: float = QIANWEN_PRESENCE_PENALTY) -> str:
        """
        流式调用API

        Args:
            messages: 消息列表
            pbar: tqdm进度条对象
            action_name: 操作名称
            max_tokens: 最大token数
            temperature: 温度参数
            top_p: 核采样概率阈值
            presence_penalty: 控制整个序列中的重复度

        Returns:
            API响应内容
        """
        max_retries = MAX_RETRIES
        retry_count = 0

        while retry_count < max_retries:
            try:
                FileUtils.write_message(f"正在调用千问API {action_name} (尝试 {retry_count + 1}/{max_retries})...", pbar)

                full_content = ""

                if pbar:
                    pbar.write(f"    └── Stream output: ")

                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    presence_penalty=presence_penalty,
                    stream=True
                )

                for chunk in stream:
                    if chunk.choices and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            content_piece = delta.content
                            full_content += content_piece
                            if pbar:
                                try:
                                    pbar.write(content_piece, end='')
                                except UnicodeEncodeError:
                                    # 处理编码问题，替换无法显示的字符
                                    safe_content = content_piece.encode('utf-8', 'replace').decode('gbk', 'replace')
                                    pbar.write(safe_content, end='')

                if pbar:
                    pbar.write("")

                if full_content:
                    FileUtils.write_message(f"{action_name}结果长度: {len(full_content)} 字符", pbar)
                    return full_content
                else:
                    FileUtils.write_message(f"API响应为空，尝试同步调用...", pbar)
                    return self._call_api_sync(messages, pbar, action_name, max_tokens, temperature, top_p, presence_penalty)

            except Exception as e:
                FileUtils.write_message(f"调用千问API失败: {e}", pbar)
                import traceback
                FileUtils.write_message(f"错误详细信息: {traceback.format_exc()}", pbar)
                retry_count += 1
                if retry_count < max_retries:
                    FileUtils.write_message(f"等待{RETRY_DELAY}秒后重试...", pbar)
                    time.sleep(RETRY_DELAY)
                else:
                    FileUtils.write_message(f"已达到最大重试次数 ({max_retries})，放弃重试", pbar)
                    return ""

    def _call_api_sync(self, messages: list, pbar, action_name: str, max_tokens: int = QIANWEN_MAX_TOKENS, temperature: float = QIANWEN_TEMPERATURE, top_p: float = QIANWEN_TOP_P, presence_penalty: float = QIANWEN_PRESENCE_PENALTY) -> str:
        """
        同步调用API（备用方案）

        Args:
            messages: 消息列表
            pbar: tqdm进度条对象
            action_name: 操作名称
            max_tokens: 最大token数
            temperature: 温度参数
            top_p: 核采样概率阈值
            presence_penalty: 控制整个序列中的重复度

        Returns:
            API响应内容
        """
        try:
            FileUtils.write_message(f"使用同步模式调用千问API {action_name}...", pbar)

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty
            )

            if response.choices and len(response.choices) > 0:
                content = response.choices[0].message.content
                FileUtils.write_message(f"{action_name}结果长度: {len(content)} 字符", pbar)
                return content
            else:
                FileUtils.write_message("API响应中没有choices字段", pbar)
                return ""

        except Exception as e:
            FileUtils.write_message(f"同步调用千问API失败: {e}", pbar)
            return ""

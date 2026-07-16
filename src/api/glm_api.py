"""GLM API调用类"""

import requests
import time
import json
from config import GLM_API_KEY, MAX_RETRIES, RETRY_DELAY, DEFAULT_GLM_MODEL, GLM_BASE_URL, GLM_MAX_TOKENS, GLM_TEMPERATURE, GLM_THINKING_ENABLED
from src.utils.file_utils import FileUtils
from src.api.base_api import BaseAPI
from src.prompts.prompt_templates import AnalysisMode


class GLMAPI(BaseAPI):
    """GLM API调用类"""

    def __init__(self, api_key=None, analysis_mode=AnalysisMode.PAPER_SUMMARY, model=None):
        """
        初始化GLM API客户端

        Args:
            api_key: GLM API密钥
            analysis_mode: 分析模式，默认为论文总结模式
            model: 模型名称，默认使用配置文件中的DEFAULT_GLM_MODEL
        """
        super().__init__(api_key or GLM_API_KEY, analysis_mode)
        self.base_url = GLM_BASE_URL
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        self.model = model or DEFAULT_GLM_MODEL

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
        FileUtils.write_message(f"开始使用GLM分析: {title}", pbar)
        FileUtils.write_message(f"文本长度: {len(content)} 字符", pbar)
        FileUtils.write_message("使用GLM-5深度思考模式进行分析...", pbar)

        prompt = self.prompt_template.analyze_prompt(content, title)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一位专业的学术分析专家，擅长对各种学术内容进行深入分析和总结。"},
                {"role": "user", "content": prompt}
            ],
            "thinking": {"type": "enabled" if GLM_THINKING_ENABLED else "disabled"},
            "max_tokens": GLM_MAX_TOKENS,
            "temperature": GLM_TEMPERATURE
        }

        return self._call_api_stream(payload, pbar, "分析")

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
        FileUtils.write_message(f"开始使用GLM对 {title} 进行质询...", pbar)
        FileUtils.write_message("使用GLM-5深度思考模式进行质询...", pbar)

        prompt = self.prompt_template.debate_prompt(title, analysis, previous_debate)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一位严谨的学术评审专家，擅长对学术内容进行深入的质询和分析。"},
                {"role": "user", "content": prompt}
            ],
            "thinking": {"type": "enabled" if GLM_THINKING_ENABLED else "disabled"},
            "max_tokens": GLM_MAX_TOKENS,
            "temperature": GLM_TEMPERATURE
        }

        return self._call_api_stream(payload, pbar, "质询")

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
        FileUtils.write_message(f"开始使用GLM回应 {title} 的质询...", pbar)
        FileUtils.write_message("使用GLM-5深度思考模式进行回应...", pbar)

        prompt = self.prompt_template.respond_prompt(title, analysis, debate_content)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一位专业的学术分析专家，擅长对学术内容进行深入分析和回应。"},
                {"role": "user", "content": prompt}
            ],
            "thinking": {"type": "enabled" if GLM_THINKING_ENABLED else "disabled"},
            "max_tokens": GLM_MAX_TOKENS,
            "temperature": GLM_TEMPERATURE
        }

        return self._call_api_stream(payload, pbar, "回应")

    def _call_api_stream(self, payload: dict, pbar, action_name: str) -> str:
        """
        流式调用API

        Args:
            payload: 请求体
            pbar: tqdm进度条对象
            action_name: 操作名称（用于日志）

        Returns:
            API响应内容
        """
        max_retries = MAX_RETRIES
        retry_count = 0

        while retry_count < max_retries:
            try:
                FileUtils.write_message(f"正在调用GLM API {action_name} (尝试 {retry_count + 1}/{max_retries})...", pbar)

                payload_with_stream = {**payload, "stream": True}

                response = requests.post(self.base_url, headers=self.headers, json=payload_with_stream, timeout=600, stream=True)
                FileUtils.write_message(f"API响应状态码: {response.status_code}", pbar)

                if response.status_code != 200:
                    FileUtils.write_message(f"API响应内容: {response.text}", pbar)
                    response.raise_for_status()

                full_content = ""
                reasoning_content = ""

                if pbar:
                    pbar.write(f"    └── Stream output: ")

                for line in response.iter_lines():
                    if line:
                        line_text = line.decode('utf-8')
                        if line_text.startswith('data: '):
                            data = line_text[6:]
                            if data == '[DONE]':
                                break
                            try:
                                chunk = json.loads(data)
                                if 'choices' in chunk and len(chunk['choices']) > 0:
                                    delta = chunk['choices'][0].get('delta', {})
                                    if 'content' in delta and delta['content']:
                                        content_piece = delta['content']
                                        full_content += content_piece
                                        if pbar:
                                            pbar.write(content_piece, end='')

                                    if 'reasoning_content' in delta and delta['reasoning_content']:
                                        reasoning_piece = delta['reasoning_content']
                                        reasoning_content += reasoning_piece
                            except json.JSONDecodeError:
                                continue

                if pbar:
                    pbar.write("")

                if reasoning_content:
                    FileUtils.write_message(f"深度思考内容长度: {len(reasoning_content)} 字符", pbar)

                if full_content:
                    FileUtils.write_message(f"{action_name}结果长度: {len(full_content)} 字符", pbar)
                    return full_content
                else:
                    FileUtils.write_message(f"API响应为空，尝试同步调用...", pbar)
                    return self._call_api_sync(payload, pbar, action_name)

            except requests.exceptions.Timeout:
                FileUtils.write_message("API调用超时", pbar)
                retry_count += 1
                if retry_count < max_retries:
                    FileUtils.write_message(f"等待{RETRY_DELAY}秒后重试...", pbar)
                    time.sleep(RETRY_DELAY)
                else:
                    FileUtils.write_message(f"已达到最大重试次数 ({max_retries})，放弃重试", pbar)
                    return ""

            except Exception as e:
                FileUtils.write_message(f"调用GLM API失败: {e}", pbar)
                retry_count += 1
                if retry_count < max_retries:
                    FileUtils.write_message(f"等待{RETRY_DELAY}秒后重试...", pbar)
                    time.sleep(RETRY_DELAY)
                else:
                    FileUtils.write_message(f"已达到最大重试次数 ({max_retries})，放弃重试", pbar)
                    return ""

    def _call_api_sync(self, payload: dict, pbar, action_name: str) -> str:
        """
        同步调用API（备用方案）

        Args:
            payload: 请求体
            pbar: tqdm进度条对象
            action_name: 操作名称

        Returns:
            API响应内容
        """
        try:
            FileUtils.write_message(f"使用同步模式调用GLM API {action_name}...", pbar)

            response = requests.post(self.base_url, headers=self.headers, json=payload, timeout=600)
            FileUtils.write_message(f"API响应状态码: {response.status_code}", pbar)

            if response.status_code != 200:
                FileUtils.write_message(f"API响应内容: {response.text}", pbar)
                response.raise_for_status()

            result = response.json()

            if "choices" in result and len(result["choices"]) > 0:
                message = result["choices"][0]["message"]

                if "reasoning_content" in message:
                    reasoning_content = message["reasoning_content"]
                    FileUtils.write_message(f"深度思考内容长度: {len(reasoning_content)} 字符", pbar)

                content = message.get("content", "")
                FileUtils.write_message(f"{action_name}结果长度: {len(content)} 字符", pbar)
                return content
            else:
                FileUtils.write_message("API响应中没有choices字段", pbar)
                return ""

        except Exception as e:
            FileUtils.write_message(f"同步调用GLM API失败: {e}", pbar)
            return ""

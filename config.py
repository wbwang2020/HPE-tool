#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置文件，存储API密钥等信息
"""

import os
from dotenv import load_dotenv

# 加载.env文件
load_dotenv()

# DeepSeek API配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1/chat/completions"

# 千问API配置
QIANWEN_API_KEY = os.getenv("QIANWEN_API_KEY", "")
QIANWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# GLM API配置
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 默认模型
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_QIANWEN_MODEL = "qwen3.7-max"
DEFAULT_GLM_MODEL = "glm-5.2"

# 可选模型（按能力从强到弱排列）
# 千问系列: qwen3.7-max(旗舰) > qwen3.7-plus(均衡多模态) > qwen3.6-flash(轻量)
# GLM系列:   glm-5.2(旗舰/1M上下文) > glm-5.1(上一代/128K)

# GLM API上下文配置
GLM_MAX_TOKENS = 131072  # GLM-5.2支持最大128K输出token
GLM_TEMPERATURE = 1.0  # 官方推荐值
GLM_THINKING_ENABLED = True

# 千问API上下文配置
QIANWEN_MAX_TOKENS = 131072  # qwen3.7-plus支持128K上下文
QIANWEN_TEMPERATURE = 0.7  # 取值范围[0, 2)，不建议为0
QIANWEN_TOP_P = 0.8  # 核采样概率阈值，取值范围(0, 1.0)
QIANWEN_REPETITION_PENALTY = 1.0  # 控制连续序列中的重复度，1.0表示不做惩罚
QIANWEN_PRESENCE_PENALTY = 0.0  # 控制整个序列中的重复度，取值范围[-2.0, 2.0]

# DeepSeek API上下文配置
DEEPSEEK_MAX_TOKENS_REASONING = 200000
DEEPSEEK_MAX_TOKENS_CHAT = 100000
DEEPSEEK_TEMPERATURE = 0.7
DEEPSEEK_THINKING_ENABLED = True
DEEPSEEK_REASONING_EFFORT = "high"

# 默认目录
DEFAULT_DIRECTORY = "."

# 文本长度限制
MAX_TEXT_LENGTH = 200000

# API调用参数
DEFAULT_MAX_TOKENS = 64000
DEFAULT_TEMPERATURE = 0.7

# 重试次数
MAX_RETRIES = 3
RETRY_DELAY = 3  # 秒

# Agent Debate配置
DEFAULT_DEBATE_ROUNDS = 2  # 默认debate轮数
DEFAULT_DEBATE_AGENTS = ["qianwen"]  # 默认用于debate的agent列表，每个agent代表一个API提供商
# 可选的agent列表: deepseek, qianwen, glm

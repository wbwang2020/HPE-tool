# HPE - High Performance Evaluator

## 项目简介

HPE（High Performance Evaluator）是一款高性能学术文献评估工具，通过调用AI API（支持DeepSeek、千问、GLM）对PDF格式的文献/科研计划书/论文方法或实验设计书进行深度分析，并生成结构化的Markdown格式综述报告。其核心亮点是支持多轮Agent Debate功能，模拟学术审稿过程，通过不同AI模型之间的智能辩论来深入挖掘论文的价值和潜在问题。

## 功能特性

- 自动扫描指定目录中的PDF文献（未指定目录则扫描当前所有子目录）
- 递归扫描子目录中的PDF文件
- 提取PDF文本内容
- 调用AI API进行文献分析
- 支持DeepSeek v4-pro的聊天模式和强思考模式（1M上下文）
- 支持GLM-5.2深度思考模式（1M上下文）
- 支持千问API（qwen3.7-plus）
- 生成符合指定格式的Markdown综述报告
- 将综述保存为与原PDF文件名相同的.md文件
- 处理同名文件：当存在同名Markdown文件时，会添加"-新增"后缀
- 处理边界情况：当文件被临时移除时，会跳过处理并继续处理其他文件
- 避免重复处理：当已经存在对应的Markdown文件时，跳过处理
- Agent Debate功能：使用多个AI API进行多轮辩论，对论文总结进行质询和回应
- 多API支持：支持DeepSeek、千问、GLM三种AI API，可自由选择总结和debate API
- 支持多个Agent Debate：可同时使用多个AI API进行多轮辩论
- 智能Debate流程：每轮由debate agent（审稿人）诘问，总结API（作者）回应，模拟学术审稿过程
- 可自定义Debate轮数：用户可根据需要设置Agent Debate的轮数
- 多种分析模式：支持论文总结、研究提案审查、实验设计审查三种模式
- 提示词模板化：AI API调用与提示词配置解耦，便于扩展和维护

## 安装说明

1. 克隆项目到本地
2. 安装依赖包：
   ```bash
   pip install -r requirements.txt
   ```
3. 配置API密钥：
   - 打开`.env`文件，填写API密钥：
   ```
   # DeepSeek API密钥
   DEEPSEEK_API_KEY=your_deepseek_api_key
   
   # 千问API密钥
   QIANWEN_API_KEY=your_qianwen_api_key
   
   # GLM API密钥
   GLM_API_KEY=your_glm_api_key
   ```

## 使用方法

1. 运行主脚本：
   ```bash
   # 扫描当前目录及所有子目录中的PDF文件
   python review_tool.py
   
   # 扫描指定目录及所有子目录中的PDF文件
   python review_tool.py --directory "指定目录"
   
   # 处理单个PDF文件
   python review_tool.py --file "单个PDF文件路径"
   
   # 启用Agent Debate功能
   python review_tool.py --enable-debate
   
   # 使用多个Agent进行Debate（DeepSeek和千问）
   python review_tool.py --enable-debate --summary-api deepseek --debate-agents deepseek,qianwen --debate-rounds 4

   # 使用所有三个API进行多轮Debate
   python review_tool.py --enable-debate --summary-api deepseek --debate-agents deepseek,qianwen,glm --debate-rounds 6

   # 使用研究提案审查模式
   python review_tool.py --analysis-mode research_proposal --summary-api deepseek
   
   # 使用实验设计审查模式
   python review_tool.py --analysis-mode experiment_design --summary-api deepseek --enable-debate --debate-rounds 3

   # 程序作者本机命令示例
   python D:\工作区\Workspace\survey-tool\HPE-survey\review_tool.py --enable-debate --summary-api deepseek --debate-agents qianwen,glm --debate-rounds 1 --directory "指定目录"

   python D:\工作区\Workspace\survey-tool\HPE-survey\review_tool.py --enable-debate --summary-api deepseek --debate-agents qianwen,glm --debate-rounds 1 --file "指定文件"

   python D:\工作区\Workspace\survey-tool\HPE-survey\review_tool.py --analysis-mode research_proposal --enable-debate --summary-api deepseek --debate-agents qianwen,glm --debate-rounds 1 --file "指定文件"
   ```
### Agent Debate流程说明

当启用Agent Debate功能时，系统会模拟学术审稿过程，按照以下流程进行多轮辩论：

1. **总结阶段**：使用`--summary-api`指定的API生成论文总结（模拟作者）
2. **辩论阶段**：每轮辩论包括两个步骤：
   - **多智能体诘问**：由`--debate-agents`指定的所有API依次进行诘问（模拟多个审稿人）
   - **统一回应**：由`--summary-api`指定的API对所有诘问进行统一回应（模拟作者）
3. **循环**：重复上述过程，直到完成`--debate-rounds`指定的轮数

**一轮完整的Debate定义**：
- 所有debate agent（审稿人）依次诘问 → 总结API（作者）统一回应

**多轮Debate流程**：
- 第1轮：debate agents（审稿人）诘问 → summary API（作者）回应
- 第2轮：debate agents（审稿人）基于上一轮回应进行进一步诘问 → summary API（作者）回应
- 第3轮及以后：以此类推，每轮都基于上一轮的回应进行深入讨论

**示例流程**（使用`--summary-api glm --debate-agents deepseek,qianwen --debate-rounds 2`）：
- 第1轮：deepseek（审稿人）诘问 → qianwen（审稿人）诘问 → glm（作者）统一回应
- 第2轮：deepseek（审稿人）基于上一轮回应诘问 → qianwen（审稿人）基于上一轮回应诘问 → glm（作者）统一回应

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--directory` | PDF文献目录 | 当前目录 |
| `--file` | 单个PDF文件路径 | - |
| `--api-key` | DeepSeek API密钥 | .env文件中的值 |
| `--output-dir` | 输出目录 | PDF文件同目录 |
| `--mode` | DeepSeek API模式 | reasoning |
| `--enable-debate` | 启用Agent Debate | False |
| `--debate-rounds` | Debate轮数 | 2 |
| `--debate-agents` | 用于debate的agent列表，用逗号分隔 | qianwen |
| `--summary-api` | 总结API提供商 | deepseek |
| `--qianwen-api-key` | 千问API密钥 | .env文件中的值 |
| `--glm-api-key` | GLM API密钥 | .env文件中的值 |
| `--analysis-mode` | 分析模式 | paper_summary |

**分析模式选项：**
- `paper_summary`：论文总结和质询
- `research_proposal`：论文构思和方法设计路线图审查
- `experiment_design`：实验项目设计路线图审查

**支持的API提供商：** `deepseek`、`qianwen`、`glm`

## 输出格式

生成的Markdown文件包含以下内容：

1. 作者、发表年份、论文标题、期刊/会议名称、卷号、期号、起止页码、DOI号
2. 研究目的和动机分析
3. 核心研究假设或理论构想
4. 研究设计和理论模型构建
5. 数据/样本来源
6. 方法与技术
7. 分析流程
8. 数据分析
9. 核心发现
10. 实验结果
11. 辅助结果
12. 最终结论
13. 研究贡献评价
14. 与研究主题的关联
15. 综述亮点
16. 图表概述
17. 方法评价
18. 疑问和不足
19. 启发和未来方向
20. 代表性参考文献
21. Agent Debate 质询结果（当启用Agent Debate功能时）：包含多个AI API的多轮辩论过程，包括质询、回应和尝试达成一致的过程

## 注意事项

- 请确保网络连接正常，能够访问所选AI API（DeepSeek、千问或GLM）
- 每个PDF文件的文本内容会被限制在200000字符以内，以避免API请求过大
- DeepSeek v4-pro支持1M（百万）token上下文，可处理更长的文档
- 生成的综述报告可能需要根据实际情况进行手动调整
- 对于一些格式复杂的PDF文件，文本提取可能不够完整
- 启用了DeepSeek的思考模式，会生成更深入的分析结果，但可能会增加API调用时间
- 系统会自动处理文件被临时移除的情况，确保处理过程不会中断
- 不同的分析模式会影响生成的综述结构，请根据需求选择合适的模式

## 依赖库

- PyMuPDF：用于PDF文本提取
- pdfplumber：用于PDF文本提取
- PyPDF2：用于PDF文本提取
- requests：用于调用DeepSeek API和GLM API
- openai：用于调用千问API
- python-dotenv：用于加载环境变量
- argparse：用于命令行参数解析
- tqdm：用于显示进度条
- pathlib：用于文件路径处理

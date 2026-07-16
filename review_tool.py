import os
import argparse
from tqdm import tqdm
from pathlib import Path

from config import DEFAULT_DIRECTORY, DEEPSEEK_API_KEY, QIANWEN_API_KEY, GLM_API_KEY, DEFAULT_DEBATE_ROUNDS, DEFAULT_DEBATE_AGENTS
from src.pdf.pdf_processor import PDFProcessor
from src.api.api_factory import APIFactory
from src.utils.file_utils import FileUtils
from src.utils.debate_progress import DebateProgress, DebateStage


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="文献综述工具 - 分析PDF文献并生成Markdown格式的综述")
    parser.add_argument("--directory", type=str, default=DEFAULT_DIRECTORY,
                        help="PDF文献目录")
    parser.add_argument("--file", type=str, default=None,
                        help="单个PDF文件路径")
    parser.add_argument("--api-key", type=str, default=DEEPSEEK_API_KEY,
                        help="DeepSeek API密钥")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="输出目录")
    parser.add_argument("--mode", type=str, default="reasoning",
                        choices=["chat", "reasoning"],
                        help="DeepSeek API模式 (chat 或 reasoning)")
    parser.add_argument("--enable-debate", action="store_true",
                        help="启用Agent Debate功能，使用AI API对论文总结进行质询")
    parser.add_argument("--debate-rounds", type=int, default=DEFAULT_DEBATE_ROUNDS,
                        help=f"Agent Debate的轮数 (默认: {DEFAULT_DEBATE_ROUNDS})")
    parser.add_argument("--debate-agents", type=str, default=",".join(DEFAULT_DEBATE_AGENTS),
                        help=f"用于debate的agent列表，用逗号分隔 (默认: {','.join(DEFAULT_DEBATE_AGENTS)})，可选值: deepseek, qianwen, glm")
    parser.add_argument("--summary-api", type=str, default="deepseek",
                        choices=["deepseek", "qianwen", "glm"],
                        help="用于总结的API提供商")
    parser.add_argument("--qianwen-api-key", type=str, default=QIANWEN_API_KEY,
                        help="千问API密钥")
    parser.add_argument("--glm-api-key", type=str, default=GLM_API_KEY,
                        help="GLM API密钥")
    parser.add_argument("--analysis-mode", type=str, default="paper_summary",
                        choices=["paper_summary", "research_proposal", "experiment_design"],
                        help="分析模式 (paper_summary: 论文总结和质询, research_proposal: 论文构思和方法设计路线图审查, experiment_design: 实验项目设计路线图审查)")

    args = parser.parse_args()

    FileUtils.write_message("=====================================")
    FileUtils.write_message("文献综述工具")
    FileUtils.write_message("=====================================")

    pdf_processor = PDFProcessor()
    analysis_mode = APIFactory.get_analysis_mode(args.analysis_mode)
    FileUtils.write_message(f"分析模式: {args.analysis_mode}")

    summary_api_provider = args.summary_api
    FileUtils.write_message(f"总结API: {summary_api_provider}")

    debate_agents = [agent.strip() for agent in args.debate_agents.split(",") if agent.strip()]
    if not debate_agents:
        debate_agents = [summary_api_provider]
        FileUtils.write_message(f"未指定debate agents，默认使用总结API: {summary_api_provider}")
    FileUtils.write_message(f"Debate Agents: {debate_agents}")
    FileUtils.write_message(f"Debate轮数: {args.debate_rounds}")

    summary_api_kwargs = {
        "analysis_mode": analysis_mode
    }
    if summary_api_provider == "deepseek":
        summary_api_kwargs.update({"api_key": args.api_key, "api_mode": args.mode})
    elif summary_api_provider == "qianwen":
        summary_api_kwargs.update({"api_key": args.qianwen_api_key})
    elif summary_api_provider == "glm":
        summary_api_kwargs.update({"api_key": args.glm_api_key})

    summary_api = APIFactory.create_api(summary_api_provider, **summary_api_kwargs)
    FileUtils.write_message(f"已初始化总结API: {summary_api_provider}")

    debate_apis = {}
    if args.enable_debate:
        for agent in debate_agents:
            debate_api_kwargs = {"analysis_mode": analysis_mode}
            if agent == "deepseek":
                debate_api_kwargs.update({"api_key": args.api_key, "api_mode": args.mode})
            elif agent == "qianwen":
                debate_api_kwargs.update({"api_key": args.qianwen_api_key})
            elif agent == "glm":
                debate_api_kwargs.update({"api_key": args.glm_api_key})
            debate_apis[agent] = APIFactory.create_api(agent, **debate_api_kwargs)
        FileUtils.write_message(f"已启用Agent Debate功能，使用Debate Agents: {debate_agents}")

    if args.file:
        pdf_files = [args.file]
        FileUtils.write_message(f"处理单个文件: {Path(args.file).name}")
    else:
        pdf_files = pdf_processor.get_pdf_files(args.directory)

        if not pdf_files:
            FileUtils.write_message(f"在目录 {args.directory} 中没有找到PDF文件")
            return

        FileUtils.write_message(f"找到 {len(pdf_files)} 个PDF文件")

    with tqdm(pdf_files, desc="处理文献") as pbar:
        for pdf_file in pbar:
            paper_name = Path(pdf_file).name
            pbar.write(f"\n{'='*60}")
            pbar.write(f"处理文件: {paper_name}")
            pbar.write(f"{'='*60}")

            progress = DebateProgress(
                paper_name=paper_name,
                debate_rounds=args.debate_rounds,
                debate_agents=debate_agents,
                summary_api=summary_api_provider
            )

            if not Path(pdf_file).exists():
                pbar.write(f"文件 {paper_name} 不存在，跳过处理")
                continue

            md_file = Path(pdf_file).with_suffix('.md')
            if md_file.exists():
                try:
                    with open(md_file, 'r', encoding='utf-8') as f:
                        md_content = f.read()

                    lines = md_content.strip().split('\n')
                    non_title_content = []
                    for line in lines:
                        line = line.strip()
                        if not (line.startswith('#') or line == ''):
                            non_title_content.append(line)

                    if len(''.join(non_title_content)) < 100:
                        pbar.write(f"对应的Markdown文件 {md_file.name} 只有章节标题，重新处理")
                    else:
                        pbar.write(f"对应的Markdown文件 {md_file.name} 已存在且内容完整，跳过处理")
                        continue
                except Exception as e:
                    pbar.write(f"读取Markdown文件 {md_file.name} 失败: {e}")
                    pbar.write(f"重新处理 {paper_name}")

            progress.set_stage(DebateStage.EXTRACTING_TEXT, "active")
            pbar.write(f"[阶段1/3] 提取文本中...")
            pbar.write(f"  开始提取: {paper_name}")

            text = pdf_processor.extract_text(pdf_file, pbar=pbar)

            if not text:
                pbar.write(f"无法提取 {paper_name} 的文本内容")
                continue

            max_text_length = 200000
            if len(text) > max_text_length:
                text = text[:max_text_length]
                pbar.write(f"文本长度超过限制，已截断至 {max_text_length} 字符")

            progress.complete_stage()
            pbar.write(f"[阶段1/3] 文本提取完成 ({len(text)} 字符)")

            paper_title = Path(pdf_file).stem
            
            # 创建LLM对话子进度条
            total_steps = 1 + (2 * args.debate_rounds)  # 1次总结 + 2次/轮debate
            with tqdm(total=100, desc=f"LLM对话", leave=False, position=1) as sub_pbar:
                # 阶段2: 生成总结
                progress.set_stage(DebateStage.GENERATING_SUMMARY, "active", summary_api_provider)
                pbar.write(f"[阶段2/3] {summary_api_provider} API生成总结中...")
                sub_pbar.update(0)

                analysis_result = summary_api.analyze_paper(text, paper_title, pbar=pbar)

                if not analysis_result:
                    pbar.write(f"分析 {paper_name} 失败，生成默认模板")
                    analysis_result = f"""
# {paper_title}

## 1. 基本信息
- 作者：
- 发表年份：
- 论文标题：{paper_title}
- 期刊/会议名称：
- 卷号：
- 期号：
- 起止页码：
- DOI号：

## 2. 研究目的与动机

## 3. 核心研究假设或理论构想

## 4. 研究设计

## 5. 数据/样本来源

## 6. 方法与技术

## 7. 分析流程

## 8. 数据分析

## 9. 核心发现

## 10. 实验结果

## 11. 辅助结果

## 12. 最终结论

## 13. 研究贡献

## 14. 与研究主题的关联

## 15. 综述亮点

## 16. 图表概述

## 17. 评价

## 18. 疑问与不足

## 19. 启发与未来方向

## 20. 代表性参考文献
            """

                progress.complete_stage()
                pbar.write(f"[阶段2/3] 总结生成完成")
                summary_progress = 30 if args.enable_debate else 100
                sub_pbar.update(summary_progress)

                if args.enable_debate and debate_apis:
                    progress.set_stage(DebateStage.DEBATE_QUESTION, "pending")
                    pbar.write(f"[阶段3/3] 开始Debate过程 ({args.debate_rounds}轮)")

                    debate_history = []
                    debate_agent_index = 0
                    num_debate_agents = len(debate_agents)
                    debate_progress = (70 / args.debate_rounds) if args.enable_debate else 0

                    for round_num in range(1, args.debate_rounds + 1):
                        progress.current_round = round_num
                        pbar.write(f"\n{'─'*50}")
                        pbar.write(f"  第 {round_num}/{args.debate_rounds} 轮 Debate")
                        pbar.write(f"{'─'*50}")

                        progress.set_stage(DebateStage.DEBATE_QUESTION, "active")

                        previous_round_context = None
                        previous_agents_questions = None
                        if debate_history:
                            last_round_responses = []
                            last_round_agents_content = []
                            current_round_marker = f"## 第{round_num - 1}轮："
                            in_last_round = False

                            for entry in debate_history:
                                if entry.startswith(current_round_marker):
                                    in_last_round = True
                                elif entry.startswith(f"## 第{round_num}轮："):
                                    in_last_round = False

                                if in_last_round or (f"## 第{round_num - 1}轮：" in entry and not entry.startswith(f"## 第{round_num}轮：")):
                                    if "作者" in entry:
                                        last_round_responses.append(entry)
                                    elif "审稿人" in entry:
                                        last_round_agents_content.append(entry)

                            if last_round_responses:
                                previous_round_context = "\n\n".join(last_round_responses)
                            if last_round_agents_content:
                                previous_agents_questions = "\n\n".join(last_round_agents_content)

                        pbar.write(f"  [提示] 各审稿人可了解上一轮其他审稿人的诘问，但不掌握本轮其他审稿人的观点")

                        round_debate_contents = []

                        for i, debate_agent in enumerate(debate_agents):
                            pbar.write(f"  [{round_num}.{i+1}] {debate_agent}（审稿人）诘问...")

                            if previous_round_context and previous_agents_questions:
                                context_for_agent = f"""上一轮作者的回应：
{previous_round_context}

上一轮其他审稿人的诘问（供参考，但不应影响你的独立判断）：
{previous_agents_questions}"""
                            elif previous_round_context:
                                context_for_agent = f"""上一轮作者的回应：
{previous_round_context}"""
                            else:
                                context_for_agent = None

                            debate_content = debate_apis[debate_agent].debate_paper_summary(
                                paper_title,
                                analysis_result,
                                previous_debate=context_for_agent,
                                pbar=pbar
                            )

                            if debate_content:
                                round_debate_contents.append((debate_agent, debate_content))
                                pbar.write(f"  [{round_num}.{i+1}] {debate_agent} 诘问完成")
                            else:
                                pbar.write(f"  [{round_num}.{i+1}] {debate_agent} 诘问失败，跳过此agent")

                        for debate_agent, debate_content in round_debate_contents:
                            debate_history.append(f"## 第{round_num}轮：{debate_agent} API（审稿人）诘问\n\n{debate_content}")

                        if not round_debate_contents:
                            pbar.write(f"  第{round_num}轮诘问全部失败，结束debate")
                            break

                        progress.complete_stage()
                        sub_pbar.update(debate_progress * 0.5)  # 诘问完成50%

                        progress.set_stage(DebateStage.DEBATE_RESPONSE, "active", summary_api_provider)
                        pbar.write(f"  [{round_num}.*] {summary_api_provider}（作者）统一回应...")

                        if summary_api_provider not in debate_apis:
                            summary_debate_kwargs = {"analysis_mode": analysis_mode}
                            if summary_api_provider == "deepseek":
                                summary_debate_kwargs.update({"api_key": args.api_key, "api_mode": args.mode})
                            elif summary_api_provider == "qianwen":
                                summary_debate_kwargs.update({"api_key": args.qianwen_api_key})
                            elif summary_api_provider == "glm":
                                summary_debate_kwargs.update({"api_key": args.glm_api_key})
                            debate_apis[summary_api_provider] = APIFactory.create_api(summary_api_provider, **summary_debate_kwargs)

                        combined_debate_content = "\n\n".join([f"**{agent} API（审稿人）诘问：**\n{content}" for agent, content in round_debate_contents])

                        response_content = debate_apis[summary_api_provider].respond_to_debate(
                            paper_title,
                            analysis_result,
                            combined_debate_content,
                            pbar=pbar
                        )

                        if response_content:
                            debate_history.append(f"## 第{round_num}轮：{summary_api_provider} API（作者）统一回应\n\n{response_content}")
                            progress.complete_stage()
                            pbar.write(f"  [{round_num}.*] {summary_api_provider} 回应完成")
                            sub_pbar.update(debate_progress * 0.5)  # 回应完成50%
                        else:
                            pbar.write(f"  [{round_num}.*] {summary_api_provider} 回应失败")
                            progress.fail_stage()
                            break

                        pbar.write(f"{'─'*50}")
                        pbar.write(f"  第 {round_num}/{args.debate_rounds} 轮 完成")
                        pbar.write(f"{'─'*50}")

                    if debate_history:
                        full_debate = f"""
# 多轮Agent Debate过程

{"\n\n".join(debate_history)}
                    """

                        analysis_result += f"\n\n## 21. Agent Debate 质询结果\n\n{full_debate}"
                        pbar.write(f"[阶段3/3] Debate完成 ({len(debate_history)}条记录)")
                    else:
                        pbar.write("[阶段3/3] Debate失败，跳过")
                else:
                    progress.set_stage(DebateStage.DEBATE_QUESTION, "completed")
                    progress.set_stage(DebateStage.DEBATE_RESPONSE, "completed")

                sub_pbar.update(100 - sub_pbar.n)  # 确保进度条达到100%

            output_path = FileUtils.get_output_path(pdf_file, args.output_dir)

            if FileUtils.write_text(analysis_result, output_path):
                pbar.write(f"综述已保存到: {output_path}")
            else:
                pbar.write(f"保存综述失败: {output_path}")

    FileUtils.write_message("\n=====================================")
    FileUtils.write_message("任务完成")
    FileUtils.write_message("=====================================")


if __name__ == "__main__":
    main()

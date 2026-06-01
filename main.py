"""
命令行入口。

用法：
  python main.py run report.pdf --field title="文档标题" --field author="作者"
  python main.py run https://example.com/article --field summary="文章摘要"
  python main.py batch docs/ --field title="标题" --field date="发布日期"
  python main.py review
"""

import json
import signal
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from logger import setup_logging
from extractor.extract import ExtractionSchema, FieldSpec
from queue_worker import DocumentQueue, JobStatus
from exporter import append_to_excel

console = Console()
LOG_FILE = setup_logging()


def parse_fields(field_args: tuple[str]) -> ExtractionSchema:
    fields: dict[str, FieldSpec] = {}
    for arg in field_args:
        if "=" not in arg:
            console.print(f"[red]字段格式错误（需要 name=描述）: {arg}[/red]")
            sys.exit(1)
        name, rest = arg.split("=", 1)
        parts = rest.split(":")
        description = parts[0]
        required = "required" in parts
        field_type = next((p for p in parts if p in {"str", "list", "int", "float"}), "str")
        fields[name.strip()] = FieldSpec(
            description=description, required=required, field_type=field_type,
        )
    if not fields:
        console.print("[red]至少需要指定一个字段，例如 --field title=文档标题[/red]")
        sys.exit(1)
    return ExtractionSchema(fields=fields)


def print_result(job, show_raw: bool = False):
    if job.status == JobStatus.FAILED:
        console.print(Panel(
            f"[red]{job.error}[/red]",
            title=f"[red]失败: {job.source}[/red]",
        ))
        console.print(f"[dim]完整错误日志: {LOG_FILE}[/dim]")
        return

    result = job.result
    quality = job.quality

    score_color = "green" if quality["passed"] else "red"
    score_str = f"[{score_color}]{quality['score']}/100[/{score_color}]"

    # 解析策略标记（非 direct 时显示）
    strategy_note = ""
    if result.parse_strategy not in ("direct", None):
        strategy_note = f"  解析策略: [yellow]{result.parse_strategy}[/yellow]"

    table = Table(title=f"抽取结果  |  质量分: {score_str}  |  模型: {result.model_used}{strategy_note}")
    table.add_column("字段", style="cyan", min_width=16)
    table.add_column("值", style="white")

    for k, v in result.data.items():
        if k == "_raw":
            continue
        val = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v or "—")
        table.add_row(k, val)

    console.print(table)

    if quality["notes"]:
        for note in quality["notes"]:
            console.print(f"  [yellow]⚠ {note}[/yellow]")

    # Token / 成本
    if result.token_usage:
        u = result.token_usage
        cost_str = f"  成本: [cyan]${u.cost_usd:.4f}[/cyan]" if u.cost_usd > 0 else "  (本地模型，无 API 成本)"
        console.print(f"[dim]Token: 输入 {u.input_tokens} + 输出 {u.output_tokens}{cost_str}[/dim]")

    if show_raw:
        console.print(Panel(result.raw_response, title="LLM 原始输出"))


def _job_to_dict(job) -> dict:
    """将 JobResult 序列化为可 JSON 序列化的 dict。"""
    token_usage = None
    parse_strategy = None
    if job.result:
        parse_strategy = job.result.parse_strategy
        if job.result.token_usage:
            token_usage = job.result.token_usage.model_dump()
    return {
        "source": job.source,
        "data": job.result.data if job.result else None,
        "model_used": job.result.model_used if job.result else None,
        "quality": job.quality,
        "parse_strategy": parse_strategy,
        "token_usage": token_usage,
        "error": job.error,
    }


# ── CLI 命令 ──────────────────────────────────────────────────

@click.group()
def cli():
    """LLM 文档 ETL 工具"""


@cli.command()
@click.argument("source")
@click.option("--field", "-f", multiple=True, help="字段定义，格式: name=描述[:required][:list]")
@click.option("--raw", is_flag=True, help="显示 LLM 原始输出")
def run(source: str, field: tuple, raw: bool):
    """处理单个文档或 URL。"""
    schema = parse_fields(field)
    queue = DocumentQueue()

    with console.status(f"[bold green]处理中: {source}..."):
        job_id = queue.submit(source, schema)
        job = queue.wait(job_id)

    queue.shutdown()
    print_result(job, raw)

    output_path = Path("output")
    output_path.mkdir(exist_ok=True)
    if job.result:
        out_file = output_path / f"{job.job_id}.json"
        out_file.write_text(
            json.dumps(_job_to_dict(job), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"\n[dim]结果已保存: {out_file}[/dim]")

    excel_path = output_path / "results.xlsx"
    append_to_excel([job], schema, excel_path)
    console.print(f"[dim]Excel 已更新: {excel_path}[/dim]")


@cli.command()
@click.argument("directory")
@click.option("--field", "-f", multiple=True, help="字段定义")
@click.option("--ext", default="pdf,txt,md", help="文件后缀（逗号分隔），默认: pdf,txt,md")
def batch(directory: str, field: tuple, ext: str):
    """
    批量处理目录下的所有文档。

    支持优雅中断：Ctrl+C 会等待当前任务完成后输出已完成结果，
    不会丢失已处理的数据。
    """
    schema = parse_fields(field)
    exts = {f".{e.strip().lstrip('.')}" for e in ext.split(",")}

    sources = [
        str(p) for p in Path(directory).rglob("*")
        if p.is_file() and p.suffix.lower() in exts
    ]

    if not sources:
        console.print(f"[yellow]目录中没有找到 {ext} 文件: {directory}[/yellow]")
        return

    console.print(f"找到 [bold]{len(sources)}[/bold] 个文件，开始并行处理...\n")
    console.print("[dim]提示: 按 Ctrl+C 可在任意时刻优雅中断，已完成的结果不会丢失[/dim]\n")

    queue = DocumentQueue()

    # 注册优雅关闭信号处理器（只在主线程生效）
    def _on_interrupt(sig, frame):
        console.print("\n[yellow]⚡ 收到中断信号，等待当前任务完成后输出已有结果...[/yellow]")
        queue.request_shutdown()

    original_handler = signal.signal(signal.SIGINT, _on_interrupt)

    job_ids = {queue.submit(src, schema): src for src in sources}

    # 等待批次稳定：完成率 >= 95% AND 30s 无新进展，或收到关闭信号
    with console.status("[bold green]处理中，Ctrl+C 可随时中断并输出已完成结果..."):
        results = queue.wait_partial(
            list(job_ids.keys()),
            threshold=0.95,
            stable_timeout=30.0,
        )

    signal.signal(signal.SIGINT, original_handler)  # 恢复默认处理
    queue.shutdown()

    # ── 汇总统计 ──────────────────────────────────────────────
    done_jobs    = [j for j in results if j.status == JobStatus.DONE]
    failed_jobs  = [j for j in results if j.status == JobStatus.FAILED]
    pending_jobs = [j for j in results if j.status not in {JobStatus.DONE, JobStatus.FAILED}]

    summary = Table(title=f"批处理完成  共 {len(results)} 个文档")
    summary.add_column("文件", style="cyan")
    summary.add_column("状态")
    summary.add_column("质量分")
    summary.add_column("解析策略")
    summary.add_column("模型")

    for job in results:
        if job.status == JobStatus.DONE:
            q = job.quality
            score_color = "green" if q["passed"] else "yellow"
            strategy = job.result.parse_strategy if job.result else "—"
            strategy_color = "white" if strategy == "direct" else "yellow"
            summary.add_row(
                Path(job.source).name,
                "[green]完成[/green]",
                f"[{score_color}]{q['score']}[/{score_color}]",
                f"[{strategy_color}]{strategy}[/{strategy_color}]",
                job.result.model_used,
            )
        elif job.status == JobStatus.FAILED:
            summary.add_row(Path(job.source).name, "[red]失败[/red]", "—", "—", "—")
        else:
            summary.add_row(Path(job.source).name, "[yellow]未完成（中断）[/yellow]", "—", "—", "—")

    console.print(summary)
    console.print(
        f"\n完成: [green]{len(done_jobs)}[/green]  "
        f"失败: [red]{len(failed_jobs)}[/red]  "
        f"未完成: [yellow]{len(pending_jobs)}[/yellow]"
    )

    # Token 成本汇总
    total_cost = sum(
        j.result.token_usage.cost_usd
        for j in done_jobs
        if j.result and j.result.token_usage
    )
    total_in  = sum(j.result.token_usage.input_tokens  for j in done_jobs if j.result and j.result.token_usage)
    total_out = sum(j.result.token_usage.output_tokens for j in done_jobs if j.result and j.result.token_usage)
    if total_in or total_out:
        cost_str = f"  成本: [cyan]${total_cost:.4f}[/cyan]" if total_cost > 0 else "  (本地模型，无 API 成本)"
        console.print(f"[dim]本批次 Token: 输入 {total_in} + 输出 {total_out}{cost_str}[/dim]")

    # 解析修复统计
    repaired = [j for j in done_jobs if j.result and j.result.parse_strategy not in ("direct", None)]
    if repaired:
        console.print(f"[yellow]⚠ {len(repaired)} 份文档触发了 JSON 修复（策略非 direct）[/yellow]")

    # ── 保存 JSON ─────────────────────────────────────────────
    output_path = Path("output")
    output_path.mkdir(exist_ok=True)

    out_file = output_path / "batch_results.json"
    out_file.write_text(
        json.dumps([_job_to_dict(j) for j in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console.print(f"[dim]批处理结果已保存: {out_file}[/dim]")

    # ── 导出 Excel ────────────────────────────────────────────
    excel_path = output_path / "results.xlsx"
    append_to_excel(results, schema, excel_path)
    console.print(f"[dim]Excel 已更新: {excel_path}[/dim]")


@cli.command()
@click.argument("targets", nargs=-1, type=click.Path())
def review(targets):
    """
    复审已提取的数据：字段完整性 + 格式校验。

    可插拔：独立于提取流程，随时运行，可多次复审。
    结果写入 output/results.xlsx 的"复审报告" Sheet。

    示例:
      python main.py review                         # 复审 output/ 下所有结果
      python main.py review output/abc123.json      # 复审某个文件
      python main.py review output/a.json b.json    # 复审多个文件
    """
    from reviewer.review import run_review

    output_dir = Path("output")

    if targets:
        paths: list[Path] = []
        for t in targets:
            p = Path(t)
            if p.is_dir():
                paths.extend(sorted(p.glob("*.json")))
            elif p.suffix == ".json" and p.exists():
                paths.append(p)
            else:
                console.print(f"[yellow]跳过（不存在或非 JSON）: {t}[/yellow]")
    else:
        paths = sorted(output_dir.glob("*.json"))

    if not paths:
        console.print("[yellow]未找到 JSON 文件。请先运行 run 或 batch 提取数据。[/yellow]")
        return

    console.print(f"加载 [bold]{len(paths)}[/bold] 个数据文件...\n")
    excel_path = output_dir / "results.xlsx"
    run_review(paths, excel_path)


if __name__ == "__main__":
    cli()
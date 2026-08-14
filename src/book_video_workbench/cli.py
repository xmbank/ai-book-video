from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from book_video_workbench.config import Settings
from book_video_workbench.doctor import diagnose, format_diagnosis
from book_video_workbench.pipeline import Pipeline, RunOptions, create_task
from book_video_workbench.state import STAGES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 图书带货视频工作台 P0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查本机依赖和 Provider 配置")
    doctor.add_argument("--json", action="store_true", help="输出 JSON")

    demo = subparsers.add_parser("demo", help="生成不调用外部 API 的离线竖版样片")
    demo.add_argument("--force-stage", choices=STAGES)

    run = subparsers.add_parser("run", help="从真实抖音分享链接运行文章同款八步生产链路")
    run.add_argument("--share", required=True, help="抖音链接或完整分享文案")
    run.add_argument("--book-title", default="", help="可选；留空时从逐字稿自动识别")
    run.add_argument("--book-author", default="", help="可选；留空时从逐字稿自动识别")
    run.add_argument("--selling-point", action="append", default=[], help="可选的已验证卖点，可重复")
    run.add_argument("--keyword", default="图书带货", help="选题或图书品类关键词")
    run.add_argument("--target-seconds", type=int, default=600)
    run.add_argument(
        "--scene-count",
        type=int,
        choices=[9, 18, 27, 36, 45, 54, 63],
        default=18,
        help="场景图数量；约十分钟视频建议 63 张",
    )
    run.add_argument("--whisper-model", default="small")
    run.add_argument("--subtitle-mode", choices=["whisper", "proportional"], default="whisper")
    run.add_argument("--book-cover", help="有权使用的本地书籍封面")
    run.add_argument("--allow-source-video", action="store_true")
    run.add_argument("--force-stage", choices=STAGES)

    resume = subparsers.add_parser("resume", help="继续一个已有任务")
    resume.add_argument("task_dir", type=Path)
    resume.add_argument("--force-stage", choices=STAGES)
    return parser


def _run_pipeline(task_dir: Path, settings: Settings, force_stage: str | None) -> int:
    try:
        output = Pipeline(task_dir, settings).run(force_stage=force_stage)
    except Exception as exc:
        print(f"任务失败: {exc}", file=sys.stderr)
        print(f"状态文件: {task_dir / 'pipeline-state.json'}", file=sys.stderr)
        return 1
    print(f"任务完成: {task_dir}")
    print(f"成片: {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "doctor":
        report = diagnose(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_diagnosis(report))
        return 0 if report["ready_for_offline_demo"] else 1
    if args.command == "resume":
        task_dir = args.task_dir.expanduser().resolve()
        if not (task_dir / "task.json").is_file():
            print(f"不是有效任务目录: {task_dir}", file=sys.stderr)
            return 2
        return _run_pipeline(task_dir, settings, args.force_stage)
    if args.command == "demo":
        options = RunOptions(
            mode="demo",
            share_text="",
            book_title="把时间当作朋友",
            selling_points=["从长期视角理解时间管理", "适合反复阅读"],
            target_seconds=90,
            subtitle_mode="proportional",
        )
    else:
        options = RunOptions(
            mode="real",
            share_text=args.share,
            book_title=args.book_title,
            book_author=args.book_author,
            selling_points=args.selling_point,
            keyword=args.keyword,
            target_seconds=args.target_seconds,
            scene_count=args.scene_count,
            whisper_model=args.whisper_model,
            subtitle_mode=args.subtitle_mode,
            book_cover=args.book_cover,
            allow_source_video=args.allow_source_video,
        )
    task_dir = create_task(settings, options)
    return _run_pipeline(task_dir, settings, args.force_stage)

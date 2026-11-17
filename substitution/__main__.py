"""Командная строка: подбор, демонстрация, сохранённые заявки и действия."""
import argparse
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import sys

from .model import Actor, Dataset, Policy, Request, aware
from .planner import Planner, SearchLimit
from .service import Service


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Поиск временных замен преподавателей")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Рассчитать назначения и рейтинг из JSON")
    plan.add_argument("input")
    plan.add_argument("--output")
    init = commands.add_parser("init", help="Создать SQLite из справочников JSON")
    init.add_argument("input")
    init.add_argument("--database", required=True)
    action = commands.add_parser("action", help="Выполнить действие из JSON")
    action.add_argument("input")
    action.add_argument("--database", required=True)
    for name in ("snapshot", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--database", required=True)
    demo = commands.add_parser("demo", help="Демонстрация до статуса confirmed")
    demo.add_argument("--database", default=":memory:")
    web = commands.add_parser("web", help="Открыть локальный экран подбора")
    web.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    service = None
    try:
        if args.command == "web":
            from .web import serve

            serve(args.port)
            return 0
        if args.command == "plan":
            raw = read(args.input)
            result = Planner(Dataset.from_dict(raw["data"]), Policy(**raw.get("policy", {}))).plan(Request(**raw["request"]))
        elif args.command == "init":
            raw = read(args.input)
            service = Service(args.database, Dataset.from_dict(raw["data"]), Policy(**raw.get("policy", {})))
            result = {"database": args.database, "status": "initialized"}
        elif args.command == "demo":
            raw = read(Path(__file__).resolve().parent.parent / "examples" / "scenario.json")
            service = Service(args.database, Dataset.from_dict(raw["data"]))
            request = Request(**raw["request"])
            secretary = Actor("secretary-1", "secretary", request.department)
            now = aware(request.deadline) - timedelta(hours=2)
            service.create(request, secretary, now)
            service.start(request.id, secretary, now)
            for _ in range(len(request.lesson_ids) + 1):
                state = service.snapshot()
                if state["requests"][request.id]["status"] == "confirmed":
                    break
                offers = [o for o in state["offers"].values() if o["status"] == "proposed"]
                if not offers:
                    raise RuntimeError("Демонстрация остановилась без предложений")
                teacher = offers[0]["teacher"]
                ids = [o["id"] for o in offers if o["teacher"] == teacher]
                actor = Actor(teacher, "teacher")
                service.interested(ids, actor, now)
                service.take(ids, actor, now)
            state = service.snapshot()
            result = {"status": state["requests"][request.id]["status"], "assignments": state["assignments"],
                      "audit_events": len(service.audit()), "queued_notifications": len(state["outbox"])}
        else:
            service = Service(args.database)
            if args.command in {"snapshot", "audit"}:
                result = getattr(service, args.command)()
            else:
                raw = read(args.input)
                method = raw["method"]
                allowed = {"create", "start", "poll", "interested", "take", "decline", "authorize",
                           "split", "manual_assign", "cancel", "set_weights", "message"}
                if method not in allowed:
                    raise ValueError("Неизвестное действие")
                kwargs = raw.get("arguments", {})
                if "actor" in kwargs:
                    kwargs["actor"] = Actor(**kwargs["actor"])
                if "request" in kwargs:
                    kwargs["request"] = Request(**kwargs["request"])
                result = getattr(service, method)(**kwargs)
                if result is None:
                    result = {"status": "ok"}
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if getattr(args, "output", None):
            Path(args.output).write_text(output + "\n", encoding="utf-8")
        else:
            print(output)
        return 0
    except (ValueError, KeyError, PermissionError, OSError, SearchLimit, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    sys.exit(main())

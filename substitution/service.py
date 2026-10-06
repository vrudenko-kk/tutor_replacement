"""Согласование с атомарными переходами и сохранением в SQLite.

Actor передаётся доверенным приложением после аутентификации. Это библиотека,
а не публичный HTTP API. Уведомления помещаются в outbox без отправки писем.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

from .model import Actor, Dataset, Policy, Request, Stage, aware
from .planner import Planner, SearchLimit


LIVE = {"proposed", "interested"}
FINAL = {"confirmed", "failed", "cancelled"}


class Service:
    def __init__(self, database, data: Dataset | None = None, policy: Policy | None = None):
        self.db = sqlite3.connect(str(database), timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, details TEXT NOT NULL);
        ''')
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()
            if row is None:
                if data is None:
                    raise ValueError("Для новой базы нужны исходные данные")
                data.validate()
                policy = policy or Policy()
                policy.validate()
                state = {"data": data.to_dict(), "policy": asdict(policy), "requests": {}, "offers": {},
                         "assignments": {}, "outbox": [], "messages": []}
                self.db.execute("INSERT INTO state VALUES (1, ?)", (json.dumps(state, ensure_ascii=False),))
            elif data is not None:
                raise ValueError("База уже существует; откройте её без повторного импорта")
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, actor, now):
        now = aware(now or datetime.now(timezone.utc))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = json.loads(self.db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
            yield state, now
            self.db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def log(self, actor, now, action, **details):
        self.db.execute("INSERT INTO audit(at, actor, action, details) VALUES (?,?,?,?)",
                        (now.isoformat(), actor.id, action, json.dumps(details, ensure_ascii=False)))

    @staticmethod
    def secretary(actor, request):
        if actor.role != "secretary" or actor.department != request["department"]:
            raise PermissionError("Действие доступно секретарю кафедры заявки")

    @staticmethod
    def notify(state, kind, recipient, **payload):
        state["outbox"].append({"id": uuid4().hex, "kind": kind, "recipient": recipient,
                                "payload": payload, "status": "pending"})

    @staticmethod
    def commitments(state, exclude=()):
        result = list(state["assignments"].values())
        result += [o for oid, o in state["offers"].items() if o["status"] == "interested" and oid not in exclude]
        return result

    def create(self, request: Request, actor: Actor, now=None):
        with self.transaction(actor, now) as (state, now):
            request = deepcopy(request)
            data = Dataset.from_dict(state["data"])
            request.validate(data)
            raw = asdict(request)
            if actor.role == "teacher":
                if actor.id != request.initiator:
                    raise PermissionError("Преподаватель может подавать только свои заявки")
                if request.online_campuses or request.cap_overrides or request.history_waivers:
                    raise PermissionError("Исключения должен одобрить секретарь")
                if request.mode != "sequential" or request.timeout_minutes != 15:
                    raise PermissionError("Режим и таймаут задаёт секретарь при запуске")
            else:
                self.secretary(actor, raw)
            if aware(request.deadline) <= now:
                raise ValueError("Предельный срок уже истёк")
            if request.id in state["requests"]:
                raise ValueError("Код заявки уже существует")
            if any(lid in state["assignments"] for lid in request.lesson_ids):
                raise ValueError("Для пары уже назначена замена")
            for existing in state["requests"].values():
                if existing["status"] not in {"cancelled", "failed"} and set(existing["lesson_ids"]) & set(request.lesson_ids):
                    raise ValueError("Пара уже включена в действующую заявку")
            raw["status"] = "submitted"
            state["requests"][request.id] = raw
            self.log(actor, now, "request_created", request=raw)
            return deepcopy(raw)

    def expire(self, state, now):
        system = Actor("system", "system")
        for o in state["offers"].values():
            if o["status"] in LIVE and aware(o["expires_at"]) <= now:
                o["status"] = "timeout"
                self.log(system, now, "offer_timeout", offer=o["id"])
        for r in state["requests"].values():
            if r["status"] not in FINAL and aware(r["deadline"]) <= now:
                r["status"] = "failed"
                self.withdraw(state, r["id"], now)
                self.log(system, now, "request_failed", request=r["id"], reason="deadline")
                self.notify(state, "manual_required", f'secretary:{r["department"]}', request=r["id"], reason="deadline")

    def withdraw(self, state, rid, now, lesson=None, except_id=None):
        for oid, o in state["offers"].items():
            if o["request"] == rid and o["status"] in LIVE and oid != except_id and (lesson is None or o["lesson"] == lesson):
                o["status"] = "withdrawn"
                self.log(Actor("system", "system"), now, "offer_withdrawn", offer=oid)
                self.notify(state, "offer_withdrawn", o["teacher"], offer=oid)

    def refresh(self, state, rid, actor, now):
        raw = state["requests"][rid]
        if raw["status"] in FINAL:
            return None
        remaining = [lid for lid in raw["lesson_ids"] if lid not in state["assignments"]]
        if not remaining:
            raw["status"] = "confirmed"
            self.log(actor, now, "request_confirmed", request=rid)
            self.notify(state, "request_confirmed", raw["initiator"], request=rid)
            self.notify(state, "request_confirmed", f'secretary:{raw["department"]}', request=rid)
            return None
        active = [o for o in state["offers"].values() if o["request"] == rid and o["status"] in LIVE]
        # Резерв по паре уже получен — ждём решения кандидата.
        held = {o["lesson"] for o in active if o["status"] == "interested"}
        remaining = [l for l in remaining if l not in held]
        if not remaining:
            raw["status"] = "negotiating"
            return None
        request = Request(**raw)
        request.lesson_ids = remaining
        if request.episode_id:
            earlier = [r for r in state["requests"].values() if r["episode_id"] == request.episode_id
                       and r["initiator"] == request.initiator and r["day"] < request.day]
            if earlier:
                first_day = min(r["day"] for r in earlier)
                first_ids = {r["id"] for r in earlier if r["day"] == first_day}
                request.preferred = sorted(set(request.preferred) | {
                    a["teacher"] for a in state["assignments"].values() if a["request"] in first_ids})
        excluded = {(o["teacher"], o["lesson"]) for o in state["offers"].values()
                    if o["request"] == rid and o["status"] in {"declined", "timeout"}}
        data = Dataset.from_dict(state["data"])
        planner = Planner(data, Policy(**state["policy"]), self.commitments(state))
        raw["status"] = "searching"
        try:
            plan = planner.plan(request, excluded)
        except SearchLimit as error:
            raw["status"] = "manual_required"
            self.log(actor, now, "search_limit", request=rid, reason=str(error))
            self.notify(state, "manual_required", f'secretary:{raw["department"]}', request=rid, reason=str(error))
            return {"complete": False, "error": str(error)}
        for step in plan["audit"]:
            self.log(actor, now, "planning_stage", request=rid, **step)
        self.log(actor, now, "ranking", request=rid, ranked=plan["ranked"])
        if not plan["complete"]:
            raw["status"] = "manual_required"
            self.notify(state, "manual_required", f'secretary:{raw["department"]}', request=rid, reason="no_full_assignment")
            return plan
        raw["status"] = "negotiating"
        for lid in remaining:
            existing = [o for o in active if o["lesson"] == lid]
            limit = 1 if request.mode == "sequential" else planner.policy.parallel_pool
            available = [c for c in plan["ranked"][lid] if c["allows_full_coverage"]]
            # Сначала согласовывается найденное глобальное оптимальное назначение.
            selected = plan["assignments"][lid]["teacher"]
            available.sort(key=lambda c: c["teacher"] != selected)
            for candidate in available:
                if len(existing) >= limit:
                    break
                if any(o["teacher"] == candidate["teacher"] for o in existing):
                    continue
                oid = uuid4().hex
                o = {"id": oid, "request": rid, "lesson": lid, "teacher": candidate["teacher"],
                     "status": "proposed", "stage": plan["stage"], "campus": candidate["campus"],
                     "online": candidate["online"], "cost": candidate["cost"], "mode": request.mode,
                     "created_at": now.isoformat(),
                     "expires_at": min(now + timedelta(minutes=request.timeout_minutes), aware(request.deadline)).isoformat()}
                state["offers"][oid] = o
                existing.append(o)
                self.log(actor, now, "offer_proposed", offer=o, channel="outbox")
                self.notify(state, "offer", o["teacher"], **o)
        return plan

    def start(self, rid, actor, now=None, *, mode=None, timeout_minutes=None):
        with self.transaction(actor, now) as (state, now):
            self.secretary(actor, state["requests"][rid])
            self.expire(state, now)
            raw = state["requests"][rid]
            if mode is not None or timeout_minutes is not None:
                if raw["status"] in FINAL:
                    raise ValueError("Заявка завершена")
                if mode is not None:
                    raw["mode"] = mode
                if timeout_minutes is not None:
                    raw["timeout_minutes"] = timeout_minutes
                Request(**raw).validate(Dataset.from_dict(state["data"]))
                self.withdraw(state, rid, now)
                self.log(actor, now, "negotiation_configured", request=rid, mode=raw["mode"], timeout_minutes=raw["timeout_minutes"])
            return self.refresh(state, rid, actor, now)

    def poll(self, now=None):
        actor = Actor("system", "system")
        with self.transaction(actor, now) as (state, now):
            self.expire(state, now)
            for rid, r in state["requests"].items():
                if r["status"] in {"searching", "negotiating"}:
                    self.refresh(state, rid, actor, now)

    def selected(self, state, offer_ids, actor, expected):
        if not offer_ids or len(set(offer_ids)) != len(offer_ids):
            raise ValueError("Нужен непустой список уникальных предложений")
        if actor.role != "teacher":
            raise PermissionError("Ответить на предложение может только кандидат")
        offers = [state["offers"][oid] for oid in offer_ids]
        if any(o["teacher"] != actor.id for o in offers):
            raise PermissionError("Нельзя отвечать за другого кандидата")
        if len({o["request"] for o in offers}) != 1:
            raise ValueError("Пакетный ответ должен относиться к одной заявке")
        if any(o["status"] != expected for o in offers):
            raise ValueError(f"Все предложения должны иметь статус {expected}")
        if len({o["lesson"] for o in offers}) != len(offers):
            raise ValueError("Нельзя выбрать одну пару дважды")
        return offers

    def check_selection(self, state, offers):
        request = Request(**state["requests"][offers[0]["request"]])
        if request.status in FINAL:
            raise ValueError("Заявка завершена")
        planner = Planner(Dataset.from_dict(state["data"]), Policy(**state["policy"]),
                          self.commitments(state, [o["id"] for o in offers]))
        selection = {}
        for o in offers:
            if o["lesson"] in state["assignments"]:
                raise ValueError("Замещающий уже назначен")
            c, reasons = planner.evaluate(request, o["lesson"], o["teacher"], Stage[o["stage"]])
            if reasons:
                raise ValueError("Кандидат больше не подходит: " + ", ".join(reasons))
            if c.campus != o["campus"] or c.online != o["online"]:
                raise ValueError("Условия площадки изменились; требуется новый подбор")
            selection[o["lesson"]] = c
        if len(offers) > min(planner.cap(o["teacher"], request, Stage[o["stage"]]) for o in offers):
            raise ValueError("Пакет превышает дневной лимит замен")
        if planner.conflict(selection, request, min(Stage[o["stage"]] for o in offers)):
            raise ValueError("Пары пакета несовместимы по времени или переезду")

    def interested(self, offer_ids, actor, now=None):
        with self.transaction(actor, now) as (state, now):
            self.expire(state, now)
            offers = self.selected(state, offer_ids, actor, "proposed")
            self.check_selection(state, offers)
            for o in offers:
                o["status"] = "interested"
                # Единый таймаут от отправки: интерес не продлевает его бесконечно.
                self.log(actor, now, "offer_interested", offer=o["id"], expires_at=o["expires_at"])
            # Уже отправленные предложения на занятый слот становятся недействительными.
            data = Dataset.from_dict(state["data"])
            held = {(data.lessons[o["lesson"]]["day"], str(data.lessons[o["lesson"]]["slot"])) for o in offers}
            for o in state["offers"].values():
                l = data.lessons[o["lesson"]]
                if o["teacher"] == actor.id and o["status"] == "proposed" and (l["day"], str(l["slot"])) in held:
                    o["status"] = "withdrawn"
                    self.log(actor, now, "offer_withdrawn", offer=o["id"], reason="slot_locked")
            return deepcopy(offers)

    def take(self, offer_ids, actor, now=None):
        with self.transaction(actor, now) as (state, now):
            self.expire(state, now)
            offers = self.selected(state, offer_ids, actor, "interested")
            self.check_selection(state, offers)
            rid = offers[0]["request"]
            request = state["requests"][rid]
            for o in offers:
                o["status"] = "taken"
                state["assignments"][o["lesson"]] = {"request": rid, "lesson": o["lesson"],
                    "teacher": actor.id, "campus": o["campus"], "online": o["online"], "accepted_at": now.isoformat()}
                self.log(actor, now, "substitute_assigned", assignment=state["assignments"][o["lesson"]])
                self.withdraw(state, rid, now, o["lesson"], o["id"])
                self.notify(state, "schedule_and_materials", actor.id, lesson=state["data"]["lessons"][o["lesson"]],
                            campus=o["campus"], online=o["online"],
                            comment=request["comments"].get(o["lesson"], ""),
                            materials=request["materials"].get(o["lesson"], ""))
            self.refresh(state, rid, actor, now)
            return deepcopy(request)

    def decline(self, offer_id, actor, now=None):
        with self.transaction(actor, now) as (state, now):
            self.expire(state, now)
            o = state["offers"][offer_id]
            self.selected(state, [offer_id], actor, o["status"])
            if o["status"] not in LIVE:
                raise ValueError("Предложение уже завершено")
            o["status"] = "declined"
            self.log(actor, now, "offer_declined", offer=offer_id)
            self.refresh(state, o["request"], actor, now)

    def authorize(self, rid, actor, reason, *, online_campuses=None, cap_overrides=None, history_waivers=None, now=None):
        if not reason.strip():
            raise ValueError("Нужно обоснование ручного решения")
        with self.transaction(actor, now) as (state, now):
            raw = state["requests"][rid]
            self.secretary(actor, raw)
            self.expire(state, now)
            if raw["status"] in FINAL:
                raise ValueError("Заявка завершена")
            for key, value in (("online_campuses", online_campuses), ("cap_overrides", cap_overrides), ("history_waivers", history_waivers)):
                if value is not None:
                    raw[key] = value
            Request(**raw).validate(Dataset.from_dict(state["data"]))
            self.log(actor, now, "manual_authorization", request=rid, reason=reason,
                     online_campuses=raw["online_campuses"], cap_overrides=raw["cap_overrides"], history_waivers=raw["history_waivers"])
            self.withdraw(state, rid, now)
            return self.refresh(state, rid, actor, now)

    def split(self, rid, lesson_ids, new_id, actor, reason, now=None):
        if not reason.strip():
            raise ValueError("Нужно обоснование выделения подзаявки")
        with self.transaction(actor, now) as (state, now):
            raw = state["requests"][rid]
            self.secretary(actor, raw)
            self.expire(state, now)
            selected = set(lesson_ids)
            if raw["status"] in FINAL or not selected or not selected < set(raw["lesson_ids"]):
                raise ValueError("Можно выделить только непустую часть незавершённой заявки")
            if any(l in state["assignments"] for l in selected) or not new_id or new_id in state["requests"]:
                raise ValueError("У пар уже есть замены или код подзаявки занят")
            child = deepcopy(raw)
            child.update(id=new_id, lesson_ids=sorted(selected), status="manual_required")
            for key in ("psz", "comments", "materials"):
                child[key] = {l: v for l, v in raw[key].items() if l in selected}
                raw[key] = {l: v for l, v in raw[key].items() if l not in selected}
            raw["lesson_ids"] = [l for l in raw["lesson_ids"] if l not in selected]
            state["requests"][new_id] = child
            for lid in selected:
                self.withdraw(state, rid, now, lid)
            self.log(actor, now, "request_split", parent=rid, child=new_id, lessons=sorted(selected), reason=reason)
            self.refresh(state, rid, actor, now)
            return deepcopy(child)

    def manual_assign(self, rid, lid, teacher, actor, reason, now=None):
        """Секретарь подтверждает внешнее согласие кандидата своим обоснованием.

        Разрешены кандидаты вне ранжированного пула; жёсткие запреты сохраняются.
        Для лимита, онлайн и исторического нуля сначала используется authorize.
        """
        if not reason.strip():
            raise ValueError("Нужно обоснование назначения и подтверждение согласия")
        with self.transaction(actor, now) as (state, now):
            raw = state["requests"][rid]
            self.secretary(actor, raw)
            self.expire(state, now)
            if raw["status"] in FINAL or lid not in raw["lesson_ids"] or lid in state["assignments"]:
                raise ValueError("Пара недоступна для назначения")
            # Освобождаем резервы по этой паре в пределах той же транзакции.
            self.withdraw(state, rid, now, lid)
            planner = Planner(Dataset.from_dict(state["data"]), Policy(**state["policy"]), self.commitments(state))
            c, reasons = planner.evaluate(Request(**raw), lid, teacher, Stage.RAISE_CAP)
            if reasons:
                raise ValueError("Жёсткие ограничения: " + ", ".join(reasons))
            state["assignments"][lid] = {"request": rid, "lesson": lid, "teacher": teacher,
                                        "campus": c.campus, "online": c.online, "accepted_at": now.isoformat()}
            self.log(actor, now, "manual_assignment", assignment=state["assignments"][lid], reason=reason)
            self.notify(state, "schedule_and_materials", teacher, lesson=state["data"]["lessons"][lid],
                        campus=c.campus, online=c.online, comment=raw["comments"].get(lid, ""), materials=raw["materials"].get(lid, ""))
            self.refresh(state, rid, actor, now)

    def set_weights(self, weights, actor, now=None):
        if actor.role != "admin":
            raise PermissionError("Веса меняет администратор")
        with self.transaction(actor, now) as (state, now):
            policy = Policy(**state["policy"])
            policy.weights = weights
            policy.validate()
            state["policy"] = asdict(policy)
            self.log(actor, now, "weights_changed", weights=weights)

    def cancel(self, rid, actor, reason, now=None):
        if not reason.strip():
            raise ValueError("Нужно обоснование отмены")
        with self.transaction(actor, now) as (state, now):
            raw = state["requests"][rid]
            self.secretary(actor, raw)
            if raw["status"] in FINAL:
                raise ValueError("Заявка уже завершена")
            if any(l in state["assignments"] for l in raw["lesson_ids"]):
                raise ValueError("В заявке есть принятые замены; сначала выделите незакрытые пары в подзаявку")
            self.withdraw(state, rid, now)
            raw["status"] = "cancelled"
            self.log(actor, now, "request_cancelled", request=rid, reason=reason)

    def message(self, rid, text, actor, now=None):
        with self.transaction(actor, now) as (state, now):
            r = state["requests"][rid]
            allowed = (actor.role == "secretary" and actor.department == r["department"]) or (
                actor.role == "teacher" and (actor.id == r["initiator"] or any(
                    o["request"] == rid and o["teacher"] == actor.id for o in state["offers"].values())))
            if not allowed:
                raise PermissionError("Лента доступна только участникам заявки")
            if not text.strip():
                raise ValueError("Сообщение пустое")
            state["messages"].append({"request": rid, "actor": actor.id, "at": now.isoformat(), "text": text})
            self.log(actor, now, "message_added", request=rid)

    def snapshot(self):
        """Административный экспорт. Публичный адаптер должен ограничить доступ."""
        return json.loads(self.db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])

    def audit(self):
        return [{"seq": seq, "at": at, "actor": actor, "action": action, "details": json.loads(details)}
                for seq, at, actor, action, details in self.db.execute("SELECT * FROM audit ORDER BY seq")]

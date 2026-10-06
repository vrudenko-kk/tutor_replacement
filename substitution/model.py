"""Модель входных данных. Время занятий — местное; события — aware datetime."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from enum import IntEnum
from math import isfinite
from zoneinfo import ZoneInfo


class Stage(IntEnum):
    BASE = 0
    GENERAL = 1
    ALL_RELATED = 2
    YELLOW = 3
    OUTER_GAP = 4
    CROSS_CAMPUS = 5
    ONLINE = 6
    RAISE_CAP = 7


def aware(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Дата и время должны содержать часовой пояс")
    return value


def minute(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def anniversary(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


@dataclass(frozen=True)
class Actor:
    id: str
    role: str  # teacher, secretary, admin
    department: str = ""


@dataclass
class Policy:
    # Значения признаков нормированы в [0, 1]; коэффициенты — предположение.
    weights: dict[str, float] = field(default_factory=lambda: {
        "psz_mismatch": 1, "comp_level": 2, "traffic_light": 1,
        "window_priority": 2, "campus_priority": 6, "travel_penalty": 2,
        "consecutive_priority": 1, "history_score": 1, "preference": 0.5,
    })
    yellow_at_base: bool = False
    parallel_pool: int = 3
    max_search_nodes: int = 20000

    def validate(self):
        if set(self.weights) != set(Policy().weights):
            raise ValueError("Необходимы все девять весов стоимости")
        if any(not isfinite(w) or w < 0 for w in self.weights.values()) or not sum(self.weights.values()):
            raise ValueError("Веса должны быть конечными, неотрицательными, с положительной суммой")
        if self.parallel_pool < 1 or self.max_search_nodes < 1:
            raise ValueError("Размер пула и предел поиска должны быть положительными")


@dataclass
class Request:
    id: str
    initiator: str
    department: str
    day: str
    lesson_ids: list[str]
    psz: dict[str, str]
    deadline: str
    timeout_minutes: int = 15
    mode: str = "sequential"
    comments: dict[str, str] = field(default_factory=dict)
    materials: dict[str, str] = field(default_factory=dict)
    # Явно заданный эпизод отсутствия исключает смешение разных отсутствий.
    episode_id: str = ""
    preferred: list[str] = field(default_factory=list)
    online_campuses: dict[str, str] = field(default_factory=dict)
    cap_overrides: dict[str, int] = field(default_factory=dict)
    history_waivers: list[str] = field(default_factory=list)
    status: str = "submitted"

    def validate(self, data: Dataset):
        day = date.fromisoformat(self.day)
        if not self.id or self.initiator not in data.teachers:
            raise ValueError("Неизвестный инициатор или пустой код заявки")
        if not self.lesson_ids or len(set(self.lesson_ids)) != len(self.lesson_ids):
            raise ValueError("Нужен непустой список уникальных пар")
        if self.mode not in {"sequential", "parallel"} or self.timeout_minutes <= 0:
            raise ValueError("Некорректный режим или таймаут")
        deadline = aware(self.deadline)
        for lid in self.lesson_ids:
            l = data.lessons.get(lid)
            if l is None or l["teacher"] != self.initiator or l["day"] != self.day:
                raise ValueError("Заявка должна содержать пары инициатора на одну дату")
            if self.psz.get(lid) not in {"general", "related", "disciplinary"}:
                raise ValueError("Неизвестный уровень ПСЗ")
            start = datetime.combine(day, datetime.min.time(), ZoneInfo(data.timezone)) + timedelta(minutes=data.interval(l)[0])
            if deadline > start:
                raise ValueError("Срок поиска не может быть позже начала первой пары")
        if not data.in_department(self.initiator, self.department, self.day):
            raise ValueError("Инициатор не работает на кафедре заявки в указанную дату")
        for teacher, cap in self.cap_overrides.items():
            if teacher not in data.teachers or type(cap) is not int or cap < data.teachers[teacher]["daily_cap"]:
                raise ValueError("Повышенный лимит должен быть целым и не ниже обычного")
        for teacher, campus in self.online_campuses.items():
            if teacher not in data.teachers or campus not in data.campuses:
                raise ValueError("Неизвестный преподаватель или место проведения онлайн-замены")
        if any(t not in data.teachers for t in self.preferred):
            raise ValueError("Неизвестный приоритетный замещающий")
        if any(h not in {x["id"] for x in data.history} for h in self.history_waivers):
            raise ValueError("Неизвестная запись истории для снятия запрета")


@dataclass
class Dataset:
    teachers: dict
    disciplines: dict
    lessons: dict
    slots: dict
    campuses: list[str]
    travel: dict
    history: list = field(default_factory=list)
    timezone: str = "Europe/Moscow"
    rooms: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw):
        data = cls(**raw)
        data.validate()
        return data

    def interval(self, lesson):
        slot = self.slots[str(lesson["slot"])]
        return minute(slot["start"]), minute(slot["end"])

    def in_department(self, teacher, department, day):
        return any(p["department"] == department and p.get("start", "0001-01-01") <= day
                   <= (p.get("end") or "9999-12-31")
                   for p in self.teachers[teacher]["positions"])

    def campus(self, lesson):
        return lesson.get("campus") or self.rooms[lesson["room"]]["campus"]

    def validate(self):
        ZoneInfo(self.timezone)
        if len(set(self.campuses)) != len(self.campuses) or not self.campuses:
            raise ValueError("Нужны уникальные площадки")
        previous = -1
        for slot, interval in sorted(self.slots.items(), key=lambda item: int(item[0])):
            start, end = minute(interval["start"]), minute(interval["end"])
            if int(slot) < 1 or start >= end or start < previous:
                raise ValueError("Сетка пар должна быть упорядочена и не пересекаться")
            previous = end
        for p in self.teachers.values():
            if type(p["daily_cap"]) is not int or p["daily_cap"] < 0:
                raise ValueError("Лимит должен быть целым и неотрицательным")
            if set(p["campus_weights"]) != set(self.campuses):
                raise ValueError("Для преподавателя нужны веса всех площадок")
            if any(not isfinite(v) or not 0 <= v <= 1 for v in p["campus_weights"].values()):
                raise ValueError("Вес площадки должен быть в [0, 1]")
            for d, c in p.get("competences", {}).items():
                if d not in self.disciplines or c["level"] not in {"priority", "ready", "prepare", "refuse"}:
                    raise ValueError("Неизвестная дисциплина или уровень компетенции")
                if not isfinite(c.get("priority", 0)) or not 0 <= c.get("priority", 0) <= 1:
                    raise ValueError("Приоритет компетенции должен быть в [0, 1]")
            for key, light in p.get("traffic", {}).items():
                day, slot = key.split("/")
                date.fromisoformat(day)
                if slot not in self.slots or light not in {"green", "yellow", "red"}:
                    raise ValueError("Некорректный светофор")
            for pos in p["positions"]:
                start = date.fromisoformat(pos.get("start", "0001-01-01"))
                end = date.fromisoformat(pos.get("end") or "9999-12-31")
                if start > end:
                    raise ValueError("Некорректный период работы")
        seen = set()
        for l in self.lessons.values():
            date.fromisoformat(l["day"])
            if l["teacher"] not in self.teachers or l["discipline"] not in self.disciplines:
                raise ValueError("Занятие ссылается на неизвестный справочник")
            self.interval(l)
            if self.campus(l) not in self.campuses:
                raise ValueError("Неизвестная площадка занятия")
            key = l["teacher"], l["day"], str(l["slot"])
            if key in seen:
                raise ValueError("В расписании преподавателя две пары в одном слоте")
            seen.add(key)
        for source, targets in self.travel.items():
            for target, value in targets.items():
                if source not in self.campuses or target not in self.campuses or not isfinite(value) or value < 0:
                    raise ValueError("Некорректная матрица переездов")
        ids = set()
        for h in self.history:
            date.fromisoformat(h["day"])
            if h["id"] in ids or not 0 <= h["score"] <= 5 or h["teacher"] not in self.teachers or h["discipline"] not in self.disciplines:
                raise ValueError("Некорректная история замен")
            ids.add(h["id"])

    def to_dict(self):
        return asdict(self)

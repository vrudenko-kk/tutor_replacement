"""Жёсткие ограничения, объяснимая стоимость и точный подбор."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import heapq
import itertools

from .flow import FlowGraph
from .model import Dataset, Policy, Request, Stage, anniversary


class SearchLimit(RuntimeError):
    """Расчёт прерван: оптимальность/невозможность не доказана."""


@dataclass(frozen=True)
class Candidate:
    teacher: str
    lesson: str
    cost: int
    features: dict
    window: str
    campus: str
    online: bool


class Planner:
    def __init__(self, data: Dataset, policy: Policy | None = None, commitments=None):
        self.data = data
        self.policy = policy or Policy()
        self.policy.validate()
        # Подтверждённые замены и действующие резервы «Заинтересован».
        self.commitments = list(commitments or [])

    def events(self, teacher, day):
        result = []
        for lid, lesson in self.data.lessons.items():
            if lesson["teacher"] == teacher and lesson["day"] == day:
                result.append((self.data.interval(lesson), self.data.campus(lesson), None))
        for c in self.commitments:
            lesson = self.data.lessons[c["lesson"]]
            if c["teacher"] == teacher and lesson["day"] == day:
                result.append((self.data.interval(lesson), c["campus"], None))
        return sorted(result, key=lambda e: e[0])

    def cap(self, teacher, request, stage):
        limit = self.data.teachers[teacher]["daily_cap"]
        if stage >= Stage.RAISE_CAP:
            limit = request.cap_overrides.get(teacher, limit)
        used = sum(c["teacher"] == teacher and self.data.lessons[c["lesson"]]["day"] == request.day
                   for c in self.commitments)
        return max(0, limit - used)

    def travel(self, source, target):
        return 0 if source == target else self.data.travel.get(source, {}).get(target)

    def evaluate(self, request, lid, teacher, stage):
        data = self.data
        p, lesson = data.teachers[teacher], data.lessons[lid]
        reasons = []
        if teacher == request.initiator:
            reasons.append("initiator")
        if not data.in_department(teacher, request.department, request.day):
            reasons.append("department")
        if self.cap(teacher, request, stage) == 0:
            reasons.append("daily_cap")
        target = lesson["discipline"]
        competences = p.get("competences", {})
        comp = competences.get(target)
        if comp and comp["level"] == "refuse":
            reasons.append("competence_refused")
        exact = comp is not None and comp["level"] != "refuse"
        target_groups = set(data.disciplines[target].get("related_groups", []))
        related = exact or any(
            c["level"] != "refuse" and (
                bool(target_groups & set(data.disciplines[d].get("related_groups", []))) or
                (not target_groups and stage >= Stage.ALL_RELATED)
            ) for d, c in competences.items())
        psz = request.psz[lid]
        if psz == "disciplinary" and not exact:
            reasons.append("disciplinary_competence")
        if psz == "related" and not related:
            reasons.append("related_competence")
        # Общий изначально не требует компетенции (раздел 6.5).
        light = p.get("traffic", {}).get(f'{request.day}/{lesson["slot"]}', "yellow")
        if light == "red":
            reasons.append("traffic_red")
        if light == "yellow" and not self.policy.yellow_at_base and stage < Stage.YELLOW:
            reasons.append("yellow_requires_escalation")
        target_campus = data.campus(lesson)
        campus_weight = p["campus_weights"][target_campus]
        if campus_weight == 0:
            reasons.append("campus_refused")
        history = [h for h in data.history if h["teacher"] == teacher and h["discipline"] == target
                   and h["group"] == lesson["group"] and h["day"] <= request.day]
        if any(h["score"] == 0 and h["id"] not in request.history_waivers
               and date.fromisoformat(request.day) < anniversary(date.fromisoformat(h["day"])) for h in history):
            reasons.append("history_zero")
        events = self.events(teacher, request.day)
        interval = data.interval(lesson)
        if any(interval[0] < e[0][1] and e[0][0] < interval[1] for e in events):
            reasons.append("time_conflict")
        before = [e for e in events if e[0][1] <= interval[0]]
        after = [e for e in events if e[0][0] >= interval[1]]
        prev, nxt = (before[-1] if before else None), (after[0] if after else None)

        def location(campus):
            issues, travel_minutes = [], 0
            crossing = any(e and e[1] != campus for e in (prev, nxt))
            if not events:
                window = "full_day"
            elif crossing:
                window = "cross_campus"
            elif prev and nxt:
                window = "inner_gap"
            else:
                # Непосредственное соседство — по номеру слота сетки.
                adjacent = any(e and any(abs(int(s) - int(lesson["slot"])) == 1
                                          and (minute_pair[0], minute_pair[1]) == e[0]
                                          for s, minute_pair in slot_intervals.items()) for e in (prev, nxt))
                window = "adjacent" if adjacent else "outer_gap"
            if window == "outer_gap" and stage < Stage.OUTER_GAP:
                issues.append("outer_gap_requires_escalation")
            if crossing and stage < Stage.CROSS_CAMPUS:
                issues.append("cross_campus_requires_escalation")
            for source, destination, gap in (
                (prev[1], campus, interval[0] - prev[0][1]) if prev else (campus, campus, 0),
                (campus, nxt[1], nxt[0][0] - interval[1]) if nxt else (campus, campus, 0),
            ):
                travel = self.travel(source, destination)
                if travel is None:
                    issues.append("travel_time_missing")
                elif travel > gap:
                    issues.append("travel_time_insufficient")
                else:
                    travel_minutes += travel
            return window, travel_minutes, issues

        slot_intervals = {s: data.interval({"slot": s}) for s in data.slots}
        window, travel_minutes, location_reasons = location(target_campus)
        online = False
        campus = target_campus
        # Онлайн разрешается только явно: пара поддерживает его, секретарь задал место.
        if location_reasons and stage >= Stage.ONLINE and lesson.get("online_allowed", False) and teacher in request.online_campuses:
            campus = request.online_campuses[teacher]
            window, travel_minutes, location_reasons = location(campus)
            online = True
        reasons.extend(location_reasons)
        if reasons:
            return None, sorted(set(reasons))
        history_score = sum(h["score"] for h in history) / len(history) if history else 3
        features = {
            "psz_mismatch": 0 if exact else (0.5 if related else 1),
            "comp_level": {"priority": 0, "ready": 1/3, "prepare": 2/3}.get(comp["level"] if comp else "", 1),
            "traffic_light": 0 if light == "green" else 1,
            "window_priority": {"inner_gap": 0, "adjacent": 0.25, "full_day": 0.5, "outer_gap": 0.75, "cross_campus": 1}[window],
            # Большая желательность площадки должна УМЕНЬШАТЬ стоимость.
            "campus_priority": 1 - campus_weight,
            "travel_penalty": min(1, travel_minutes / 120),
            "consecutive_priority": 0 if teacher in request.preferred else 1,
            "history_score": history_score / 5,
            "preference": comp.get("priority", 0) if comp else 0,
        }
        weights = self.policy.weights
        cost = round(1_000_000 * sum(weights[k] * v * (-1 if k in {"history_score", "preference"} else 1)
                                      for k, v in features.items()) / sum(weights.values()))
        return Candidate(teacher, lid, cost, features, window, campus, online), []

    def conflict(self, assignment, request, stage=Stage.RAISE_CAP):
        for teacher in sorted({c.teacher for c in assignment.values()}):
            events = self.events(teacher, request.day)
            for lid, c in assignment.items():
                if c.teacher == teacher:
                    events.append((self.data.interval(self.data.lessons[lid]), c.campus, (teacher, lid)))
            events.sort(key=lambda e: e[0])
            for left, right in zip(events, events[1:]):
                if left[2] is None and right[2] is None:
                    continue
                travel = self.travel(left[1], right[1])
                if (left[1] != right[1] and stage < Stage.CROSS_CAMPUS) or travel is None or left[0][1] + travel > right[0][0]:
                    return tuple(e[2] for e in (left, right) if e[2] is not None)
        return None

    def optimize(self, request, stage, candidates, forced=None):
        """Поток — нижняя оценка. Ветвление устраняет конфликты переездов."""
        forced = forced or {}
        if len(set(forced)) != len(forced):
            raise ValueError("Повторяющиеся обязательные пары")
        by_key = {(c.teacher, c.lesson): c for c in candidates}
        if any((p, l) not in by_key for l, p in forced.items()):
            return {}, 0

        def relaxed(forbidden):
            graph = FlowGraph()
            refs = {}
            fixed = {lid: by_key[p, lid] for lid, p in forced.items()}
            used_slots = {(c.teacher, str(self.data.lessons[c.lesson]["slot"])) for c in fixed.values()}
            if len(used_slots) != len(fixed) or any((c.teacher, c.lesson) in forbidden for c in fixed.values()):
                return None
            for p in sorted(self.data.teachers):
                remaining_cap = self.cap(p, request, stage) - sum(c.teacher == p for c in fixed.values())
                if remaining_cap < 0:
                    return None
                graph.add("S", ("p", p), remaining_cap)
                for slot in self.data.slots:
                    graph.add(("p", p), ("pt", p, slot), int((p, slot) not in used_slots))
            for lid in request.lesson_ids:
                graph.add(("l", lid), "T", int(lid not in forced))
            for key, c in sorted(by_key.items()):
                if key in forbidden or c.lesson in forced:
                    continue
                slot = str(self.data.lessons[c.lesson]["slot"])
                refs[key] = graph.add(("pt", c.teacher, slot), ("l", c.lesson), 1, c.cost)
            graph.solve("S", "T", len(request.lesson_ids) - len(fixed))
            solution = {lid: by_key[p, lid] for (p, lid), edge in refs.items() if edge.capacity == 0}
            solution.update(fixed)
            return solution, sum(c.cost for c in solution.values())

        initial = relaxed(frozenset())
        if initial is None:
            return {}, 0
        seq = itertools.count()
        queue = [(-len(initial[0]), initial[1], next(seq), frozenset(), initial[0])]
        seen = {frozenset()}
        explored = 0
        while queue:
            negative_flow, cost, _, forbidden, solution = heapq.heappop(queue)
            explored += 1
            if explored > self.policy.max_search_nodes:
                raise SearchLimit("Достигнут предел поиска; требуется ручное решение или увеличение max_search_nodes")
            conflict = self.conflict(solution, request, stage)
            if not conflict:
                return solution, cost
            for key in conflict:
                if forced.get(key[1]) == key[0]:
                    continue
                child = forbidden | {key}
                if child in seen:
                    continue
                seen.add(child)
                result = relaxed(child)
                if result is not None:
                    assignment, total = result
                    heapq.heappush(queue, (-len(assignment), total, next(seq), child, assignment))
        return {}, 0

    def plan(self, request: Request, excluded=None, start_stage=Stage.BASE):
        request.validate(self.data)
        excluded = set(excluded or [])
        audit = []
        for stage in Stage:
            if stage < start_stage:
                continue
            candidates, rejected = [], {}
            for lid in request.lesson_ids:
                rejected[lid] = {}
                for p in sorted(self.data.teachers):
                    c, reasons = self.evaluate(request, lid, p, stage)
                    if (p, lid) in excluded:
                        c, reasons = None, reasons + ["declined_or_timed_out"]
                    if c:
                        candidates.append(c)
                    else:
                        rejected[lid][p] = reasons
            solution, total = self.optimize(request, stage, candidates)
            audit.append({"stage": stage.name, "covered": len(solution), "required": len(request.lesson_ids),
                          "cost": total, "rejected": rejected,
                          "candidates": [asdict(c) for c in candidates], "weights": self.policy.weights})
            if len(solution) == len(request.lesson_ids) or stage == Stage.RAISE_CAP:
                break
        ranked = {lid: [] for lid in request.lesson_ids}
        for c in candidates:
            alternative, global_cost = self.optimize(request, stage, candidates, {c.lesson: c.teacher})
            ranked[c.lesson].append({**asdict(c), "global_coverage": len(alternative),
                                    "global_cost": global_cost,
                                    "allows_full_coverage": len(alternative) == len(request.lesson_ids)})
        for values in ranked.values():
            values.sort(key=lambda c: (-c["global_coverage"], c["global_cost"], c["cost"], c["teacher"]))
        return {"stage": stage.name, "complete": len(solution) == len(request.lesson_ids),
                "assignments": {lid: asdict(c) for lid, c in solution.items()}, "cost": total,
                "ranked": ranked, "audit": audit}

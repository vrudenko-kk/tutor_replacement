import copy
from datetime import timedelta
import itertools
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest

from substitution import Actor, Dataset, Planner, Policy, Request, Service, Stage
from substitution.model import aware, anniversary
from substitution.planner import Candidate, SearchLimit


ROOT = Path(__file__).resolve().parent.parent
NOW = aware("2026-10-05T15:00:00+03:00")
SECRETARY = Actor("secretary-1", "secretary", "informatics")


def fixture():
    raw = json.loads((ROOT / "examples/scenario.json").read_text())
    return Dataset.from_dict(raw["data"]), Request(**raw["request"])


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.data, self.request = fixture()

    def evaluate(self, teacher="anna", lesson="L1", stage=Stage.BASE):
        return Planner(self.data).evaluate(self.request, lesson, teacher, stage)

    def test_hard_bans_survive_every_escalation(self):
        changes = {
            "traffic_red": lambda d: d.teachers["anna"]["traffic"].update({"2026-10-06/2": "red"}),
            "campus_refused": lambda d: d.teachers["anna"]["campus_weights"].update(A=0),
            "competence_refused": lambda d: d.teachers["anna"]["competences"]["python"].update(level="refuse"),
            "department": lambda d: d.teachers["anna"]["positions"][0].update(end="2026-10-05"),
        }
        for reason, change in changes.items():
            with self.subTest(reason=reason):
                self.data, self.request = fixture()
                change(self.data)
                self.assertIn(reason, self.evaluate(stage=Stage.RAISE_CAP)[1])
        self.assertIn("initiator", self.evaluate("initiator")[1])

    def test_general_needs_no_competence_but_honors_refusal(self):
        self.request.psz["L1"] = "general"
        self.data.teachers["anna"]["competences"] = {}
        self.assertIsNotNone(self.evaluate()[0])
        self.data.teachers["anna"]["competences"] = {"python": {"level": "refuse"}}
        self.assertIn("competence_refused", self.evaluate()[1])

    def test_related_groups_and_missing_group_escalation(self):
        self.request.psz["L1"] = "related"
        self.data.teachers["anna"]["competences"] = {"java": {"level": "ready"}}
        self.assertIsNotNone(self.evaluate()[0])
        self.data.disciplines["python"]["related_groups"] = []
        self.assertIn("related_competence", self.evaluate()[1])
        self.assertIsNotNone(self.evaluate(stage=Stage.ALL_RELATED)[0])
        self.request.psz["L1"] = "disciplinary"
        self.assertIn("disciplinary_competence", self.evaluate(stage=Stage.RAISE_CAP)[1])

    def test_yellow_and_unknown_require_escalation(self):
        self.data.teachers["anna"]["traffic"].pop("2026-10-06/2")
        self.assertIn("yellow_requires_escalation", self.evaluate()[1])
        self.assertIsNotNone(self.evaluate(stage=Stage.YELLOW)[0])
        self.assertIsNotNone(Planner(self.data, Policy(yellow_at_base=True)).evaluate(self.request, "L1", "anna", Stage.BASE)[0])

    def test_history_zero_expiration_and_neutral(self):
        self.data.history = [{"id": "H1", "teacher": "anna", "discipline": "python", "group": "ИС-101", "day": "2025-10-07", "score": 0}]
        self.assertIn("history_zero", self.evaluate(stage=Stage.RAISE_CAP)[1])
        self.request.history_waivers = ["H1"]
        self.assertIsNotNone(self.evaluate()[0])
        self.request.history_waivers = []
        self.data.history[0]["day"] = "2025-10-06"
        self.assertIsNotNone(self.evaluate()[0])
        self.data.history = []
        self.assertEqual(self.evaluate()[0].features["history_score"], 0.6)
        self.assertEqual(str(anniversary(__import__('datetime').date(2024, 2, 29))), "2025-02-28")

    def test_windows_and_campus_cost_direction(self):
        self.assertEqual(self.evaluate()[0].window, "inner_gap")
        del self.data.lessons["A4"]
        self.assertEqual(self.evaluate()[0].window, "adjacent")
        self.assertIn("outer_gap_requires_escalation", self.evaluate(lesson="L2")[1])
        self.assertEqual(self.evaluate(lesson="L2", stage=Stage.OUTER_GAP)[0].window, "outer_gap")
        del self.data.lessons["A1"]
        self.assertEqual(self.evaluate()[0].window, "full_day")
        original_cost = self.evaluate()[0].cost
        self.data.teachers["anna"]["campus_weights"]["A"] = 0.1
        self.assertGreater(self.evaluate()[0].cost, original_cost)

    def test_travel_checks_both_sides_and_missing_direction(self):
        self.data.lessons["A4"]["campus"] = "B"
        self.assertIn("cross_campus_requires_escalation", self.evaluate()[1])
        self.assertIsNotNone(self.evaluate(stage=Stage.CROSS_CAMPUS)[0])
        self.assertIn("travel_time_insufficient", self.evaluate(lesson="L2", stage=Stage.CROSS_CAMPUS)[1])
        self.data.travel["A"].pop("B")
        self.assertIn("travel_time_missing", self.evaluate(stage=Stage.CROSS_CAMPUS)[1])

    def test_online_is_explicit_and_preserves_travel(self):
        self.data.lessons["A1"]["campus"] = "B"
        self.data.lessons["A4"]["campus"] = "B"
        self.assertIn("travel_time_insufficient", self.evaluate(stage=Stage.ONLINE)[1])
        self.request.online_campuses = {"anna": "B"}
        candidate, reasons = self.evaluate(stage=Stage.ONLINE)
        self.assertFalse(reasons)
        self.assertTrue(candidate.online)
        self.assertEqual(candidate.campus, "B")

    def test_confirmed_commitments_and_explicit_cap(self):
        commitment = {"teacher": "boris", "lesson": "L2", "campus": "A"}
        planner = Planner(self.data, commitments=[commitment])
        self.assertIn("daily_cap", planner.evaluate(self.request, "L1", "boris", Stage.RAISE_CAP)[1])
        self.request.cap_overrides = {"boris": 2}
        self.assertIsNotNone(planner.evaluate(self.request, "L1", "boris", Stage.RAISE_CAP)[0])
        self.assertIn("time_conflict", planner.evaluate(self.request, "L2", "boris", Stage.RAISE_CAP)[1])

    def test_request_rejects_mixed_dates_duplicates_and_late_deadline(self):
        for mutate in (lambda r: r.lesson_ids.append("L1"),
                       lambda r: setattr(r, "day", "2026-10-07"),
                       lambda r: setattr(r, "deadline", "2026-10-06T10:11:00+03:00"),
                       lambda r: setattr(r, "deadline", "2026-10-06T09:00:00")):
            with self.subTest(mutate=mutate):
                request = copy.deepcopy(self.request)
                mutate(request)
                with self.assertRaises(ValueError):
                    request.validate(self.data)


class SolverTests(unittest.TestCase):
    def test_min_cost_assignment_avoids_greedy_dead_end(self):
        data, request = fixture()
        data.teachers["anna"]["daily_cap"] = 1
        data.teachers["vera"]["daily_cap"] = 0
        result = Planner(data).plan(request)
        self.assertTrue(result["complete"])
        self.assertEqual(result["assignments"]["L1"]["teacher"], "boris")
        self.assertEqual(result["assignments"]["L2"]["teacher"], "anna")
        anna = next(c for c in result["ranked"]["L1"] if c["teacher"] == "anna")
        self.assertFalse(anna["allows_full_coverage"])
        self.assertEqual(anna["global_coverage"], 1)

    def test_travel_between_new_replacements(self):
        data, request = fixture()
        del data.lessons["A1"], data.lessons["A4"]
        data.lessons["L1"]["slot"] = 3
        data.lessons["L2"]["slot"] = 4
        data.lessons["L2"]["campus"] = "B"
        result = Planner(data).plan(request)
        self.assertTrue(result["complete"])
        self.assertNotEqual(result["assignments"]["L1"]["teacher"], result["assignments"]["L2"]["teacher"])
        data.teachers["boris"]["daily_cap"] = data.teachers["vera"]["daily_cap"] = 0
        result = Planner(data).plan(request)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["assignments"]), 1)

    def test_search_limit_is_not_reported_as_infeasible(self):
        data, request = fixture()
        del data.lessons["A1"], data.lessons["A4"]
        data.lessons["L1"]["slot"] = 3
        data.lessons["L2"]["slot"] = 4
        data.lessons["L2"]["campus"] = "B"
        data.teachers["boris"]["daily_cap"] = data.teachers["vera"]["daily_cap"] = 0
        with self.assertRaises(SearchLimit):
            Planner(data, Policy(max_search_nodes=1)).plan(request)

    def test_feasible_travel_between_new_pairs_requires_escalation(self):
        data, request = fixture()
        del data.lessons["A1"], data.lessons["A4"]
        data.lessons["L2"].update(slot=4, campus="B")
        data.teachers["boris"]["daily_cap"] = data.teachers["vera"]["daily_cap"] = 0
        result = Planner(data).plan(request)
        self.assertTrue(result["complete"])
        self.assertEqual(result["stage"], "CROSS_CAMPUS")

    def test_capacity_escalation_needs_authorization(self):
        data, request = fixture()
        data.teachers["anna"]["daily_cap"] = 1
        data.teachers["boris"]["daily_cap"] = data.teachers["vera"]["daily_cap"] = 0
        self.assertFalse(Planner(data).plan(request)["complete"])
        request.cap_overrides = {"anna": 2}
        result = Planner(data).plan(request)
        self.assertTrue(result["complete"])
        self.assertEqual(result["stage"], "RAISE_CAP")

    def test_solver_matches_exhaustive_search_100_instances(self):
        rng = random.Random(2048)
        for case in range(100):
            data, request = fixture()
            data.lessons = {"L1": data.lessons["L1"], "L2": data.lessons["L2"]}
            for i in (3, 4):
                data.lessons[f"L{i}"] = {**data.lessons["L1"], "slot": i + 1}
            request.lesson_ids = list(data.lessons)
            teachers = ["anna", "boris", "vera"]
            for t in teachers:
                data.teachers[t]["daily_cap"] = rng.randrange(1, 4)
            for l in data.lessons.values():
                l["campus"] = rng.choice(data.campuses)
            edges = [Candidate(t, l, rng.randrange(-100, 101), {}, "full_day", data.campus(data.lessons[l]), False)
                     for t in teachers for l in request.lesson_ids if rng.random() < 0.8]
            planner = Planner(data)
            forced = {"L1": edges[0].teacher} if case % 3 == 0 and edges and edges[0].lesson == "L1" else {}
            choices = [[None] + [e for e in edges if e.lesson == l] for l in request.lesson_ids]
            best = None
            for combination in itertools.product(*choices):
                picked = {e.lesson: e for e in combination if e}
                if any(l not in picked or picked[l].teacher != t for l, t in forced.items()):
                    continue
                if any(sum(e.teacher == t for e in picked.values()) > data.teachers[t]["daily_cap"] for t in teachers):
                    continue
                if planner.conflict(picked, request, Stage.BASE):
                    continue
                score = (-len(picked), sum(e.cost for e in picked.values()))
                best = score if best is None else min(best, score)
            assignment, cost = planner.optimize(request, Stage.BASE, edges, forced)
            with self.subTest(case=case):
                self.assertEqual((-len(assignment), cost), best or (0, 0))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.data, self.request = fixture()
        self.service = Service(":memory:", self.data)

    def tearDown(self):
        self.service.close()

    def start(self, parallel=False):
        self.request.mode = "parallel" if parallel else "sequential"
        self.service.create(self.request, SECRETARY, NOW)
        self.service.start("R1", SECRETARY, NOW)

    def offers(self, status="proposed", teacher=None, lesson=None):
        return [o for o in self.service.snapshot()["offers"].values() if o["status"] == status
                and (teacher is None or o["teacher"] == teacher) and (lesson is None or o["lesson"] == lesson)]

    def test_complete_batch_persists_audit_and_notifications(self):
        self.start()
        ids = [o["id"] for o in self.offers(teacher="anna")]
        actor = Actor("anna", "teacher")
        self.service.interested(ids, actor, NOW)
        result = self.service.take(ids, actor, NOW)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(self.service.snapshot()["assignments"]), 2)
        self.assertIn("request_confirmed", [a["action"] for a in self.service.audit()])
        self.assertTrue(all(o["status"] == "pending" for o in self.service.snapshot()["outbox"]))

    def test_take_requires_interest_and_full_request_only(self):
        self.start()
        offer = self.offers()[0]
        actor = Actor(offer["teacher"], "teacher")
        with self.assertRaises(ValueError):
            self.service.take([offer["id"]], actor, NOW)
        self.service.interested([offer["id"]], actor, NOW)
        result = self.service.take([offer["id"]], actor, NOW)
        self.assertNotEqual(result["status"], "confirmed")
        self.assertEqual(len(self.service.snapshot()["assignments"]), 1)

    def test_decline_advances_sequential_queue(self):
        self.start()
        offer = self.offers(lesson="L1")[0]
        self.service.decline(offer["id"], Actor(offer["teacher"], "teacher"), NOW)
        successor = self.offers(lesson="L1")
        self.assertEqual(len(successor), 1)
        self.assertNotEqual(successor[0]["teacher"], offer["teacher"])

    def test_parallel_first_take_withdraws_only_same_lesson(self):
        self.start(parallel=True)
        candidates = self.offers(lesson="L1")
        for offer in candidates:
            self.service.interested([offer["id"]], Actor(offer["teacher"], "teacher"), NOW)
        first = candidates[0]
        self.service.take([first["id"]], Actor(first["teacher"], "teacher"), NOW)
        self.assertFalse(self.offers("interested", lesson="L1"))
        self.assertTrue(self.offers(lesson="L2"))
        self.assertEqual(len(self.offers("withdrawn", lesson="L1")), len(candidates) - 1)

    def test_timeout_releases_reservation_at_exact_boundary(self):
        self.start()
        offer = self.offers(lesson="L1")[0]
        self.service.interested([offer["id"]], Actor(offer["teacher"], "teacher"), NOW)
        self.service.poll(NOW + timedelta(minutes=15))
        self.assertEqual(self.service.snapshot()["offers"][offer["id"]]["status"], "timeout")
        self.assertFalse(self.offers("interested"))
        self.assertTrue(self.offers(lesson="L1"))

    def test_deadline_stops_offers(self):
        self.start()
        self.service.poll(aware(self.request.deadline))
        self.assertEqual(self.service.snapshot()["requests"]["R1"]["status"], "failed")
        self.assertFalse(self.offers())

    def test_roles_and_duplicate_requests(self):
        self.service.create(self.request, Actor("initiator", "teacher"), NOW)
        with self.assertRaises(PermissionError):
            self.service.start("R1", Actor("bad", "secretary", "other"), NOW)
        duplicate = copy.deepcopy(self.request)
        duplicate.id = "R2"
        with self.assertRaises(ValueError):
            self.service.create(duplicate, SECRETARY, NOW)

    def test_secretary_sets_mode_and_timeout(self):
        self.request.mode = "parallel"
        with self.assertRaises(PermissionError):
            self.service.create(self.request, Actor("initiator", "teacher"), NOW)
        self.request.mode = "sequential"
        self.service.create(self.request, Actor("initiator", "teacher"), NOW)
        self.service.start("R1", SECRETARY, NOW, mode="parallel", timeout_minutes=7)
        self.assertGreater(len(self.offers(lesson="L1")), 1)
        self.assertTrue(all(aware(o["expires_at"]) == NOW + timedelta(minutes=7) for o in self.offers()))

    def test_interest_blocks_same_slot_in_other_request(self):
        self.service.close()
        self.data.teachers["other"] = copy.deepcopy(self.data.teachers["initiator"])
        self.data.lessons["L3"] = {**self.data.lessons["L1"], "teacher": "other"}
        self.service = Service(":memory:", self.data)
        self.start()
        offer = self.offers(teacher="anna", lesson="L1")[0]
        self.service.interested([offer["id"]], Actor("anna", "teacher"), NOW)
        second = copy.deepcopy(self.request)
        second.id, second.initiator = "R2", "other"
        second.lesson_ids, second.psz = ["L3"], {"L3": "disciplinary"}
        self.service.create(second, SECRETARY, NOW)
        self.service.start("R2", SECRETARY, NOW)
        self.assertFalse(self.offers(teacher="anna", lesson="L3"))
        self.assertTrue(self.offers(teacher="boris", lesson="L3"))

    def test_first_day_substitute_preference_is_episode_scoped(self):
        self.service.close()
        for lid in ("L1", "L2"):
            self.data.lessons[lid + "next"] = {**self.data.lessons[lid], "day": "2026-10-07"}
        self.service = Service(":memory:", self.data)
        self.start()
        ids = [o["id"] for o in self.offers(teacher="anna")]
        self.service.interested(ids, Actor("anna", "teacher"), NOW)
        self.service.take(ids, Actor("anna", "teacher"), NOW)
        second = copy.deepcopy(self.request)
        second.id, second.day, second.deadline = "R2", "2026-10-07", "2026-10-07T10:00:00+03:00"
        second.lesson_ids = ["L1next", "L2next"]
        second.psz = {"L1next": "disciplinary", "L2next": "disciplinary"}
        self.service.create(second, SECRETARY, NOW)
        plan = self.service.start("R2", SECRETARY, NOW)
        anna = next(c for c in plan["ranked"]["L1next"] if c["teacher"] == "anna")
        boris = next(c for c in plan["ranked"]["L1next"] if c["teacher"] == "boris")
        self.assertEqual(anna["features"]["consecutive_priority"], 0)
        self.assertEqual(boris["features"]["consecutive_priority"], 1)

    def test_atomic_batch_rejects_over_capacity_without_partial_locks(self):
        self.start()
        ids = [o["id"] for o in self.offers(teacher="anna")]
        # Имитируем изменение дневного лимита в импортированных данных до ответа.
        with self.service.transaction(SECRETARY, NOW) as (state, now):
            state["data"]["teachers"]["anna"]["daily_cap"] = 1
        with self.assertRaises(ValueError):
            self.service.interested(ids, Actor("anna", "teacher"), NOW)
        self.assertFalse(self.offers("interested"))
        self.assertEqual(len(self.offers(teacher="anna")), 2)

    def test_manual_split_confirms_only_remaining_parent(self):
        self.start()
        self.service.manual_assign("R1", "L1", "anna", SECRETARY, "Кандидат подтвердил по телефону", NOW)
        child = self.service.split("R1", ["L2"], "R2", SECRETARY, "Сложная замена отдельно", NOW)
        self.assertEqual(child["status"], "manual_required")
        self.assertEqual(self.service.snapshot()["requests"]["R1"]["status"], "confirmed")

    def test_authorized_waiver_does_not_mutate_history(self):
        self.service.close()
        self.data.history = [{"id": "H", "teacher": "anna", "discipline": "python", "group": "ИС-101", "day": "2026-10-01", "score": 0}]
        self.service = Service(":memory:", self.data)
        self.start()
        self.service.authorize("R1", SECRETARY, "Повторное проведение одобрено", history_waivers=["H"], now=NOW)
        self.assertEqual(self.service.snapshot()["data"]["history"][0]["score"], 0)
        self.assertIn("H", self.service.snapshot()["requests"]["R1"]["history_waivers"])

    def test_message_has_no_notification(self):
        self.start()
        count = len(self.service.snapshot()["outbox"])
        self.service.message("R1", "Материалы доступны", Actor("initiator", "teacher"), NOW)
        self.assertEqual(len(self.service.snapshot()["outbox"]), count)

    def test_restart_preserves_interest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            service = Service(path, self.data)
            service.create(self.request, SECRETARY, NOW)
            service.start("R1", SECRETARY, NOW)
            offer = next(iter(service.snapshot()["offers"].values()))
            service.interested([offer["id"]], Actor(offer["teacher"], "teacher"), NOW)
            service.close()
            service = Service(path)
            self.assertEqual(service.snapshot()["offers"][offer["id"]]["status"], "interested")
            service.close()

    def test_two_connections_cannot_accept_same_lesson(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "race.sqlite"
            service = Service(path, self.data)
            self.request.mode = "parallel"
            service.create(self.request, SECRETARY, NOW)
            service.start("R1", SECRETARY, NOW)
            offers = [o for o in service.snapshot()["offers"].values() if o["lesson"] == "L1"]
            for offer in offers:
                service.interested([offer["id"]], Actor(offer["teacher"], "teacher"), NOW)
            barrier = threading.Barrier(len(offers))
            results = []
            def accept(offer):
                connection = Service(path)
                barrier.wait(timeout=10)
                try:
                    connection.take([offer["id"]], Actor(offer["teacher"], "teacher"), NOW)
                    results.append("accepted")
                except ValueError:
                    results.append("rejected")
                finally:
                    connection.close()
            threads = [threading.Thread(target=accept, args=(o,)) for o in offers]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
                self.assertFalse(t.is_alive())
            self.assertEqual(results.count("accepted"), 1)
            self.assertEqual(results.count("rejected"), len(offers) - 1)
            self.assertIn("L1", service.snapshot()["assignments"])
            service.close()


if __name__ == "__main__":
    unittest.main()

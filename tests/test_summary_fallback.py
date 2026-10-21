import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from spiritboard.model import now
from spiritboard.summaries import Summarizer


class Providers:
    def __init__(self, routes: list[str]):
        self.names = routes
        self.revision = 0
        self.enabled = True
        self.responses: dict[str, dict | Exception] = {}
        self.requests: list[str] = []
        self.active = None
        self.pause_during_request = False

    def public_state(self):
        return {"enabled": self.enabled, "active_provider": self.active}

    def routes(self):
        return self.names if self.enabled else []

    def allowed(self, name: str, revision: int):
        return revision == self.revision and name in self.routes()

    def model(self, name: str):
        return "gpt-6-luna" if name == "codex" else "deepseek/deepseek-v4-flash"

    def billing_kind(self, name: str):
        return "subscription" if name == "codex" else "api"

    def request(self, name: str, payload: dict, timeout: float):
        self.requests.append(name)
        self.payload = payload
        if self.pause_during_request:
            self.enabled = False
            self.revision += 1
        result = self.responses[name]
        if isinstance(result, Exception):
            raise result
        return result

    def record_success(self, name: str):
        self.active = name


def response(cost: float | None = None):
    return {
        "id": "synthetic-response",
        "usage": {"cost": cost} if cost is not None else {},
        "choices": [{"message": {"content": json.dumps({"r1": "Investigating retry behavior"})}}],
    }


class SummaryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Mock()
        self.store.snapshot.return_value = {
            "runs": [
                {
                    "run_id": "synthetic-run",
                    "parent_run_id": None,
                    "source": "sdk",
                    "state": "working",
                    "last_seen": now(),
                    "task": "Fix the invented retry behavior",
                    "activity": "Reading fixtures",
                }
            ]
        }

    def create(self, providers: Providers):
        summarizer = Summarizer(
            lambda: [],
            self.store,
            providers=providers,
            state_path=Path(self.temporary.name) / "summaries.sqlite3",
        )
        self.addCleanup(summarizer.db.close)
        self.addCleanup(summarizer.stop)
        return summarizer

    def test_codex_only_uses_plan_calls_not_fake_zero_dollar_costs(self):
        providers = Providers(["codex"])
        providers.responses["codex"] = response(123.0)
        summarizer = self.create(providers)
        summarizer.tick()
        state = summarizer.public_state()
        self.assertEqual(providers.requests, ["codex"])
        self.assertEqual(state["subscription_calls"], 1)
        self.assertEqual(state["api_calls"], 0)
        self.assertIsNone(state["actual_cost_usd"])
        self.assertEqual(state["unknown_cost_calls"], 0)
        self.assertEqual(state["summaries"][0]["model"], "gpt-6-luna")
        self.assertEqual(state["summaries"][0]["provider"], "codex")
        self.assertIsNone(state["summaries"][0]["actual_cost_usd"])

    def test_or_failure_falls_back_and_journals_both_attempts_honestly(self):
        providers = Providers(["openrouter", "codex"])
        providers.responses = {
            "openrouter": ValueError("synthetic upstream failure"),
            "codex": response(),
        }
        summarizer = self.create(providers)
        summarizer.tick()
        state = summarizer.public_state()
        self.assertEqual(providers.requests, ["openrouter", "codex"])
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["unknown_cost_calls"], 1)
        self.assertEqual(state["subscription_calls"], 1)
        self.assertEqual(state["provider"], "codex")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(len(state["summaries"]), 1)

    def test_or_success_never_spends_a_second_call_on_codex(self):
        providers = Providers(["openrouter", "codex"])
        providers.responses["openrouter"] = response(0.001)
        summarizer = self.create(providers)
        summarizer.tick()
        self.assertEqual(providers.requests, ["openrouter"])
        self.assertEqual(summarizer.public_state()["actual_cost_usd"], 0.001)

    def test_invalid_or_output_preserves_charge_and_uses_same_fallback_context(self):
        providers = Providers(["openrouter", "codex"])
        invalid = response(0.002)
        invalid["choices"][0]["message"]["content"] = '{"unexpected":"not our session"}'
        providers.responses = {"openrouter": invalid, "codex": response()}
        summarizer = self.create(providers)
        summarizer.tick()
        state = summarizer.public_state()
        self.assertEqual(providers.requests, ["openrouter", "codex"])
        self.assertEqual(state["known_cost_usd"], 0.002)
        self.assertEqual(state["unknown_cost_calls"], 0)
        self.assertEqual(state["subscription_calls"], 1)
        self.assertEqual(state["summaries"][0]["provider"], "codex")

    def test_both_failures_are_recorded_without_hidden_immediate_retries(self):
        providers = Providers(["openrouter", "codex"])
        providers.responses = {name: ValueError("synthetic failure") for name in providers.names}
        summarizer = self.create(providers)
        summarizer.tick()
        summarizer.tick()
        state = summarizer.public_state()
        self.assertEqual(providers.requests, ["openrouter", "codex"])
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["unknown_cost_calls"], 1)
        self.assertEqual(state["subscription_calls"], 1)
        self.assertEqual(state["summaries"], [])
        self.assertIsNone(state["actual_cost_usd"])

    def test_unconnected_and_paused_never_collect_context_or_dispatch(self):
        providers = Providers([])
        providers.enabled = False
        summarizer = self.create(providers)
        summarizer.tick()
        self.assertEqual(providers.requests, [])
        self.store.snapshot.assert_not_called()

    def test_pause_during_response_preserves_attempt_but_discards_late_summary(self):
        providers = Providers(["openrouter", "codex"])
        providers.pause_during_request = True
        providers.responses["openrouter"] = response(0.001)
        summarizer = self.create(providers)
        summarizer.tick()
        self.assertEqual(providers.requests, ["openrouter"])
        state = summarizer.public_state()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["calls"], 1)
        self.assertEqual(state["known_cost_usd"], 0.001)
        self.assertEqual(state["summaries"], [])

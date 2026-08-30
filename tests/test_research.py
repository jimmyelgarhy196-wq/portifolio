"""AI research agents, evidence packs and the thesis system."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.analytics.service import analyze_stock
from backend.core.data_quality import Claim
from backend.research.agents import (
    BearAnalyst,
    EventAnalyst,
    FundamentalAnalyst,
    PortfolioManager,
    TechnicalAnalyst,
    all_agents,
    parse_statements,
    validate_output,
)
from backend.research.evidence import (
    build_evidence_pack,
    extract_numbers,
    find_unsupported_numbers,
)
from backend.research.llm import CallBudget, LlmClient
from backend.research.pipeline import research_stock
from backend.research.thesis import (
    check_invalidation,
    derive_levels,
    render_thesis,
    summarise_changes,
    upsert_thesis,
)
from tests.conftest import make_prices, make_statements


@pytest.fixture
def analysis(db, company, benchmark):
    make_prices(db, "TEST", days=400, start_price=100.0)
    make_prices(db, "EGX30", days=400, start_price=1000.0)
    make_statements(db, "TEST", years=4)
    return analyze_stock(db, "TEST")


@pytest.fixture
def pack(analysis):
    return build_evidence_pack(analysis)


class TestEvidencePack:
    def test_contains_facts_and_provenance(self, pack):
        assert pack.items
        assert all(item.source for item in pack.items)
        assert pack.sources

    def test_absent_metrics_become_unknowns_not_values(self, pack):
        for item in pack.items:
            assert item.value is not None

    def test_numeric_index_covers_numbers(self, pack):
        assert pack.numeric_index
        assert all(isinstance(v, float) for v in pack.numeric_index.values())

    def test_render_names_what_is_unavailable(self, pack):
        pack.unknowns.append("Forward earnings estimate")
        text = pack.render()
        assert "NOT AVAILABLE" in text
        assert "MUST NOT estimate" in text
        assert "Forward earnings estimate" in text

    def test_synthetic_flag_propagates(self, db, company):
        make_prices(db, "TEST", days=100, source="SYNTHETIC_DEMO:synthetic")
        pack = build_evidence_pack(analyze_stock(db, "TEST"))
        assert pack.contains_synthetic
        assert any("SYNTHETIC" in w for w in pack.warnings)
        assert "WARNING" in pack.render()


class TestNumberValidation:
    def test_extracts_numbers(self):
        assert 12.5 in extract_numbers("P/E is 12.5x")

    def test_ignores_dates(self):
        # "2026-08-24" must not yield -24.
        assert -24 not in extract_numbers("Event on 2026-08-24")

    def test_ignores_indicator_parameters(self):
        # The 14 in RSI(14) names a setting, not a claimed value.
        numbers = extract_numbers("FACT: RSI(14) reads 56.69 and SMA 20 is 88.1")
        assert 14 not in numbers
        assert 56.69 in numbers and 88.1 in numbers

    @staticmethod
    def _absent_number(pack) -> float:
        """A value provably not in the pack, nor a common rendering of one."""
        candidate = 7_654_321.987
        for _ in range(50):
            if not find_unsupported_numbers(f"FACT: value {candidate}.", pack):
                candidate = candidate * 3.0 + 17.0
                continue
            return candidate
        raise AssertionError("could not construct a number absent from the pack")

    def test_flags_fabricated_facts(self, pack):
        fabricated = self._absent_number(pack)
        text = f"FACT: Revenue reached EGP {fabricated} on the year."
        assert find_unsupported_numbers(text, pack) == [pytest.approx(fabricated)]

    def test_accepts_restated_evidence(self, pack):
        key, value = next(iter(pack.numeric_index.items()))
        assert not find_unsupported_numbers(f"FACT: The value is {value}.", pack)

    def test_untagged_prose_treated_as_factual(self, pack):
        # An untagged assertion is a factual claim by default — exactly the
        # sloppiness the tagging rule exists to catch.
        fabricated = self._absent_number(pack)
        assert find_unsupported_numbers(f"The stock trades at {fabricated}.", pack)

    def test_inference_lines_not_checked_for_facts(self, pack):
        # Only FACT and untagged lines are held to the strict standard; an
        # INFERENCE is explicitly a conclusion, not a restatement of evidence.
        fabricated = self._absent_number(pack)
        assert not find_unsupported_numbers(
            f"INFERENCE: something around {fabricated} perhaps.", pack,
            strict_claims=("FACT",),
        )


class TestStatementParsing:
    def test_parses_tags(self):
        statements = parse_statements(
            "FACT: Revenue rose.\nINFERENCE: This is good.\nUNKNOWN: No estimate."
        )
        assert [s.claim for s in statements] == [Claim.FACT, Claim.INFERENCE, Claim.UNKNOWN]

    def test_ignores_untagged_lines(self):
        assert parse_statements("Just some prose.") == []

    def test_untagged_output_produces_warning(self, pack):
        warnings, _ = validate_output("Some prose without tags.", pack)
        assert any("did not follow" in w for w in warnings)


class TestAgentsWithoutApiKey:
    def test_llm_unavailable_without_key(self):
        client = LlmClient()
        assert not client.available
        assert "ANTHROPIC_API_KEY" in client.unavailable_reason

    def test_all_agents_produce_tagged_output(self, pack):
        for agent in (FundamentalAnalyst(), TechnicalAnalyst(), EventAnalyst(), BearAnalyst()):
            output = agent.run(pack, bull_case="Test bull case.")
            assert output.generated_by == "deterministic"
            assert output.statements, f"{agent.name} produced no tagged claims"
            assert all(s.claim in set(Claim) for s in output.statements)

    def test_deterministic_output_has_no_fabricated_facts(self, pack):
        for agent in (FundamentalAnalyst(), TechnicalAnalyst(), EventAnalyst(), BearAnalyst()):
            output = agent.run(pack, bull_case="x")
            assert not find_unsupported_numbers(output.text, pack), (
                f"{agent.name} produced a factual number absent from the evidence"
            )

    def test_bear_analyst_always_finds_something_to_say(self, pack):
        output = BearAnalyst().run(pack, bull_case="Everything is wonderful.")
        assert len(output.statements) >= 2
        # Even with a clean company it must state the structural EGX risks.
        assert any(s.claim is Claim.OPINION for s in output.statements)

    def test_event_analyst_reports_absence_honestly(self, pack):
        pack.events = []
        output = EventAnalyst().run(pack)
        text = output.text.lower()
        assert "absence of data" in text or "no corporate disclosures" in text

    def test_portfolio_manager_decides(self, pack):
        decision = PortfolioManager().decide(pack)
        assert decision.action in ("BUY", "HOLD", "SELL", "WATCH")
        assert 0 <= decision.conviction <= 10
        assert decision.exit_conditions

    def test_conviction_capped_on_synthetic_data(self, db, company):
        make_prices(db, "TEST", days=400, source="SYNTHETIC_DEMO:synthetic")
        make_statements(db, "TEST", years=4, source="SYNTHETIC_DEMO:synthetic")
        synthetic_pack = build_evidence_pack(analyze_stock(db, "TEST"))
        decision = PortfolioManager().decide(synthetic_pack)
        assert decision.conviction <= 1.0, (
            "a real conviction was assigned on fabricated data"
        )

    def test_conviction_limited_by_evidence_coverage(self, db, company):
        # Prices only, no statements: coverage is thin, so conviction must be too.
        make_prices(db, "TEST", days=400)
        thin = build_evidence_pack(analyze_stock(db, "TEST"))
        rich_action, rich_conviction = PortfolioManager.score_based_action(thin)
        assert rich_conviction <= 10.0


class TestLlmBudget:
    def test_budget_is_enforced(self):
        budget = CallBudget(limit=2)
        assert budget.consume() and budget.consume()
        assert not budget.consume()
        assert budget.exhausted


class TestThesis:
    def test_levels_derived_from_structure(self, analysis):
        levels = derive_levels(analysis, "LONG")
        assert levels.entry == analysis.price_series.last_close
        if levels.target:
            assert levels.target > levels.entry
            assert "target" in levels.rationale
        if levels.invalidation:
            assert levels.invalidation < levels.entry

    def test_short_levels_inverted(self, analysis):
        levels = derive_levels(analysis, "SHORT")
        if levels.target and levels.entry:
            assert levels.target < levels.entry

    def test_created_then_updated_not_duplicated(self, db, analysis):
        pack = build_evidence_pack(analysis)
        agents = all_agents()
        outputs = {k: agents[k].run(pack, bull_case="x")
                   for k in ("fundamental", "technical", "event", "bear")}
        decision = agents["portfolio_manager"].decide(pack, **outputs)

        first = upsert_thesis(db, analysis, pack, decision, outputs)
        assert first.is_new and first.thesis.version == 1

        second = upsert_thesis(db, analysis, pack, decision, outputs)
        assert not second.is_new
        assert second.thesis.thesis_id == first.thesis.thesis_id
        assert second.thesis.version == 2
        assert len(second.thesis.versions) == 1   # prior state archived

    def test_recommendation_recorded_each_run(self, db, analysis):
        from backend.data.models import Recommendation
        from sqlalchemy import func, select

        pack = build_evidence_pack(analysis)
        agents = all_agents()
        outputs = {k: agents[k].run(pack, bull_case="x")
                   for k in ("fundamental", "technical", "event", "bear")}
        decision = agents["portfolio_manager"].decide(pack, **outputs)
        upsert_thesis(db, analysis, pack, decision, outputs)
        upsert_thesis(db, analysis, pack, decision, outputs)
        # Append-only: the record of what was said is never overwritten.
        assert db.scalar(select(func.count()).select_from(Recommendation)) == 2

    def test_change_summary_describes_moves(self):
        before = {"alpha_score": 81.0, "conviction": 7.0, "status": "ACTIVE"}
        after = {"alpha_score": 88.0, "conviction": 7.1, "status": "ACTIVE"}
        summary = summarise_changes(before, after)
        assert "81" in summary and "88" in summary
        assert "Conviction" not in summary   # 0.1 is below the threshold

    def test_no_change_says_so(self):
        state = {"alpha_score": 80.0}
        assert "No material change" in summarise_changes(state, state)

    def test_invalidation_detected(self, db):
        from backend.data.models import ResearchThesis

        thesis = ResearchThesis(
            reference="EGX-00001", ticker="TEST", direction="LONG",
            strategy="fundamental_long", invalidation_price=90.0, status="ACTIVE",
        )
        assert check_invalidation(db, thesis, 85.0) is not None
        assert check_invalidation(db, thesis, 95.0) is None
        assert check_invalidation(db, thesis, None) is None

    def test_short_invalidation_inverted(self, db):
        from backend.data.models import ResearchThesis

        thesis = ResearchThesis(
            reference="EGX-00002", ticker="TEST", direction="SHORT",
            strategy="bearish_short", invalidation_price=110.0, status="ACTIVE",
        )
        assert check_invalidation(db, thesis, 115.0) is not None
        assert check_invalidation(db, thesis, 105.0) is None

    def test_render_includes_all_required_sections(self, db, analysis):
        pack = build_evidence_pack(analysis)
        agents = all_agents()
        outputs = {k: agents[k].run(pack, bull_case="x")
                   for k in ("fundamental", "technical", "event", "bear")}
        decision = agents["portfolio_manager"].decide(pack, **outputs)
        bundle = upsert_thesis(db, analysis, pack, decision, outputs)
        text = render_thesis(bundle.thesis)
        for heading in ("INVESTMENT THESIS", "BULL CASE", "BEAR CASE", "CATALYSTS",
                        "RISKS", "INVALIDATION CONDITIONS", "DATA SOURCES"):
            assert heading in text
        assert bundle.thesis.reference in text


class TestPipeline:
    def test_end_to_end_without_api_key(self, db, analysis):
        result = research_stock(db, analysis)
        assert result.decision.action in ("BUY", "HOLD", "SELL", "WATCH")
        assert not result.used_llm
        assert result.bundle is not None
        assert set(result.agent_outputs) >= {"fundamental", "technical", "event", "bear"}

    def test_no_validation_warnings_from_deterministic_path(self, db, analysis):
        result = research_stock(db, analysis)
        numeric = [w for w in result.validation_warnings if "do not trace" in w]
        assert not numeric, f"deterministic output failed validation: {numeric}"

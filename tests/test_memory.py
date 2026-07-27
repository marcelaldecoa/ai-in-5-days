"""Memory tests: async consolidation, compaction policy, and retrieval."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from content_forge.memory.vector_store import LocalCorpusKnowledgeBase
from content_forge.plugins.memory_plugin import AsyncMemoryPlugin


class FakePart:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeContent:
    def __init__(self, text: str) -> None:
        self.parts = [FakePart(text)]


class FakeEvent:
    def __init__(self, text: str) -> None:
        self.content = FakeContent(text)


class FakeSession:
    def __init__(self, events: list[FakeEvent] | None = None) -> None:
        self.id = "session-1"
        self.user_id = "author-1"
        self.events = events or []


class RecordingMemoryService:
    """Captures what would be written, and how long the write took."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.written: list[FakeSession] = []

    async def add_session_to_memory(self, session: FakeSession) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.written.append(session)


class FailingMemoryService:
    async def add_session_to_memory(self, session: FakeSession) -> None:
        raise RuntimeError("memory bank unavailable")


class FakeInvocationContext:
    def __init__(self, memory_service: Any, session: FakeSession) -> None:
        self.memory_service = memory_service
        self.session = session
        self.invocation_id = "inv-1"


# ---------------------------------------------------------------------------
# Async consolidation
# ---------------------------------------------------------------------------


async def test_consolidation_does_not_block_the_turn():
    """The whole point: the user's turn must not wait on memory."""
    service = RecordingMemoryService(delay=0.25)
    plugin = AsyncMemoryPlugin()
    ctx = FakeInvocationContext(service, FakeSession([FakeEvent("I prefer a TL;DR at the top.")]))

    loop = asyncio.get_running_loop()
    start = loop.time()
    await plugin.after_run_callback(invocation_context=ctx)
    elapsed = loop.time() - start

    # Returns immediately, well before the 0.25s write completes.
    assert elapsed < 0.1, f"after_run blocked for {elapsed:.3f}s"
    assert not service.written, "the write should still be in flight"

    await plugin.close()
    assert service.written, "the write must complete in the background"


async def test_consolidation_completes_after_drain():
    service = RecordingMemoryService(delay=0.05)
    plugin = AsyncMemoryPlugin()
    ctx = FakeInvocationContext(service, FakeSession([FakeEvent("hello")]))

    await plugin.after_run_callback(invocation_context=ctx)
    await plugin.close()

    assert len(service.written) == 1


async def test_pii_is_redacted_before_it_is_memorised():
    """Memory is re-injected into future prompts, so a leak here is permanent."""
    service = RecordingMemoryService()
    plugin = AsyncMemoryPlugin()
    session = FakeSession([FakeEvent("My editor is jane.doe@example.com, call 555-867-5309.")])

    await plugin.after_run_callback(invocation_context=FakeInvocationContext(service, session))
    await plugin.close()

    stored = service.written[0].events[0].content.parts[0].text
    assert "jane.doe@example.com" not in stored
    assert "555-867-5309" not in stored
    assert "[REDACTED_EMAIL]" in stored


async def test_a_failing_write_does_not_propagate():
    """A background failure must be logged, not crash the request that spawned it."""
    plugin = AsyncMemoryPlugin()
    ctx = FakeInvocationContext(FailingMemoryService(), FakeSession([FakeEvent("x")]))

    await plugin.after_run_callback(invocation_context=ctx)
    await plugin.close()  # must not raise


async def test_tasks_are_strongly_referenced_until_done():
    """An un-referenced task can be garbage-collected mid-flight."""
    service = RecordingMemoryService(delay=0.1)
    plugin = AsyncMemoryPlugin()
    ctx = FakeInvocationContext(service, FakeSession([FakeEvent("x")]))

    await plugin.after_run_callback(invocation_context=ctx)
    assert len(plugin._pending) == 1

    await plugin.close()
    assert not plugin._pending


async def test_missing_memory_service_is_a_no_op():
    plugin = AsyncMemoryPlugin()
    ctx = FakeInvocationContext(None, FakeSession())
    await plugin.after_run_callback(invocation_context=ctx)
    assert not plugin._pending


# ---------------------------------------------------------------------------
# Compaction policy
# ---------------------------------------------------------------------------


def test_compaction_keeps_a_nonzero_overlap():
    """Zero overlap can sever a function call from its response."""
    from content_forge.memory.services import build_compaction_config

    config = build_compaction_config()
    assert config.compaction_interval > 0
    assert config.overlap_size > 0
    assert config.token_threshold and config.token_threshold > 0
    assert config.event_retention_size and config.event_retention_size > 0


def test_app_wires_compaction_and_resumability():
    from content_forge.agent import build_app

    app = build_app()
    assert app.events_compaction_config is not None
    # Resumability is required for the HITL gate: the invocation suspends
    # awaiting approval and may resume in a different process.
    assert app.resumability_config is not None
    assert app.resumability_config.is_resumable is True


def test_app_registers_all_three_plugins():
    from content_forge.agent import build_app

    names = {p.name for p in build_app().plugins}
    assert names == {"guardrail_plugin", "intent_outcome_plugin", "async_memory_plugin"}


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_local_corpus_ranks_by_relevance():
    kb = LocalCorpusKnowledgeBase()
    matches = kb.search_prior_posts(query="model routing", limit=5)
    assert matches
    assert matches[0].primary_keyword == "model routing"
    # Ranking must be monotonic, or "most similar" is meaningless.
    assert all(a.similarity >= b.similarity for a, b in zip(matches, matches[1:], strict=False))


def test_local_corpus_returns_nothing_for_unrelated_queries():
    kb = LocalCorpusKnowledgeBase()
    assert kb.search_prior_posts(query="zzzz qqqq nonexistent", limit=5) == []


def test_evidence_carries_provenance():
    kb = LocalCorpusKnowledgeBase()
    evidence = kb.gather_evidence(subtopic="prompt injection risks in LLM applications", limit=4)
    assert evidence
    for item in evidence:
        assert item.source_url.startswith("http")
        assert item.source_title
        assert item.credibility in {"primary", "reputable", "community", "unknown"}


@pytest.mark.parametrize(
    "content_type,expected_section",
    [
        ("tutorial", "Prerequisites"),
        ("case_study", "Measured results"),
        ("announcement", "What's new"),
    ],
)
def test_style_guide_varies_by_content_type(content_type, expected_section):
    kb = LocalCorpusKnowledgeBase()
    guide = kb.fetch_style_guide(topic="anything", content_type=content_type)
    assert expected_section in guide.required_sections

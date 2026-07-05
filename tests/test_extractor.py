"""
Contract tests for opencrab.ontology.extractor.LLMExtractor.

Covers:
  - _split: paragraph-boundary chunk packing (pure function)
  - _extract_chunk: JSON salvage from raw LLM text + dataclass mapping
  - extract_from_text: per-chunk failure isolation (never crashes the caller)
  - _call_llm: dual backend dispatch (api / cli / auto resolution)

Contract established by reading opencrab/cli.py:358-392 and
opencrab/mcp/tools.py:551-631 (ontology_extract): a malformed or missing LLM
response must degrade gracefully — extract_from_text always returns an
ExtractionResult (never raises), recording per-chunk failures in
`.errors` rather than propagating exceptions.
"""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.extractor import ExtractedEdge, ExtractedNode, LLMExtractor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeMessages:
    def create(self, **kwargs):
        raise AssertionError("test must override .create before calling _call_llm")


class _FakeAnthropicClient:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.messages = _FakeMessages()


@pytest.fixture
def fake_anthropic_module(monkeypatch):
    """Inject a fake `anthropic` module so backend='api' can be constructed
    without the real SDK installed (it is not a project dependency here)."""
    mod = types.ModuleType("anthropic")
    mod.Anthropic = _FakeAnthropicClient
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


def _api_response(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])


# ===========================================================================
# _split — 정상 (Normal)
# ===========================================================================

class TestSplitNormal:
    def test_packs_multiple_paragraphs_under_max(self):
        ext = LLMExtractor(backend="cli", chunk_size=20)
        p1, p2, p3 = "A" * 10, "B" * 5, "C" * 15
        text = f"{p1}\n\n{p2}\n\n{p3}"

        chunks = ext._split(text)

        # p1+p2 (10+5=15, does not exceed 20) pack into one chunk; p3 (15)
        # alone would push the running total to 30 > 20, so it starts a new chunk.
        assert chunks == [f"{p1}\n\n{p2}", p3]

    def test_single_paragraph_stays_whole(self):
        ext = LLMExtractor(backend="cli", chunk_size=100)
        assert ext._split("just one paragraph") == ["just one paragraph"]


# ===========================================================================
# _split — 오류/설계 결정 (Error / design-intent)
# ===========================================================================

class TestSplitErrors:
    def test_max_chars_smaller_than_every_paragraph_keeps_paragraphs_atomic(self):
        """When chunk_size is smaller than any single paragraph, _split does
        not further sub-split a paragraph — paragraph boundaries are the only
        split points (per docstring: 'Split text into chunks at paragraph
        boundaries'). Each paragraph becomes its own (oversized) chunk rather
        than raising or silently truncating content."""
        ext = LLMExtractor(backend="cli", chunk_size=3)
        chunks = ext._split("aaaa\n\nbbbb\n\ncccc")
        assert chunks == ["aaaa", "bbbb", "cccc"]

    def test_oversized_single_paragraph_is_not_split(self):
        ext = LLMExtractor(backend="cli", chunk_size=20)
        oversized = "Z" * 50
        assert ext._split(oversized) == [oversized]


# ===========================================================================
# _split — 엣지 (Edge)
# ===========================================================================

class TestSplitEdges:
    def test_empty_text_returns_single_empty_chunk_not_empty_list(self):
        """extract_from_text iterates `for i, chunk in enumerate(chunks)`; an
        empty list would silently skip processing with no error recorded.
        _split guarantees a non-empty list even for empty input."""
        ext = LLMExtractor(backend="cli", chunk_size=100)
        assert ext._split("") == [""]

    def test_single_char_text(self):
        ext = LLMExtractor(backend="cli", chunk_size=100)
        assert ext._split("x") == ["x"]

    def test_exact_boundary_tie_packs_together_not_split(self):
        """Sum of two paragraph lengths (separator excluded) exactly equal to
        chunk_size does not trigger a split — the condition is strict '>'."""
        ext = LLMExtractor(backend="cli", chunk_size=10)
        chunks = ext._split("11111\n\n66666")
        assert chunks == ["11111\n\n66666"]


# ===========================================================================
# _extract_chunk (JSON salvage + dataclass mapping) — 정상 (Normal)
# ===========================================================================

class TestExtractChunkNormal:
    def test_markdown_fenced_json_is_parsed(self):
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: (
            '```json\n'
            '{"nodes": [{"space": "subject", "node_type": "Agent", '
            '"node_id": "a1", "properties": {"name": "A"}}], "edges": []}\n'
            '```'
        )
        nodes, edges = ext._extract_chunk("chunk text", "src", 0)
        assert nodes == [
            ExtractedNode(space="subject", node_type="Agent", node_id="a1", properties={"name": "A"})
        ]
        assert edges == []

    def test_bare_json_object_is_parsed(self):
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: (
            '{"nodes": [], "edges": [{"from_space": "subject", "from_id": "a", '
            '"relation": "owns", "to_space": "resource", "to_id": "b"}]}'
        )
        nodes, edges = ext._extract_chunk("chunk text", "src", 0)
        assert nodes == []
        assert edges == [
            ExtractedEdge(from_space="subject", from_id="a", relation="owns",
                           to_space="resource", to_id="b", properties={})
        ]

    def test_json_embedded_in_surrounding_prose_is_salvaged(self):
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: (
            'Sure! {"nodes": [{"space": "concept", "node_type": "Concept", '
            '"node_id": "c1"}], "edges": []} Hope that helps.'
        )
        nodes, edges = ext._extract_chunk("chunk text", "src", 0)
        assert [n.node_id for n in nodes] == ["c1"]

    def test_missing_properties_key_defaults_to_empty_dict(self):
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: (
            '{"nodes": [{"space": "subject", "node_type": "Agent", "node_id": "a1"}], "edges": []}'
        )
        nodes, _ = ext._extract_chunk("chunk text", "src", 0)
        assert nodes[0].properties == {}


# ===========================================================================
# _extract_chunk / extract_from_text — 오류 (Error)
# ===========================================================================

class TestExtractChunkErrors:
    def test_no_json_found_returns_empty_without_raising(self):
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: "I could not find any entities here."
        nodes, edges = ext._extract_chunk("chunk text", "src", 0)
        assert (nodes, edges) == ([], [])

    def test_malformed_json_raises_in_extract_chunk(self):
        """_extract_chunk itself does not catch json.JSONDecodeError — the
        catching responsibility belongs to extract_from_text's per-chunk loop."""
        ext = LLMExtractor(backend="cli")
        ext._call_llm = lambda prompt: '{"nodes": [}'
        with pytest.raises(ValueError):  # json.JSONDecodeError is a ValueError subclass
            ext._extract_chunk("chunk text", "src", 0)

    def test_extract_from_text_degrades_gracefully_on_malformed_json(self):
        """Public contract (relied on by cli.py extract and the ontology_extract
        MCP tool): a malformed LLM reply must not crash extract_from_text —
        it is recorded in .errors and extraction continues."""
        ext = LLMExtractor(backend="cli", chunk_size=3000)
        ext._call_llm = lambda prompt: '{"nodes": [}'
        result = ext.extract_from_text("hello world", "src1")
        assert result.nodes == []
        assert result.edges == []
        assert len(result.errors) == 1
        assert "Expecting value" in result.errors[0]

    def test_extract_from_text_survives_backend_exception_per_chunk(self):
        """A backend-level exception (e.g. CLI nonzero exit, API error) for
        one chunk must not abort extraction of other chunks."""
        ext = LLMExtractor(backend="cli", chunk_size=5)
        calls = {"n": 0}

        def flaky_call(prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("backend exploded")
            return '{"nodes": [{"space": "subject", "node_type": "Agent", "node_id": "ok"}], "edges": []}'

        ext._call_llm = flaky_call
        text = "first paragraph long enough\n\nsecond paragraph long enough too"
        result = ext.extract_from_text(text, "src2")

        assert len(result.errors) == 1
        assert "backend exploded" in result.errors[0]
        assert [n.node_id for n in result.nodes] == ["ok"]


# ===========================================================================
# extract_from_text — 엣지 (Edge): de-duplication
# ===========================================================================

class TestExtractFromTextEdges:
    def test_duplicate_node_ids_across_chunks_are_deduplicated(self):
        ext = LLMExtractor(backend="cli", chunk_size=5)
        ext._call_llm = lambda prompt: (
            '{"nodes": [{"space": "subject", "node_type": "Agent", "node_id": "dup"}], "edges": []}'
        )
        text = "para one is long enough\n\npara two is long enough too"
        result = ext.extract_from_text(text, "src3")
        assert [n.node_id for n in result.nodes] == ["dup"]


# ===========================================================================
# _call_llm — 정상 (Normal)
# ===========================================================================

class TestCallLlmNormal:
    def test_api_backend_returns_stripped_reply_text(self, fake_anthropic_module):
        ext = LLMExtractor(api_key="test-key", backend="api", model="test-model")
        ext._client.messages.create = MagicMock(return_value=_api_response("  hello reply  "))
        assert ext._call_llm("prompt") == "hello reply"
        kwargs = ext._client.messages.create.call_args.kwargs
        assert kwargs["model"] == "test-model"
        assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]

    def test_cli_backend_returns_stripped_stdout(self, monkeypatch):
        ext = LLMExtractor(backend="cli")

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="  cli reply  ", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert ext._call_llm("prompt") == "cli reply"

    def test_backend_auto_resolves_to_api_when_key_present(self, monkeypatch, fake_anthropic_module):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "somekey")
        ext = LLMExtractor(backend="auto")
        assert ext.backend == "api"

    def test_backend_auto_resolves_to_cli_when_key_absent(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ext = LLMExtractor(backend="auto")
        assert ext.backend == "cli"


# ===========================================================================
# _call_llm — 오류 (Error)
# ===========================================================================

class TestCallLlmErrors:
    def test_api_exception_propagates(self, fake_anthropic_module):
        ext = LLMExtractor(api_key="test-key", backend="api")

        def boom(**kwargs):
            raise RuntimeError("api down")

        ext._client.messages.create = boom
        with pytest.raises(RuntimeError, match="api down"):
            ext._call_llm("prompt")

    def test_cli_nonzero_returncode_raises_runtime_error(self, monkeypatch):
        ext = LLMExtractor(backend="cli")

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="boom detail")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="claude CLI exited 1"):
            ext._call_llm("prompt")

    def test_cli_timeout_propagates(self, monkeypatch):
        ext = LLMExtractor(backend="cli")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=120)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(subprocess.TimeoutExpired):
            ext._call_llm("prompt")


# ===========================================================================
# _call_llm — 엣지 (Edge)
# ===========================================================================

class TestCallLlmEdges:
    def test_api_empty_completion_content_raises_index_error(self, fake_anthropic_module):
        """An empty `content` list from the API is not handled specially —
        it surfaces as IndexError, which extract_from_text's per-chunk
        try/except then absorbs into `.errors` (see TestExtractChunkErrors)."""
        ext = LLMExtractor(api_key="test-key", backend="api")
        ext._client.messages.create = MagicMock(return_value=types.SimpleNamespace(content=[]))
        with pytest.raises(IndexError):
            ext._call_llm("prompt")

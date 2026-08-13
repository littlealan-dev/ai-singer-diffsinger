from __future__ import annotations

"""LLM client protocol and test stub implementation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Protocol
import json

from src.backend.llm_prompt import PromptBundle

TOOL_RESULT_PREFIX = "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"


class LlmRole(str, Enum):
    """Logical model role for a single LLM request."""

    DEFAULT = "default"
    PREPROCESS = "preprocess"


class LlmClient(Protocol):
    """Protocol for LLM clients used by the orchestrator."""
    def generate(
        self,
        prompt_bundle: PromptBundle | str,
        history: List[Dict[str, str]],
        *,
        role: LlmRole = LlmRole.DEFAULT,
    ) -> str:
        """Return a model response given prompt and chat history."""
        raise NotImplementedError


@dataclass
class StaticLlmClient:
    """Static response client for testing and offline flows."""
    response_text: str
    responses: List[str] = field(default_factory=list)
    loop: bool = False
    _index: int = 0

    def generate(
        self,
        prompt_bundle: PromptBundle | str,
        history: List[Dict[str, str]],
        *,
        role: LlmRole = LlmRole.DEFAULT,
    ) -> str:
        """Return the configured static response."""
        if history:
            last = history[-1].get("content", "")
            if isinstance(last, str) and last.startswith(TOOL_RESULT_PREFIX):
                tool_output = last[len(TOOL_RESULT_PREFIX):].strip()
                return json.dumps(
                    {
                        "tool_calls": [],
                        "final_message": tool_output,
                        "include_score": False,
                    }
                )
            if self.responses and len(history) <= 2:
                self._index = 0
        if self.responses:
            response = self.responses[min(self._index, len(self.responses) - 1)]
            if self.loop:
                self._index = (self._index + 1) % len(self.responses)
            else:
                self._index += 1
            return response
        return self.response_text


@dataclass
class RegressionLlmClient:
    """Deterministic planner used only by the local browser regression harness."""

    _verse_one = {"id": "lyr_24cf7b5a2230af5e8e11", "number": "1", "name": ""}
    _solfege_verse_one = {
        "id": "lyr_71ece7634e0a27d2984d",
        "number": "SSSolfege",
        "name": "SightSinger Solfege",
    }
    _staff_derived_verse_one = {"id": "lyr_7567cc2310ec4fd83dcc", "number": "1", "name": ""}
    _chord_derived_verse_one = {"id": "lyr_34ae8f300f849b77c4a6", "number": "1", "name": ""}

    def generate(
        self,
        prompt_bundle: PromptBundle | str,
        history: List[Dict[str, str]],
        *,
        role: LlmRole = LlmRole.DEFAULT,
    ) -> str:
        del prompt_bundle
        contents = [str(item.get("content") or "") for item in history]
        # Background preprocessing may append its assistant summary after a new
        # browser message. Drive this deterministic harness from the latest user
        # entry, while preserving terminal tool-result handling below.
        latest = next(
            (
                str(item.get("content") or "")
                for item in reversed(history)
                if item.get("role") == "user"
            ),
            "",
        )
        transcript = "\n".join(contents)
        scenario = next(
            (
                name
                for name in ("basic", "solfege", "split-staff", "split-chords")
                if f"[e2e:{name}]" in transcript
            ),
            None,
        )
        if role == LlmRole.PREPROCESS and scenario == "split-staff":
            actions = [{"type": "split_voice_part", "split_shared_note_policy": "duplicate_to_all"}]
            if scenario == "split-staff":
                actions.append(
                    {
                        "type": "propagate_lyrics",
                        "verse_number": "1",
                        "source_priority": [{"part_index": 0, "voice_part_id": "soprano"}],
                        "strategy": "strict_onset",
                        "policy": "fill_missing_only",
                        "section_overrides": [],
                    }
                )
            return self._response(
                "Preparing the requested derived singing line.",
                "preprocess_voice_parts",
                {
                    "request": {
                        "plan": {
                            "targets": [
                                {
                                    "target": {"part_index": 0, "voice_part_id": "alto"},
                                    "actions": actions,
                                }
                            ]
                        }
                    }
                },
            )
        if role == LlmRole.PREPROCESS and scenario == "split-chords":
            def chord_lane(rank_index: int) -> Dict[str, object]:
                return {
                    "source": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "output": {"mode": "append_new_derived_lane"},
                    "split_coverage": "complete",
                    "sections": [
                        {
                            "start_measure": 1,
                            "end_measure": 2,
                            "mode": "derive",
                            "decision_type": "SPLIT_CHORDS_SELECT_NOTES",
                            "method": "ranked",
                            "rank_index": rank_index,
                            "rank_fallback": "skip",
                            "melody_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                            "lyric_source": {"part_index": 0, "voice_part_id": "voice part 1"},
                            "lyric_strategy": "strict_onset",
                            "lyric_policy": "replace_all",
                        }
                    ],
                }

            return self._response(
                "Preparing the requested derived singing lines.",
                "preprocess_voice_parts",
                {"request": {"plan": {"targets": [chord_lane(0), chord_lane(1)]}}},
            )

        if latest.startswith(TOOL_RESULT_PREFIX):
            return self._response("The requested operation completed.")
        if scenario is None:
            return self._response("Choose a part and verse to continue.")

        if latest.startswith("[e2e:render-solfege]"):
            return self._response(
                "Starting the solfege take.",
                "synthesize",
                {
                    "part_index": 0,
                    "lyric_selection": self._solfege_verse_one,
                    "require_solfege_lyrics": True,
                },
            )
        if latest.startswith("[e2e:render-derived]"):
            lyric_selection = (
                self._staff_derived_verse_one
                if scenario == "split-staff"
                else self._chord_derived_verse_one
            )
            return self._response(
                "Starting the derived-part take.",
                "synthesize",
                {"part_index": 1, "lyric_selection": lyric_selection},
            )
        if latest.startswith("[e2e:"):
            if scenario == "basic":
                return self._response(
                    "Starting the take.",
                    "synthesize",
                    {"part_index": 0, "lyric_selection": self._verse_one},
                )
            if scenario == "solfege":
                return self._response(
                    "Adding solfege to the active score.",
                    "add_solfege_lyric_verse",
                    {"part_id": "Solo"},
                    include_score=True,
                )
            return self._response(
                "Preparing the selected line.", "start_preprocess_voice_part_workflow", {}
            )
        if "Please sing" not in latest:
            return self._response("Choose the displayed part and verse.")
        if scenario == "basic":
            return self._response(
                "Starting the take.", "synthesize", {"part_index": 0, "lyric_selection": self._verse_one}
            )
        if scenario == "solfege":
            return self._response(
                "Adding solfege to the active score.",
                "add_solfege_lyric_verse",
                {"part_id": "Solo"},
                include_score=True,
            )
        return self._response(
            "Preparing the selected line.", "start_preprocess_voice_part_workflow", {}
        )

    @staticmethod
    def _response(
        message: str,
        tool_name: str | None = None,
        arguments: Dict[str, object] | None = None,
        *,
        include_score: bool = False,
    ) -> str:
        return json.dumps(
            {
                "tool_calls": (
                    [{"name": tool_name, "arguments": arguments or {}}] if tool_name else []
                ),
                "final_message": message,
                "include_score": include_score,
            }
        )

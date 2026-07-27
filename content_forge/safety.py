"""Model-level safety settings and sampling configuration.

Two distinct guardrail layers, and it matters that they are distinct:

* **Vertex AI safety filters** (this module) run *inside the model serving stack*,
  before a token ever reaches ContentForge. They cover the harm categories Google
  classifies natively - hate speech, harassment, sexually explicit and dangerous
  content.
* **:class:`~content_forge.plugins.guardrail_plugin.GuardrailPlugin`** runs in our
  process and covers the things a generic harm classifier cannot know about:
  prompt injection, tool authorisation, brand policy, credential leakage.

Neither subsumes the other. The platform filter cannot know that only
``publisher_agent`` may publish; our plugin cannot re-rank logits.

Threshold rationale
-------------------
Thresholds are chosen per category rather than set uniformly, because a uniform
setting is wrong in one direction or the other:

* ``HATE_SPEECH``, ``HARASSMENT``, ``SEXUALLY_EXPLICIT`` -> ``BLOCK_MEDIUM_AND_ABOVE``.
  A brand blog has no legitimate need to approach these, so a tighter threshold
  costs nothing real.
* ``DANGEROUS_CONTENT`` -> ``BLOCK_ONLY_HIGH``. This is the deliberate exception.
  The blog covers security topics - prompt injection, CVEs, vulnerability
  analysis - and a medium threshold produces false positives on exactly the
  technical writing the pipeline exists to produce. Blocking a legitimate
  security post is a real cost; the plugin layer and the human approval gate
  remain in place beneath this.

Sampling is likewise per role: structural output wants determinism, prose wants
some variation.
"""

from __future__ import annotations

from typing import Final

from google.genai import types

from content_forge.models import ModelRole

#: Harm-category thresholds. See the module docstring for why these differ.
SAFETY_SETTINGS: Final[list[types.SafetySetting]] = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    ),
    types.SafetySetting(
        # Deliberately looser: the blog writes about security. See module docstring.
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

#: Per-role sampling. ``(temperature, top_p, max_output_tokens)``.
_SAMPLING_BY_ROLE: Final[dict[str, tuple[float, float, int]]] = {
    # Structured planning: near-deterministic so the same brief yields a stable
    # plan and the golden-dataset evaluation is reproducible.
    "planner": (0.2, 0.85, 4096),
    # Long-form prose: enough variation to avoid formulaic phrasing, but not so
    # much that the critic spends revision rounds fixing drift.
    "editorial": (0.7, 0.95, 8192),
    # Retrieval summarisation: factual, so low.
    "research": (0.3, 0.9, 4096),
    # Mechanical extraction: deterministic.
    "extraction": (0.0, 1.0, 2048),
    # Policy screening: deterministic, or the same input could be judged
    # differently on two consecutive turns.
    "guardrail": (0.0, 1.0, 1024),
}


def build_generate_content_config(role: ModelRole) -> types.GenerateContentConfig:
    """Build the generation config for a pipeline role.

    Applies the platform safety filters plus role-appropriate sampling.

    Args:
        role: The pipeline role, one of ``planner``, ``editorial``, ``research``,
            ``extraction`` or ``guardrail``.

    Returns:
        A :class:`~google.genai.types.GenerateContentConfig` carrying
        :data:`SAFETY_SETTINGS` and the role's sampling parameters.

    Raises:
        KeyError: If ``role`` is not a known role. The message lists valid roles.
    """
    if role not in _SAMPLING_BY_ROLE:
        raise KeyError(
            f"Unknown role {role!r} for generation config. "
            f"Valid roles: {sorted(_SAMPLING_BY_ROLE)}."
        )
    temperature, top_p, max_output_tokens = _SAMPLING_BY_ROLE[role]
    return types.GenerateContentConfig(
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        safety_settings=list(SAFETY_SETTINGS),
    )

import os
import warnings

import google.genai as genai

_ENTERPRISE_ENV_VAR = "GOOGLE_GENAI_USE_ENTERPRISE"
_VERTEXAI_ENV_VAR = "GOOGLE_GENAI_USE_VERTEXAI"
_PROJECT_ENV_VAR = "GOOGLE_CLOUD_PROJECT"
_LOCATION_ENV_VAR = "GOOGLE_CLOUD_LOCATION"
_API_KEY_ENV_VAR = "GOOGLE_API_KEY"


def _parse_flag(name: str) -> bool | None:
    """Parses one mode env var, matching google-genai's own parsing exactly
    (installed package, google/genai/_api_client.py:620-621,624-625:
    `env_..._str.lower() in ['true', '1']`). Returns None when the var is
    unset at all - as opposed to False - because that distinction is what
    the precedence rule in _vertexai_enabled() below depends on."""
    raw = os.environ.get(name)
    return None if raw is None else raw.lower() in ("true", "1")


def _vertexai_enabled() -> bool:
    """Whether Vertex mode is selected, matching google-genai's own
    precedence exactly (installed package, google/genai/_api_client.py:
    627-641): both GOOGLE_GENAI_USE_ENTERPRISE and GOOGLE_GENAI_USE_VERTEXAI
    are read; if both are set to conflicting values, GOOGLE_GENAI_USE_ENTERPRISE
    wins and a warning is emitted; otherwise ENTERPRISE is used if set, else
    VERTEXAI if set, else Vertex mode is off.

    This duplicates the SDK's own env parsing instead of just passing
    vertexai=None and letting the SDK decide, because the SDK only consults
    the env vars when the `vertexai` kwarg is not explicitly passed
    (_api_client.py:615, `if self.vertexai is None:`). Our API-key branch
    below passes api_key= explicitly, which requires us to also pass an
    explicit vertexai= value - so we must resolve the same precedence
    ourselves to stay in agreement with what the SDK would have chosen.
    """
    enterprise = _parse_flag(_ENTERPRISE_ENV_VAR)
    vertexai = _parse_flag(_VERTEXAI_ENV_VAR)

    if enterprise is not None and vertexai is not None and enterprise != vertexai:
        warnings.warn(
            f"Both {_ENTERPRISE_ENV_VAR} and {_VERTEXAI_ENV_VAR} are set with "
            f"conflicting values. The value of {_ENTERPRISE_ENV_VAR} will be used.",
            stacklevel=2,
        )

    if enterprise is not None:
        return enterprise
    if vertexai is not None:
        return vertexai
    return False


def get_genai_client() -> genai.Client:
    """Returns a configured google-genai Client for tools to share.

    Read at call time (not import time), so tests and deployments never need
    to reimport this module to pick up an env var change.

    GOOGLE_GENAI_USE_VERTEXAI=true (or 1) constructs a Vertex-mode client
    from GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION, with no api_key -
    see google/genai/client.py's Client.__init__ (installed package) for the
    exact constructor arguments this mirrors: `vertexai`, `project`,
    `location`. Otherwise, returns the existing API-key client, unchanged.
    """
    if _vertexai_enabled():
        return genai.Client(
            vertexai=True,
            project=os.environ[_PROJECT_ENV_VAR],
            location=os.environ[_LOCATION_ENV_VAR],
        )
    return genai.Client(api_key=os.environ[_API_KEY_ENV_VAR])


def has_valid_genai_config() -> bool:
    """True if either a complete Vertex config or an API-key config is
    present. Used by main.py's startup check, which must accept either."""
    if _vertexai_enabled():
        return bool(os.environ.get(_PROJECT_ENV_VAR)) and bool(os.environ.get(_LOCATION_ENV_VAR))
    return bool(os.environ.get(_API_KEY_ENV_VAR))

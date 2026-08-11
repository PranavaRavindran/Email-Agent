import os

import google.genai as genai

_VERTEXAI_ENV_VAR = "GOOGLE_GENAI_USE_VERTEXAI"
_PROJECT_ENV_VAR = "GOOGLE_CLOUD_PROJECT"
_LOCATION_ENV_VAR = "GOOGLE_CLOUD_LOCATION"
_API_KEY_ENV_VAR = "GOOGLE_API_KEY"


def _vertexai_enabled() -> bool:
    """Whether Vertex mode is selected, matching google-genai's own
    GOOGLE_GENAI_USE_VERTEXAI parsing exactly (see the installed package,
    google/genai/_api_client.py: `env_vertexai_str.lower() in ['true', '1']`),
    so this factory and the underlying SDK never disagree about which mode
    is selected."""
    return os.environ.get(_VERTEXAI_ENV_VAR, "").lower() in ("true", "1")


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

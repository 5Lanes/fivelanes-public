"""Feature access layer: OS (base) vs premium capabilities.

The public repo ships with all OS-tier features enabled. Premium-tier features stay
disabled until a premium package calls ``unlock_premium()`` or ``FIVELANES_PREMIUM=1``
is set (useful for local development without the paid add-on).

Premium extensions live in ``fivelanes-premium/`` (see ``premium_root()``). That package
should import ``unlock_premium`` on load, or expose ``fivelanes_premium.bootstrap()``.
"""

from __future__ import annotations

import os
import sys
from enum import Enum

from utils.runtime_paths import premium_root


class FeatureTier(str, Enum):
    OS = "os"
    PREMIUM = "premium"


class FeatureUnavailableError(Exception):
    """Raised when code or an API handler requires a disabled feature."""

    def __init__(self, feature_id: str) -> None:
        self.feature_id = feature_id
        super().__init__(f"Feature not available: {feature_id}")


FEATURE_REGISTRY: dict[str, FeatureTier] = {
    # OS — always available in the public repo
    "dashboard": FeatureTier.OS,
    "threads": FeatureTier.OS,
    "meetings": FeatureTier.OS,
    "plans": FeatureTier.OS,
    "lanes": FeatureTier.OS,
    "pipeline": FeatureTier.OS,
    "meeting_prep": FeatureTier.OS,
    "email_reply": FeatureTier.OS,
    # Premium — enabled when premium is unlocked
    "texts": FeatureTier.PREMIUM,
    "slack": FeatureTier.PREMIUM,
    "linkedin": FeatureTier.PREMIUM,
    "availability": FeatureTier.PREMIUM,
}

_OS_FEATURES = frozenset(
    feature_id for feature_id, tier in FEATURE_REGISTRY.items() if tier == FeatureTier.OS
)
_PREMIUM_FEATURES = frozenset(
    feature_id for feature_id, tier in FEATURE_REGISTRY.items() if tier == FeatureTier.PREMIUM
)

_ROUTE_FEATURES: dict[tuple[str, str], str] = {
    ("GET", "/out/availability_calendar_latest.json"): "availability",
    ("GET", "/api/texts/catalog"): "texts",
    ("POST", "/api/texts/track"): "texts",
    ("POST", "/api/texts/summarize"): "texts",
    ("GET", "/api/slack/catalog"): "slack",
    ("POST", "/api/slack/pull"): "slack",
    ("POST", "/api/slack/track"): "slack",
    ("POST", "/api/slack/summarize"): "slack",
    ("GET", "/api/linkedin/catalog"): "linkedin",
    ("POST", "/api/linkedin/track"): "linkedin",
    ("POST", "/api/linkedin/summarize"): "linkedin",
}

_premium_unlocked = False
_env_feature_overrides: dict[str, bool] | None = None


def unlock_premium() -> None:
    """Mark premium features as available (called by the premium package on import)."""
    global _premium_unlocked
    _premium_unlocked = True


def premium_unlocked() -> bool:
    return _premium_unlocked


def _truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_env_feature_overrides() -> dict[str, bool]:
    overrides: dict[str, bool] = {}
    enable_raw = (os.getenv("FIVELANES_FEATURES") or "").strip()
    if enable_raw:
        for part in enable_raw.split(","):
            feature_id = part.strip()
            if feature_id:
                overrides[feature_id] = True
    disable_raw = (os.getenv("FIVELANES_FEATURES_DISABLE") or "").strip()
    if disable_raw:
        for part in disable_raw.split(","):
            feature_id = part.strip()
            if feature_id:
                overrides[feature_id] = False
    return overrides


def _feature_overrides() -> dict[str, bool]:
    global _env_feature_overrides
    if _env_feature_overrides is None:
        _env_feature_overrides = _parse_env_feature_overrides()
    return _env_feature_overrides


def is_enabled(feature_id: str) -> bool:
    if feature_id not in FEATURE_REGISTRY:
        return False
    overrides = _feature_overrides()
    if feature_id in overrides:
        return overrides[feature_id]
    if feature_id in _OS_FEATURES:
        return True
    if feature_id in _PREMIUM_FEATURES:
        return _premium_unlocked or _truthy_env("FIVELANES_PREMIUM")
    return False


def enabled_features() -> list[str]:
    return sorted(feature_id for feature_id in FEATURE_REGISTRY if is_enabled(feature_id))


def require_feature(feature_id: str) -> None:
    if not is_enabled(feature_id):
        raise FeatureUnavailableError(feature_id)


def required_feature_for_route(method: str, path: str) -> str | None:
    """Return the feature id gating an API route, or None if unrestricted."""
    key = (method.upper(), path)
    return _ROUTE_FEATURES.get(key)


def features_config_payload() -> dict:
    return {
        "enabled_features": enabled_features(),
        "premium_unlocked": premium_unlocked() or _truthy_env("FIVELANES_PREMIUM"),
    }


def _try_load_premium_package() -> None:
    if _truthy_env("FIVELANES_PREMIUM"):
        unlock_premium()
        return
    root = premium_root()
    if not root.is_dir():
        return
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        from fivelanes_premium import bootstrap as premium_bootstrap

        premium_bootstrap()
        return
    except ImportError:
        pass
    try:
        import premium_bootstrap

        apply = getattr(premium_bootstrap, "apply", None)
        if callable(apply):
            apply()
    except ImportError:
        pass


_try_load_premium_package()

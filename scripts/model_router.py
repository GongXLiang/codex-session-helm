"""Prepare model/effort overrides from assessed tasks and observed host capabilities.

No model is invoked and no setting is changed. The caller assesses the task,
supplies a current catalog, and separately verifies the eventual runtime.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import io
import json
from pathlib import Path
import sys
from typing import TypedDict

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ROLES = ("focused", "general", "deep", "visual")


class RoutingError(ValueError):
    """The requested selection cannot be prepared safely from this snapshot."""


@dataclass(frozen=True)
class Model:
    id: str
    roles: tuple[str, ...]
    efforts: tuple[str, ...]
    baseline_effort: str


@dataclass(frozen=True)
class Assessment:
    ambiguity: int
    coupling: int
    consequence: int
    verification: int
    kind: str = "general"


@dataclass(frozen=True)
class Request:
    assessment: Assessment
    models: tuple[Model, ...]
    default_model: str | None
    model_policy: str
    thinking_allowed: bool
    mode: str = "auto"
    user_model: str | None = None
    user_effort: str | None = None
    allowed_model_ids: tuple[str, ...] | None = None
    max_auto_effort: str = "high"


class Choice(TypedDict):
    model: str | None
    thinking: str | None


@dataclass(frozen=True)
class Decision:
    level: str
    role: str
    recommended: Choice
    target: Choice
    overrides: dict[str, str]
    notes: tuple[str, ...]
    runtime_verified: bool = False


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RoutingError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingError(f"{name} must be a nonempty string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RoutingError(f"{name} must be an array")
    return tuple(_text(item, name) for item in value)


def _known_fields(raw: dict[str, object], allowed: set[str], name: str) -> None:
    unknown = raw.keys() - allowed
    if unknown:
        raise RoutingError(f"unknown {name} fields: {', '.join(sorted(unknown))}")


def parse_request(value: object) -> Request:
    """Parse the portable input; capability values must come from the host."""
    raw = _object(value, "request")
    _known_fields(raw, {"schema_version", "assessment", "models", "default_model", "model_policy",
                        "thinking_allowed", "mode", "user_model", "user_effort", "allowed_model_ids",
                        "max_auto_effort"}, "request")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise RoutingError("schema_version must be 1")
    task = _object(raw.get("assessment"), "assessment")
    _known_fields(task, {"ambiguity", "coupling", "consequence", "verification", "kind"}, "assessment")
    scores: list[int] = []
    for field in ("ambiguity", "coupling", "consequence", "verification"):
        score = task.get(field)
        if type(score) is not int or score not in (0, 1, 2):
            raise RoutingError(f"assessment.{field} must be 0, 1 or 2")
        scores.append(score)
    catalog = raw.get("models")
    if not isinstance(catalog, list):
        raise RoutingError("models must be an array")
    models: list[Model] = []
    for item in catalog:
        model = _object(item, "model")
        _known_fields(model, {"id", "roles", "efforts", "baseline_effort"}, "model")
        models.append(Model(_text(model.get("id"), "model.id"),
                            _strings(model.get("roles"), "model.roles"),
                            _strings(model.get("efforts"), "model.efforts"),
                            _text(model.get("baseline_effort"), "model.baseline_effort")))
    thinking = raw.get("thinking_allowed")
    if type(thinking) is not bool:
        raise RoutingError("thinking_allowed must be a boolean")
    allowed = raw.get("allowed_model_ids")
    return Request(Assessment(*scores, _text(task.get("kind", "general"), "assessment.kind")),
                   tuple(models), _optional_text(raw.get("default_model"), "default_model"),
                   _text(raw.get("model_policy"), "model_policy"), thinking,
                   _text(raw.get("mode", "auto"), "mode"),
                   _optional_text(raw.get("user_model"), "user_model"),
                   _optional_text(raw.get("user_effort"), "user_effort"),
                   None if allowed is None else _strings(allowed, "allowed_model_ids"),
                   _text(raw.get("max_auto_effort", "high"), "max_auto_effort"))


def route(request: Request) -> Decision:
    """Select a profile, honor explicit settings, then gate host overrides."""
    task = request.assessment
    scores = (task.ambiguity, task.coupling, task.consequence, task.verification)
    if any(type(score) is not int or score not in (0, 1, 2) for score in scores):
        raise RoutingError("assessment scores must be 0, 1 or 2")
    if task.kind not in ("general", "visual") or request.mode not in ("auto", "inherit"):
        raise RoutingError("unsupported kind or mode")
    if request.model_policy not in ("allowed", "explicit_only", "unsupported"):
        raise RoutingError("unknown model override policy")
    if request.max_auto_effort not in ("low", "medium", "high", "xhigh"):
        raise RoutingError("max_auto_effort must be low, medium, high or xhigh")
    if request.user_effort is not None and request.user_effort not in EFFORTS:
        raise RoutingError("unknown user effort")
    if request.mode == "inherit" and request.user_model is not None:
        raise RoutingError("inherit mode conflicts with an explicit model")
    catalog = {model.id: model for model in request.models}
    if len(catalog) != len(request.models):
        raise RoutingError("duplicate model id")
    for model in request.models:
        if not model.id or not model.roles or any(role not in ROLES for role in model.roles):
            raise RoutingError("model id and known roles are required")
        if not model.efforts or any(effort not in EFFORTS for effort in model.efforts):
            raise RoutingError("model efforts must be known and nonempty")
        if model.baseline_effort not in model.efforts:
            raise RoutingError("baseline effort must be supported by its model")
    if request.default_model is not None and request.default_model not in catalog:
        raise RoutingError("default model is missing from the current catalog")
    if request.allowed_model_ids is not None and (
        not request.allowed_model_ids or any(item not in catalog for item in request.allowed_model_ids)
    ):
        raise RoutingError("allowed model ids must be a nonempty subset of the catalog")
    pool = [model for model in request.models
            if request.allowed_model_ids is None or model.id in request.allowed_model_ids]
    default = catalog.get(request.default_model) if request.default_model is not None else None
    level = "complex" if task.consequence == 2 or task.verification == 2 or sum(scores) >= 5 else "simple" if sum(scores) == 0 else "standard"
    role = "deep" if level == "complex" else "visual" if task.kind == "visual" else "focused" if level == "simple" else "general"
    notes = [f"{level}: ambiguity={task.ambiguity}, coupling={task.coupling}, consequence={task.consequence}, verification={task.verification}"]
    if request.user_model is not None:
        chosen = next((model for model in pool if model.id == request.user_model), None)
        if chosen is None:
            raise RoutingError("explicit model is unavailable or outside the allowed set")
        if request.model_policy == "unsupported":
            raise RoutingError("host cannot apply the explicit model")
    elif request.mode == "inherit":
        chosen = default
    else:
        eligible = [model for model in pool
                    if request.user_effort is None or request.user_effort in model.efforts]
        chosen = next((model for model in eligible if role in model.roles), None)
        if chosen is None:
            chosen = default if default in eligible else None
            notes.append("Preferred model role unavailable; keep an allowed known default or omit overrides.")

    def effort_for(model: Model | None) -> str | None:
        if model is None:
            if request.user_effort is not None:
                raise RoutingError("cannot validate explicit effort without a known target model")
            return None
        if request.user_effort is not None:
            if request.user_effort not in model.efforts:
                raise RoutingError("explicit effort is unsupported by the target model")
            return request.user_effort
        if request.mode == "inherit":
            return None
        desired = model.baseline_effort if level == "simple" else "high" if level == "complex" else "medium"
        ceiling = min(EFFORTS.index(desired), EFFORTS.index(request.max_auto_effort))
        valid = [effort for effort in model.efforts if EFFORTS.index(effort) <= ceiling]
        return max(valid, key=EFFORTS.index) if valid else None

    recommended: Choice = {"model": chosen.id if chosen else None, "thinking": effort_for(chosen)}
    override_model = chosen is not None and request.mode != "inherit" and (
        request.model_policy == "allowed" or request.user_model is not None)
    target = chosen if override_model else default
    if request.allowed_model_ids is not None and (target is None or target.id not in request.allowed_model_ids):
        raise RoutingError("host default cannot be shown to satisfy the allowed-model constraint")
    if not override_model and request.mode == "auto" and chosen != default:
        notes.append("Model recommendation cannot be applied here; inherited target differs or is unknown.")
    if request.user_effort is not None and not request.thinking_allowed:
        raise RoutingError("host cannot apply the explicit effort")
    thinking = effort_for(target) if request.thinking_allowed else None
    if override_model and request.mode == "auto" and request.user_model is None and thinking is None:
        raise RoutingError("automatic model change requires a supported effort within the limit and host effort control")
    if target is None:
        notes.append("Target model unknown: omit model-specific effort overrides.")
    if thinking is None and request.mode == "auto":
        notes.append("Effort inherited: no supported override within the automatic limit, or host lacks control.")
    overrides: dict[str, str] = {}
    if override_model and chosen is not None:
        overrides["model"] = chosen.id
    if thinking is not None:
        overrides["thinking"] = thinking
    return Decision(level, role, recommended,
                    {"model": target.id if target else None, "thinking": thinking},
                    overrides, tuple(notes))


def main(argv: list[str] | None = None) -> int:
    """Print a decision without mutating the request or host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    try:
        request = parse_request(json.loads(args.request.read_text(encoding="utf-8")))
        print(json.dumps(asdict(route(request)), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"SessionHelm model routing: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

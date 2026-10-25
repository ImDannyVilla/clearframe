"""The reviewer model: Qwen3.6 behind an OpenAI-compatible server, or a fake for laptop tests.

Standard library only. The real client reads `VISION_URL`, `VISION_API_KEY` and
`VISION_MODEL` from the environment (the worker gets them from `vision.env`, as
the GB10 cheat sheet says). Nothing here calls anything off the box: the URL is
the local model server.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Sampling:
    """How one request is sampled. Votes need randomness: thinking on, or temperature > 0 and top_k > 1."""

    thinking: bool
    temperature: float
    top_p: float
    top_k: int
    max_tokens: int


@dataclass(frozen=True)
class Request:
    """One question about some frames. `frames` are take frame numbers, in the order the model sees them."""

    check: str
    frames: tuple[int, ...]
    content: tuple[dict, ...]


@dataclass(frozen=True)
class Reply:
    text: str
    seconds: float


class ModelError(RuntimeError):
    """The model server failed or sent something that isn't a chat reply."""


class Model(Protocol):
    name: str

    def ask(self, request: Request, sampling: Sampling) -> Reply: ...


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines, as in `vision.env`; blank lines and `#` comments skipped."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class VisionClient:
    """Chat completions on the local model server, with frames sent as base64."""

    def __init__(self, url: str, model: str, api_key: str = "", timeout: float = 600.0):
        self.url = url.rstrip("/") + "/chat/completions"
        self.name = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "VisionClient":
        env = dict(os.environ)
        if env_file is not None:
            env = {**read_env_file(env_file), **env}
        missing = [key for key in ("VISION_URL", "VISION_MODEL") if not env.get(key)]
        if missing:
            raise ModelError(f"set {' and '.join(missing)} (see vision.env in the GB10 cheat sheet)")
        return cls(env["VISION_URL"], env["VISION_MODEL"], env.get("VISION_API_KEY", ""))

    def payload(self, request: Request, sampling: Sampling) -> dict:
        return {
            "model": self.name,
            "messages": [{"role": "user", "content": list(request.content)}],
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "max_tokens": sampling.max_tokens,
            "chat_template_kwargs": {"enable_thinking": sampling.thinking},
        }

    def ask(self, request: Request, sampling: Sampling) -> Reply:
        body = json.dumps(self.payload(request, sampling)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(self.url, data=body, headers=headers), timeout=self.timeout
            ) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise ModelError(f"model server answered {error.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ModelError(f"model server unreachable or bad reply: {error}") from None
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("model server reply has no choices[0].message") from None
        return Reply(text=text, seconds=round(time.monotonic() - started, 3))


class FakeModel:
    """Answers like the model would, from a sample file, so laptop tests need no model.

    The file maps a check to the frame ranges where the flaw shows and the note to
    give, as in `vision/samples/answers-4-2.json`. Every other frame gets `yes`.
    """

    def __init__(self, answers: dict, name: str = "fake"):
        self.name = name
        self.answers = answers

    @classmethod
    def from_file(cls, path: Path) -> "FakeModel":
        return cls(json.loads(path.read_text(encoding="utf-8")), name=f"fake:{path.name}")

    def ask(self, request: Request, sampling: Sampling) -> Reply:
        answer = self.answers.get(request.check, {})
        ranges = answer.get("flaw_frames", [])
        lines = []
        for number, frame in enumerate(request.frames, start=1):
            if any(start <= frame <= end for start, end in ranges):
                lines.append(f"{number}: no - {answer.get('note', 'wrong')}")
            else:
                lines.append(f"{number}: yes")
        return Reply(text="\n".join(lines), seconds=0.0)

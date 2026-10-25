"""Posting a finished card. Standard library only.

The card text is built by `agent.card` from the rubric's templates; this only
delivers it. Which poster runs is set by the environment, never in git:

- `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` set: Slack's `chat.postMessage`, from
  inside the sandbox, whose policy allows only Slack and the local model.
- `CLEARFRAME_SLACK=print`: print the card and count it as posted, for dry runs.
- Neither: nothing is posted and the card stays unposted, so it goes out on a
  later poll once Slack is configured.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"

# A poster takes the card text and returns the message `ts`, or None if nothing was posted.
Poster = Callable[[str], "str | None"]


class SlackError(RuntimeError):
    """Slack refused the post or couldn't be reached. The message carries no card text."""


def slack_poster(token: str, channel: str, timeout: float = 15.0) -> Poster:
    def post(text: str) -> str | None:
        body = json.dumps({"channel": channel, "text": text, "unfurl_links": False}).encode("utf-8")
        request = urllib.request.Request(
            SLACK_POST_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                reply = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise SlackError(f"Slack unreachable: {type(error).__name__}") from None
        if not reply.get("ok"):
            raise SlackError(f"Slack refused the post: {reply.get('error', 'unknown error')}")
        return str(reply.get("ts"))

    return post


def print_poster(text: str) -> str | None:
    print("----- card (CLEARFRAME_SLACK=print) -----\n" + text + "\n-----------------------------------------",
          flush=True)
    return "printed"


def unconfigured_poster(text: str) -> str | None:
    return None


def poster_from_env() -> Poster:
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL")
    if token and channel:
        return slack_poster(token, channel)
    if os.environ.get("CLEARFRAME_SLACK") == "print":
        return print_poster
    return unconfigured_poster

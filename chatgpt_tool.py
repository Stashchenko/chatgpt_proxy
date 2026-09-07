#!/usr/bin/env python3
"""
Hermes Tool: ChatGPT Subagent
Interacts with the local persistent ChatGPT proxy server.
"""

import json
import sys
import urllib.error
import urllib.request

PROXY_URL = "http://localhost:8000/api/chat"
RESET_URL = "http://localhost:8000/api/reset"


def ask_chatgpt(prompt: str, timeout: int = 120) -> str:
    """Sends a prompt to the ChatGPT subagent proxy and returns the response."""
    payload = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        PROXY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                return data.get("response", "No response content returned.")
            return f"Error: Received HTTP status {response.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP Error ({e.code}): {e.reason}"
    except urllib.error.URLError as e:
        return f"Connection Error: Ensure ChatGPT server is running on http://localhost:8000 ({e.reason})"
    except Exception as e:
        return f"Unexpected Error: {str(e)}"


def reset_chatgpt(timeout: int = 30) -> str:
    """Resets the active ChatGPT subagent conversation session."""
    req = urllib.request.Request(RESET_URL, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return "ChatGPT subagent session reset successfully."
            return f"Failed to reset: HTTP {response.status}"
    except Exception as e:
        return f"Reset Error: {str(e)}"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 chatgpt_tool.py <prompt> [--reset]")
        sys.exit(1)

    if sys.argv[1] == "--reset":
        print(reset_chatgpt())
    else:
        prompt_text = " ".join(sys.argv[1:])
        print(ask_chatgpt(prompt_text))

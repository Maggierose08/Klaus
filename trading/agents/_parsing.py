import re

# Claude sometimes wraps a JSON response in a markdown code fence (```json
# ... ```) even when told to respond with JSON only - this strips it before
# json.loads() so a fenced response parses the same as a bare one.
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def strip_json_fence(text: str) -> str:
    """Returns the JSON body inside a markdown code fence, if present;
    otherwise returns the text unchanged (stripped) so a genuinely malformed
    response still fails json.loads() with a clear error."""
    match = _FENCE_PATTERN.search(text)
    return match.group(1).strip() if match else text.strip()

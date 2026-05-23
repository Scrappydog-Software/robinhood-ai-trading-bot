from anthropic import Anthropic
import re
import json
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL_NAME


# Initialize Anthropic client
client = Anthropic(api_key=ANTHROPIC_API_KEY)


# Make AI request to Anthropic API.
# Returns the response text as a plain string. This is a deliberate simplification
# from the prior OpenAI integration, which returned a nested object the caller had
# to drill into via `.choices[0].message.content`. Anthropic's response shape is
# `resp.content[0].text` (a list of content blocks); rather than mirror that
# indirection, we collapse the response to a string at the boundary so callers can
# treat the AI output as the value it actually is.
def make_ai_request(prompt):
    ai_resp = client.messages.create(
        model=ANTHROPIC_MODEL_NAME,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate text from all content blocks (typically just one for plain prompts)
    return "".join(block.text for block in ai_resp.content if getattr(block, "type", None) == "text")


# Parse AI response. Accepts the response text as a string.
def parse_ai_response(ai_response):
    try:
        ai_content = re.sub(r'```json|```', '', ai_response.strip())
        decisions = json.loads(ai_content)
    except json.JSONDecodeError:
        raise Exception("Invalid JSON response from Anthropic: " + ai_response.strip())
    return decisions

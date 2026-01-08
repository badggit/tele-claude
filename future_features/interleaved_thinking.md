# Interleaved Thinking for Claude Agent SDK

## Summary
Enable interleaved thinking in the Claude Agent SDK to allow Claude to reason between tool calls rather than planning everything upfront.

## Background
From [@steipete's tweet](https://x.com/steipete/status/2000560023165125076) quoting [@joshtriescoding](https://x.com/joshtriescoding):

> **interleaved thinking lets agents think between each step**
>
> before: agent thinks once, then runs all actions
> now: agent thinks → acts → thinks → acts

This is a Claude 4 feature that significantly improves agent behavior by allowing reasoning after receiving tool results.

## Current Behavior
- Agent plans all actions upfront
- Executes tools in sequence without intermediate reasoning
- May miss opportunities to adjust strategy based on tool outputs

## Proposed Behavior
- Agent thinks before first action
- After each tool result, agent can reason about what it learned
- Decisions are made incrementally based on actual results
- More adaptive and context-aware behavior

## Implementation

### Option 1: ClaudeAgentOptions Parameter
```python
options = ClaudeAgentOptions(
    # ... existing options ...
    thinking={"type": "enabled", "budget_tokens": 10000},
    # or
    interleaved_thinking=True,
)
```

### Option 2: Beta Header
May require `anthropic-beta` header to enable:
```python
# Need to check Claude Agent SDK docs for exact syntax
```

## Research Needed
1. Check Claude Agent SDK documentation for exact parameter name
2. Determine if this is a beta feature requiring special headers
3. Test impact on response latency and token usage
4. Verify compatibility with our streaming response handling

## Benefits
- More intelligent tool usage decisions
- Better error recovery (can reason about failures)
- More natural multi-step workflows
- Improved accuracy on complex tasks

## Considerations
- May increase token usage (thinking tokens)
- Could add latency between tool calls
- Need to handle thinking content in our response streaming

## References
- [Building agents with Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)
- [Using Strands Agents with Claude 4 Interleaved Thinking](https://aws.amazon.com/blogs/opensource/using-strands-agents-with-claude-4-interleaved-thinking/)
- [Claude Agent SDK overview](https://platform.claude.com/docs/en/agent-sdk/overview)

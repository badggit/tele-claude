# Smart Compact Handling

## Problem Statement

When Claude's context window fills up, it performs "compaction" - summarizing older conversation parts to free space. This causes:

1. **Loss of critical details**: File paths, function names, error messages, architectural decisions
2. **Generic summaries**: Default compaction produces ~3000-5000 tokens of vague context
3. **Session continuity breaks**: Users have to re-explain what they were working on

### Current Implementation Issues

Our `PreCompact` hook (`session.py:277-306`) notifies users when compaction happens, but:
- **Context threshold detection is broken** - never triggers the low-context warning
- We don't preserve any state before compaction
- No recovery mechanism after compaction

The context calculation in `calculate_context_remaining()` (`session.py:319-359`) excludes `cache_read_input_tokens` due to SDK quirks, but this may not be accurate.

## Research: What Others Are Doing

### 1. Continuous-Claude - "Clear, Don't Compact"
**Source**: https://github.com/parcadei/Continuous-Claude

Philosophy: Fresh context + curated state beats degraded compacted context.

**Approach**:
- Save state to persistent ledger files before compaction
- Wipe context clean
- Resume with fresh context + saved state injected

**Key Components**:
- `HANDOFF.md` documents for session transfer
- SQLite indexing for historical session search
- Sub-agents with isolated context windows

### 2. Context Forge - Two-Hook System
**Source**: https://github.com/webdevtodayjason/claude-hooks

**Approach**:
- `precompact-context-refresh.py`: Detect project structure, prepare recovery instructions
- `stop-context-refresh.py`: After compaction, force re-read of critical files

**Key Innovation**: Bookend the compaction with preparation and restoration phases.

### 3. External DB Extraction (Advanced)
**Source**: https://github.com/anthropics/claude-code/issues/13170

**Approach**:
- Use secondary model (Gemini) to extract strategic context (WHY/WHAT/HOW/CONTINUE_BY)
- Persist to PostgreSQL
- Inject ~636 tokens of targeted context vs 3000+ generic tokens

**Problem**: Can't suppress default summary, so custom context is additive (wasteful).

### 4. Custom Summarization Instructions (Requested Feature)
**Source**: https://github.com/anthropics/claude-code/issues/14160

Users want `autoCompact.customInstructions` in settings:
```json
{
  "autoCompact": {
    "customInstructions": "Preserve all file paths, function names, error messages, debugging steps, and architectural decisions. Do not abstract or generalize."
  }
}
```

**Status**: Requested but not implemented by Anthropic.

## SDK Limitations

The `PreCompact` hook can only inject `additionalContext` which is **additive** to the default summary. There's no way to:
- Suppress or replace the default compact summary
- Set `skipDefaultSummary: true`
- Control `summaryMaxTokens`

## Proposed Implementation Options

### Option A: Handoff Document Pattern (Recommended)

**When**: PreCompact hook fires OR context drops below threshold OR session ends

**Do**:
1. Generate `HANDOFF-<session_id>.md` containing:
   - Current task/goal
   - Files touched this session
   - Key decisions made
   - Pending work items
   - Recent errors/blockers
2. Save to `.bot-logs/handoffs/`

**On Session Start**:
1. Check for existing handoff for this thread
2. If exists, inject into system prompt via `additionalContext`
3. Claude picks up where it left off

**Pros**: Minimal code, works within SDK constraints
**Cons**: Doesn't prevent default compaction, just supplements it

### Option B: Proactive Session Reset (Nuclear Option)

**When**: Context drops below 20% (if we can detect it properly)

**Do**:
1. Generate handoff document
2. Stop current session
3. Start fresh session with handoff injected
4. Notify user: "Context was running low, started fresh session with state preserved"

**Pros**: Avoids degraded compaction entirely
**Cons**: Disruptive, may lose in-flight operations

### Option C: Transcript Backup + Selective Replay

**When**: PreCompact fires

**Do**:
1. Save full transcript to file
2. Extract "important" messages (tool calls, user requests, key decisions)
3. After compaction, inject extracted context

**Pros**: Preserves more detail than default summary
**Cons**: Complex to implement, token-heavy

## Implementation Priority

1. **Fix context threshold detection** - Debug why `calculate_context_remaining()` never triggers warnings
2. **Implement Option A** - Handoff document generation in PreCompact hook
3. **Add SessionStart injection** - Load handoff on new session/after compaction
4. **Consider Option B** - If users complain about compaction quality

## Code Locations

- PreCompact hook: `session.py:277-306`
- Context calculation: `session.py:319-359`
- Session logging: `logger.py`
- Hook registration: `session.py:554-557`

## References

- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks)
- [claude-code-hooks-mastery](https://github.com/disler/claude-code-hooks-mastery)
- [Claude Hooks Manager](https://github.com/webdevtodayjason/claude-hooks)
- [Context Forge](https://github.com/webdevtodayjason/context-forge)
- [Feature Request: Skip Default Summary](https://github.com/anthropics/claude-code/issues/13170)
- [Feature Request: Custom Compact Instructions](https://github.com/anthropics/claude-code/issues/14160)
- [Continuous-Claude](https://github.com/parcadei/Continuous-Claude)

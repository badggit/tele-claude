# Improved Memory — Feature Plan

## Motivation

Currently each Claude session lives entirely in the SDK's context window. Long conversations hit token limits, lose coherence, and burn costs. This document captures architectural patterns for adding structured memory to the bot, based on research into production memory architectures for agentic AI systems.

Source: [How to Design Efficient Memory Architectures for Agentic AI Systems](https://pub.towardsai.net/how-to-design-efficient-memory-architectures-for-agentic-ai-systems-81ed456bb74f) (Suchitra Malimbada, Nov 2025)

---

## Why Flat Vector Storage Fails at Scale

Four failure modes emerge when you dump everything into a vector DB and retrieve by cosine similarity:

| Failure Mode | Description |
|---|---|
| **Context Poisoning** | Agent stores hallucinations/errors, retrieves and reinforces them in a feedback loop |
| **Context Distraction** | Top-K semantically similar results ≠ contextually relevant results; signal drowns in noise |
| **Context Clash** | Contradictory facts (e.g. old vs new address) have similar relevance scores; agent guesses wrong |
| **Work Duplication** | In multi-agent setups without shared memory, agents repeat each other's work |

---

## The Four Memory Types

Modeled after human cognition:

| Type | Stores | Persistence | Maps To |
|---|---|---|---|
| **Working** | Current conversation, recent tool outputs, active variables | Temporary (context window) | LLM context window |
| **Episodic** | Past conversation history, task outcomes, tool results | Long-term | Vector DBs with temporal indexing |
| **Semantic** | Domain facts, knowledge base, documentation | Indefinite | Knowledge graphs, vector DBs, wikis |
| **Procedural** | Learned skills, action sequences, validated workflows | Indefinite | PDDL, Pydantic schemas, code templates |

**Key insight:** Treating all memory identically is the root cause of most production failures. Different information types need different persistence, retrieval, and eviction strategies.

---

## Architecture Patterns

### 1. Hierarchical Memory (H-MEM / MemGPT)

**Problem solved:** Flat vector search doesn't scale past millions of entries.

**H-MEM** — 4 routing layers: Domain → Category → Memory Trace → Episode. Uses self-position index encoding to route queries layer-by-layer instead of exhaustive similarity search.

**MemGPT** — Borrows from OS memory management:
- Small **Core Memory** (always in context) — essential facts + identity
- Massive **External Context** (archival storage)
- Agent manages paging via function calls: `load_context()`, `update_core_memory()`, `archive_memory()`
- Claims **85-95% token reduction** vs naive context stuffing
- 10,000 tokens naive → ~1,000 tokens with hierarchical memory

**When to use:** Long-running sessions (100+ turns), multi-day user interactions.
**When to avoid:** Simple one-shot tasks.
**Pitfall:** Over-indexing — start with max 3 hierarchy layers, expand only if retrieval precision degrades.

### 2. Knowledge Graphs (GraphRAG)

**Problem solved:** Vector similarity is fuzzy. In high-stakes domains, "approximately correct" is dangerous.

**How it works:**
- Entities and relationships stored in graph DB (e.g. Neo4j)
- Queries traverse edges via Cypher
- Enables multi-hop reasoning vectors fundamentally cannot do

**Implementation approach:**
- Hybrid architecture: vector search for fast retrieval, GraphRAG for complex queries
- Orchestration layer routes based on query complexity (length, compound clauses, relationship indicators)
- **Use predefined Cypher queries, NOT LLM-generated ones** — hallucinated queries corrupt the graph

**Cost:** 1.5-2x vector-only infrastructure. Offset by reduced hallucination costs in high-stakes apps.
**Pitfall:** Graph explosion from unrestricted entity extraction. Prune aggressively, only store entities mentioned multiple times or tagged as high-importance.

### 3. Selective Forgetting

**Problem solved:** Unbounded memory = landfill. Retrieval degrades, costs balloon.

**RIF Formula** (Recency-Relevance-Frequency):

```
R_i = e^(-λ * t)                        # Recency (exponential decay)
RIF_score = α*R_i + β*E_i + γ*U_i       # Combined score (tunable weights)
```

- **Recency** — exponential decay, λ tunable per domain (fast-moving: λ=0.1, slow domains: λ=0.01)
- **Relevance** — cosine similarity to current query vector
- **Frequency/Utility** — access count or manually assigned importance score

Inspired by **Ebbinghaus Forgetting Curve**: steep initial decay, memories that survive get reinforced with lower decay rates.

**SynapticRAG** — encodes temporal information directly into vectors (semantic content + timestamp component), solving the "homogeneous recall" problem where RAG retrieves identical-but-temporally-distinct memories without distinguishing recency.

**Production results:** Aggressive forgetting reduces vector DB size **40-60% after 30 days**.
**Caveat:** Some domains legally require perfect recall — use tiered archival storage instead of hard deletes.
**Pitfall:** Premature deletion. Implement soft-delete first, monitor archived memory access patterns before enabling hard deletes.

---

## Decision Framework

| Scenario | Recommended Architecture |
|---|---|
| Simple one-shot tasks | Basic vector RAG |
| 100+ turn conversations | H-MEM or MemGPT (hierarchical) |
| Factual accuracy / explainability critical | Knowledge graphs / GraphRAG |
| Multi-agent coordination | Shared memory with CRDTs or event-sourcing |
| Latency-sensitive (<200ms) | Vector-only, post-process for contradictions |
| Budget-conscious | Start with vector RAG, add complexity at pain points |

---

## Production Tradeoffs

### Latency vs Fidelity
- Vector search: p95 < 30ms, fuzzy results
- Graph traversal: precise + explainable, but 3+ edge hops add substantial latency
- Hybrid deployments: ~30-40% queries hit graphs, 60-70% use vectors. Orchestration overhead ~10-20ms.

### Cost
- MemGPT paging: **90% token savings** vs stuffing full history
- Graph infra (Neo4j) more expensive than managed vector services (Pinecone, Weaviate)
- ROI calculation: if graph prevents even one critical error in medical/financial, it justifies the cost

### Operational Complexity
- Knowledge graph ETL: budget **20-30% engineering time** for ongoing maintenance
- Vector RAG iterates fast; graphs require upfront architectural planning
- Development velocity drops **30-50%** during initial graph implementation, recovers after learning curve

---

## Applicability to This Project

Current state: `sessions: dict[int, ClaudeSession]` — each topic/thread maps to one Claude session, memory lives entirely in the SDK context window.

Potential improvements to explore:
1. **MemGPT-style paging** — maintain core memory (user identity, preferences, key facts) across sessions, archive older context
2. **Episodic memory** — store conversation summaries per thread, retrieve relevant past interactions when user returns
3. **Selective forgetting** — implement RIF scoring to prune low-value memories from long-running sessions
4. **Cross-session semantic memory** — shared knowledge base across threads for the same user

Priority: MemGPT-style paging is the lowest-effort, highest-impact starting point for this bot.

---

## References

- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)
- [GraphRAG: Unlocking LLM discovery on narrative private data](https://arxiv.org/abs/2404.16130)
- [SynapticRAG: Temporal-aware retrieval](https://arxiv.org/abs/2405.13637)
- H-MEM: Hierarchical Memory for AI Agents

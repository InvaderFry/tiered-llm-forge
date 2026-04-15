# Free-Tier Coding Models: Groq + Gemini Comparison

**Last updated:** April 14, 2026  
**Optimized for:** Iterative feature building (2–3 loops per feature), quality-first, ~4 hours/day  
**Sources:** console.groq.com/docs/models (production section), console.groq.com/docs/rate-limits (free plan), AI Studio rate-limit screenshot, Aider & SWE-bench leaderboards

---

## What's Actually Available

**Groq production text models (4):** GPT-OSS 120B, GPT-OSS 20B, Llama 3.3 70B, Llama 3.1 8B  
**Groq production systems (2):** Compound, Compound Mini  
**Groq preview (2, may vanish):** Qwen3 32B, Llama 4 Scout 17B  
**Gemini usable models (4):** Gemini 3 Flash (20 RPD), Gemini 3.1 Flash Lite (500 RPD), Gemini 2.5 Flash (20 RPD), Gemini 2.5 Flash Lite (20 RPD)

**Gemini 2.5 Pro is 0/0 on your project — completely unavailable.**  
**Kimi K2 has been removed from Groq's models page entirely.**

---

## Model Rankings (by coding quality)

Assumptions for "features/day": each feature = 3 loop iterations × ~5K tokens = 15K tokens total. The binding constraint (TPD or RPD) determines the cap.

### Tier A — Best Quality

| # | Model | Provider | Quality | Practical | Features/Day | Bottleneck | Notes |
|---|-------|----------|---------|-----------|-------------|------------|-------|
| 1 | **GPT-OSS 120B** | Groq (Prod) | ★★★★ | ★★★½ | ~13 | TPD (200K) | Best production coding model on Groq. LCB 69%. 500 t/s. Apache 2.0. Your primary workhorse. |
| 2 | **Groq Compound** | Groq (System) | ★★★★ | ★★½ | ~83 req | RPD (250) | Uses GPT-OSS 120B + built-in web search & code execution. 8K max output limits large code gen. |
| 3 | **Gemini 3 Flash** | Gemini (Preview) | ★★★★ | ★½ | ~6 | RPD (20) | Highest quality Gemini you have. 1M context is unmatched. But only 20 RPD — save for hardest problems. |

### Tier B — Solid Performers

| # | Model | Provider | Quality | Practical | Features/Day | Bottleneck | Notes |
|---|-------|----------|---------|-----------|-------------|------------|-------|
| 4 | **Llama 3.3 70B** | Groq (Prod) | ★★★½ | ★★★ | ~6 | TPD (100K) | HumanEval 88.4%. Only 100K TPD — runs dry after ~6 features. Use as secondary. |
| 5 | **Gemini 2.5 Flash** | Gemini (Prod) | ★★★½ | ★½ | ~6 | RPD (20) | Better quality than Flash Lite but same brutal 20 RPD cap. |
| 6 | **Qwen3 32B** | Groq (Preview ⚠️) | ★★★½ | ★★★½ | ~33 | TPD (500K) | Best TPD on Groq. Thinking mode. 60 RPM. **Preview = could disappear any time.** |
| 7 | **GPT-OSS 20B** | Groq (Prod) | ★★★ | ★★★½ | ~13 | TPD (200K) | Fastest model on Groq at 1,000 t/s. Great for implementing from a clear plan. |
| 8 | **Gemini 3.1 Flash Lite** | Gemini (Preview) | ★★★ | ★★★★ | ~166 | RPD (500) | Best Gemini daily driver. 500 RPD is 25× other Flash models. 250K TPM = no per-minute throttling. |
| 9 | **Compound Mini** | Groq (System) | ★★★ | ★★½ | ~83 req | RPD (250) | Lighter variant of Compound. Same limits. |

### Tier C — Niche / Low Quality

| # | Model | Provider | Quality | Practical | Features/Day | Bottleneck | Notes |
|---|-------|----------|---------|-----------|-------------|------------|-------|
| 10 | **Gemini 2.5 Flash Lite** | Gemini (Prod) | ★★★ | ★½ | ~6 | RPD (20) | 10 RPM but 20 RPD kills it. Not worth prioritizing over 3.1 Flash Lite. |
| 11 | **Llama 4 Scout 17B** | Groq (Preview ⚠️) | ★★½ | ★★★ | ~33 | RPD (1K) | Highest TPM on Groq (30K). 8K max output. Better for reading code than writing it. |
| 12 | **Llama 3.1 8B** | Groq (Prod) | ★★ | ★★★½ | ~33 | TPD (500K) | 14,400 RPD = virtually unlimited requests. Low quality but fine for trivial tasks. |

---

## Rate Limits — Complete Reference

### Groq Production Models (Free Plan)

| Model | RPM | RPD | TPM | TPD | Speed | Context | Max Output | Binding Constraint |
|-------|-----|-----|-----|-----|-------|---------|------------|-------------------|
| GPT-OSS 120B | 30 | 1,000 | 8,000 | 200,000 | 500 t/s | 128K | 65,536 | **TPD** |
| GPT-OSS 20B | 30 | 1,000 | 8,000 | 200,000 | 1,000 t/s | 128K | 65,536 | **TPD** |
| Llama 3.3 70B | 30 | 1,000 | 12,000 | 100,000 | 280 t/s | 128K | 32,768 | **TPD** |
| Llama 3.1 8B | 30 | 14,400 | 6,000 | 500,000 | 560 t/s | 128K | 131,072 | **TPD** |

### Groq Production Systems (Free Plan)

| System | RPM | RPD | TPM | TPD | Speed | Context | Max Output | Binding Constraint |
|--------|-----|-----|-----|-----|-------|---------|------------|-------------------|
| Compound | 30 | 250 | 70,000 | — | 450 t/s | 128K | 8,192 | **RPD** |
| Compound Mini | 30 | 250 | 70,000 | — | 450 t/s | 128K | 8,192 | **RPD** |

### Groq Preview Models (Free Plan) — May Vanish Without Notice

| Model | RPM | RPD | TPM | TPD | Speed | Context | Max Output | Binding Constraint |
|-------|-----|-----|-----|-----|-------|---------|------------|-------------------|
| Qwen3 32B | 60 | 1,000 | 6,000 | 500,000 | 400 t/s | 128K | 40,960 | **TPD** |
| Llama 4 Scout 17B | 30 | 1,000 | 30,000 | 500,000 | 750 t/s | 128K | 8,192 | **RPD** |

### Gemini — Your Actual Limits (from AI Studio screenshot)

| Model | RPM | RPD | TPM | Context | Status |
|-------|-----|-----|-----|---------|--------|
| Gemini 3 Flash | 5 | 20 | 250,000 | 1M | Preview |
| Gemini 3.1 Flash Lite | 15 | 500 | 250,000 | 1M | Preview |
| Gemini 2.5 Flash | 5 | 20 | 250,000 | 1M | Production |
| Gemini 2.5 Flash Lite | 10 | 20 | 250,000 | 1M | Production |

### Key Insight

**GPT-OSS 120B is your best production model** but 200K TPD caps you at ~13 features/day. Llama 3.3 70B is worse at 100K TPD (~6 features). If you're willing to use preview, **Qwen3 32B's 500K TPD** is the most generous on Groq. On Gemini, **3.1 Flash Lite at 500 RPD** is your only viable daily driver — everything else caps at 20 RPD (~6 features).

---

## Strategies

### Strategy 1: Quality Cascade ⭐ RECOMMENDED

Route every request through a three-tier waterfall based on task complexity.

**Hard tasks** (architecture, complex debugging, multi-file refactoring):
- Models: **GPT-OSS 120B** + **Gemini 3 Flash** (sparingly)
- Budget: ~13 features from 120B + ~6 from Gemini 3 Flash = **~19 hard features/day**

**Medium tasks** (function implementation, code review, moderate debugging):
- Models: **Qwen3 32B** (preview) + **Gemini 3.1 Flash Lite**
- Budget: ~33 from Qwen3 + ~166 from Gemini 3.1 FL = **plenty of medium features**

**Simple tasks** (boilerplate, formatting, docstrings, quick completions):
- Models: **GPT-OSS 20B** + **Llama 3.1 8B**
- Budget: Virtually unlimited

**Why this works:** GPT-OSS 120B (LCB 69%) gets priority for hard tasks. For the truly hardest problems, spend 1 of your 20 daily Gemini 3 Flash requests for its 1M context + higher reasoning. Medium work goes to Qwen3 32B (500K TPD is enormous) or Gemini 3.1 Flash Lite (500 RPD, no TPD wall). Simple stuff burns through Llama 3.1 8B's massive 14.4K RPD.

**Trade-off:** Relies on Qwen3 32B which is preview (could vanish like Kimi K2 did).

**Estimated capacity: ~45+ features/day across all tiers**

---

### Strategy 2: Production Only — MOST STABLE

Zero preview models — only what's guaranteed to stay available.

**Primary — all coding:** **GPT-OSS 120B** → 200K TPD = ~13 features  
**Speed overflow:** **GPT-OSS 20B** → 200K TPD = ~13 more features at 1,000 t/s  
**Volume overflow:** **Llama 3.1 8B** (500K TPD, ~33 features) + **Llama 3.3 70B** (100K TPD, ~6 features)

**Why this works:** If you don't want to depend on preview models (which vanish overnight — see Kimi K2), this is your safest bet. GPT-OSS 120B and 20B combined give 26 quality features. Llama 3.1 8B handles overflow with its absurd 14.4K RPD. No Gemini dependency either.

**Trade-off:** ~26 quality features/day is tight if you're building a lot.

**Estimated capacity: ~26 quality features + unlimited simple tasks**

---

### Strategy 3: Think → Build → Review ⭐ BEST FOR YOUR LOOP WORKFLOW

Match each loop iteration to a different model's strength.

**Loop 1 — Plan:** **GPT-OSS 120B** (or Qwen3 32B thinking mode)
- Plan the feature, design approach, write pseudocode
- 1 request per feature at highest quality

**Loop 2 — Implement:** **GPT-OSS 20B** at 1,000 t/s
- Generate actual code from the plan
- Fastest model on Groq — near-instant code gen from clear spec

**Loop 3 — Review & Fix:** **GPT-OSS 120B** + **Gemini 3.1 Flash Lite** (for 1M context)
- Review output, fix bugs, refine
- Switch to Gemini when you need full-codebase context

**Why this works:** This maps directly to your 2–3 loop workflow. Loop 1 uses the best reasoner for planning. Loop 2 uses the fastest model to write code from that plan — clear specs + fast model = great results. Loop 3 brings back quality for review. This conserves 120B's TPD by using 20B for the most token-heavy step (implementation). Each feature costs ~5K on 120B + ~7K on 20B + ~3K on 120B = spreading the TPD load across models.

**Estimated capacity: ~20 features/day, highest quality per feature**

---

### Strategy 4: Provider Rotation — NEVER STUCK

Bounce between Groq and Gemini — zero downtime from rate limits.

**Primary chain** (rotate when throttled):
- **GPT-OSS 120B → GPT-OSS 20B → Gemini 3.1 Flash Lite**
- 13 + 13 + 166 = ~192 features across all three

**Burst / simple** (during per-minute cooldowns):
- **Llama 3.1 8B** + **Qwen3 32B** (preview)
- 14.4K + 1K RPD = massive headroom

**Premium reserve** (hardest 5–6 problems only):
- **Gemini 3 Flash** + **Groq Compound**
- 20 RPD + 250 RPD = ~90 requests total

**Why this works:** Groq's per-minute TPM limits (8K–12K) mean you'll get throttled during intense loops. Gemini's 250K TPM means zero per-minute issues — it's the perfect complement. Use the `x-ratelimit-remaining-tokens` response header to auto-switch. The primary chain gives ~192 features/day across three models.

**Estimated capacity: ~60 features realistically in 4 hours**

---

## Model Groupings

### The "Daily Drivers" — GPT-OSS 120B + Gemini 3.1 Flash Lite
Best balance of quality and quota. 120B for quality, 3.1 Flash Lite for volume and 1M context. Two providers means one can't take you down.

### The "Speed Pair" — GPT-OSS 120B + GPT-OSS 20B
Same family, same TPD (200K each), different speeds. 120B thinks better, 20B writes faster. Use 120B to plan, 20B to implement.

### The "Token Hoarders" — Qwen3 32B + Llama 3.1 8B
Both have 500K TPD — the most generous on Groq. Qwen3 is better quality but preview. Llama 3.1 8B has 14.4K RPD on top. Combined: 1M+ tokens/day.

### The "Context Kings" — Gemini 3 Flash + Gemini 3.1 Flash Lite
Both have 1M token context windows. 3 Flash for quality (20 RPD), 3.1 Flash Lite for volume (500 RPD). Use when you need to paste entire codebases.

---

## Unavailable Models — Migration Guide

| Dead Model | Reason | Replace With |
|------------|--------|-------------|
| Kimi K2 (both variants) | Removed from Groq models page | **GPT-OSS 120B** (closest quality match, production stable) |
| Gemini 2.5 Pro | 0/0 on your project — unavailable | **Gemini 3 Flash** (quality, 20 RPD) or **3.1 Flash Lite** (volume, 500 RPD) |
| Gemini 2.5 Pro TTS | 0/0 — unavailable | N/A (not coding-relevant) |
| Gemini 3.1 Pro | 0/0 — paid only | **Gemini 3 Flash** |
| Gemini 2 Flash | 0/0 — deprecated | **Gemini 2.5 Flash** or **3.1 Flash Lite** |
| Gemini 2 Flash Lite | 0/0 — deprecated | **Gemini 2.5 Flash Lite** or **3.1 Flash Lite** |
| DeepSeek R1 Distill 70B | Deprecated from Groq | **Qwen3 32B** (preview) or **Llama 3.3 70B** (production) |
| Mixtral 8x7B | Deprecated from Groq | **GPT-OSS 20B** (similar speed tier) |

---

## Per-Minute Throttling: The Hidden Problem

RPD and TPD are the daily caps, but **TPM (tokens per minute) is what throttles you in real-time** during iterative coding. A single coding prompt with 2K input + 3K output = 5K tokens. Here's how fast each model lets you work:

| Model | TPM | Coding Requests/Min | Wait Between Requests |
|-------|-----|--------------------|-----------------------|
| GPT-OSS 120B | 8,000 | ~1.6 | ~37 seconds |
| GPT-OSS 20B | 8,000 | ~1.6 | ~37 seconds |
| Llama 3.3 70B | 12,000 | ~2.4 | ~25 seconds |
| Llama 3.1 8B | 6,000 | ~1.2 | ~50 seconds |
| Qwen3 32B | 6,000 | ~1.2 | ~50 seconds |
| Llama 4 Scout | 30,000 | ~6 | ~10 seconds |
| Compound | 70,000 | ~14 | ~4 seconds |
| Gemini (all) | 250,000 | ~50 | Instant |

**This is why Gemini is valuable despite low RPD** — its 250K TPM means zero per-minute throttling. When Groq's TPM wall hits you mid-loop, switch to Gemini for instant throughput, then switch back when the minute resets.

---

## Bottom Line

1. **GPT-OSS 120B is your #1 model.** Strongest production option on Groq, 200K TPD gives ~13 features/day with 3-loop iterations.
2. **Gemini 3.1 Flash Lite is your Gemini daily driver.** 500 RPD and 250K TPM make it the only Gemini model worth relying on.
3. **Qwen3 32B is the best value if you accept preview risk.** 500K TPD = ~33 features/day, but it could vanish like Kimi K2 did.
4. **GPT-OSS 20B at 1,000 t/s is your speed weapon.** Use it for the implementation loop after planning with 120B.
5. **Gemini's real value is TPM, not quality.** 250K TPM means zero per-minute throttling — use it as overflow when Groq's 8K TPM throttles you.

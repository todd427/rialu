---
name: billing-integration-pending
description: Multiple billing provider integrations still needed — Anthropic (needs admin key), Google Translate, Microsoft Voice, Railway, Cloudflare
type: project
---

Billing integrations still to wire up:
1. **Anthropic** — needs admin API key from console.anthropic.com > Settings > Admin Keys. Regular API key (in /home/Projects/Keys/anthro.key) only works for API calls, not usage queries. Todd has both API and Max subscription.
2. **Google Translate** — need to identify which GCP project / API key is used
3. **Microsoft Voice** — Azure Speech Services, need subscription details
4. **Railway** — have RAILWAY_API_TOKEN, just needs poller built
5. **Cloudflare** — currently all free tier, track for when it's not

**Why:** Todd wants per-project token counts, costs paid, costs owed across all providers.
**How to apply:** Remind Todd about pending integrations when billing/budget work comes up. Prioritize Anthropic admin key since it's the biggest cost.

# FAWN Merchant Compliance Path — Cannabis Pilot

**Status:** working analysis for Alex Goldsmith / GoldstarOrbital Inc.
**Date:** 2026-08 · **Author:** engineering (Claude)
**This is not legal advice.** Every item marked ⚖️ needs sign-off from a
cannabis-specialized payments attorney before you take a single live dollar.

---

## 0. Read this first — three findings that change the plan

### 0.1 The Stripe premise doesn't hold
The brief says "scaffold using our existing Stripe Issuing/Treasury stack."
Two problems:

1. **That stack doesn't exist.** Stripe in this codebase is only
   `services/stripe_payouts.py` (bank payouts) and founding-member Checkout
   sessions. There is no Connect onboarding, no Issuing, no Treasury.
2. **It couldn't be used anyway.** Stripe's prohibited-business list covers
   marijuana. Onboarding dispensaries onto Stripe rails risks termination of
   FAWN's *existing* Stripe account — which would take out the founding-member
   payment flow too. **Do not connect dispensaries to anything Stripe-touching.**

This is not a setback. FAWN already has the *better* rail for this vertical:
the closed-loop network codex built (`routers/closed_loop.py`) — merchant
accounts, checkouts, NFC tap, and $0.01/$0.01 fees settling between FAWN
custodial USDC balances. No card network, no Stripe, no interchange. That is
precisely why FAWN can quote $0.01 when Aeropay quotes 2.5% + $0.25.

### 0.2 FAWN's P2P feature currently breaks the cheapest exemption ⚖️
FinCEN's prepaid-access rule exempts **closed-loop** value of **≤$2,000/day**.
But the exemption is lost if funds can be *"electronically transferred to other
users of the prepaid access."*

FAWN's core consumer product is exactly that: `@username` P2P transfers. So
FAWN balances today are **not** closed-loop for exemption purposes. This is the
single most consequential architectural fact in this document.

**Two ways out, pick one:**

| Option | What it means | Cost |
|---|---|---|
| **A. Agent-of-payee** (recommended) | Keep P2P. Rely on the agent-of-payee exemption for the *merchant* leg only: FAWN contracts as the merchant's agent, so receipt by FAWN legally discharges the customer's debt to the merchant. | Legal drafting only |
| **B. Segregated merchant purse** | A separate, non-P2P-transferable "spend" balance used only at merchants, capped <$2,000/day. Preserves the closed-loop exemption. | Real engineering |

Option A is faster and cheaper, and it is how payment facilitators normally
operate. Option B is the fallback if counsel dislikes A in a target state.

### 0.3 The real bottleneck is the merchant's off-ramp, not your code
FAWN can move USDC to a dispensary's balance instantly and near-free. But the
dispensary must eventually pay staff, rent, and taxes in **USD**. Converting
USDC→USD requires a bank that knowingly accepts marijuana-related business
(MRB) funds. **No amount of code solves this.** Any pitch that ignores it will
die in the dispensary's CFO conversation.

Mitigations, in order of realism:
1. Target dispensaries that **already have an MRB-friendly credit union**
   (many do — they pay ~$500–$2,000/mo in MRB account fees). FAWN just feeds
   that existing account. **This is the fast path — qualify for it in outreach.**
2. Merchant holds USDC as working capital and pays MRB-friendly vendors directly.
3. Partner with an MRB-accepting off-ramp/OTC desk. Slowest, needs diligence.

---

## 1. Federal layer

| Item | 2026 status | Impact on FAWN |
|---|---|---|
| Cannabis scheduling | Still Schedule I; rescheduling did **not** create a banking safe harbor | Every downstream bank treats this as high-risk |
| SAFE Banking Act | **Not enacted.** Do not build a plan that assumes it passes | Plan must work without it |
| FinCEN MRB guidance (FIN-2014-G001) | Live; banks serving MRBs file Marijuana Limited/Priority/Termination SARs | Your banking partner inherits SAR duty |
| FinCEN MSB registration | Required if FAWN is a money transmitter | ~$0 filing, but triggers BSA/AML program cost |
| Card networks (Visa/MC) | Prohibit cannabis; 2021 crackdown killed "cashless ATM" misuse | ❌ Never route cannabis over card rails |

**On "cashless ATM" workarounds:** widely used, and widely understood to
involve misrepresenting transactions to the debit networks. Visa's 2021
crackdown made this an enforcement target. **Do not build this.** It is the
single fastest way to lose your processor, your bank, and your credibility —
and it is precisely the thing FAWN's honest $0.01 rail makes unnecessary.

---

## 2. State layer — where you can actually launch fastest

Two independent filters must **both** pass:

- **Filter 1 — Agent-of-payee exemption exists.** 42 states recognize it
  (via MTMA adoption or pre-existing law). **Missing in: FL, NJ, NM, OK, OR,
  RI, UT, WY, and DC.**
- **Filter 2 — No cannabis-specific processor mandate.** Washington is the
  cautionary case: WAC 314-55-115 requires the money transmitter to be
  **licensed by WA DFI**, so WA is *not* fast. (POSaBIT also holds ~85% of WA
  retail — bad beachhead economics regardless.)

### Ranked pilot candidates

| Rank | State | Adult-use | Agent-of-payee | Notes | Verdict |
|---|---|---|---|---|---|
| **1** | **California** | ✅ | ✅ | Home turf (Bay Area — you can walk in). DFPI issues **written interpretive opinions** on closed-loop/agent-of-payee — cheap written comfort. Largest market. | **Pilot here** |
| 2 | Colorado | ✅ | ✅ | Most mature regulator (MED), clearest rules, high dispensary density | Strong fallback |
| 3 | Michigan | ✅ | ✅ | Huge, fragmented market — good cold-start dynamics | Expansion #2 |
| 4 | Missouri | ✅ | ✅ | Newer adult-use, fast growth, less processor lock-in | Expansion #3 |
| — | Washington | ✅ | ✅ | ⚠️ WAC 314-55-115 requires WA DFI MTL; POSaBIT ~85% share | **Avoid at launch** |
| — | Oregon / NJ / NM / RI | ✅ | ❌ | No agent-of-payee exemption | Avoid until licensed |

**Recommendation: pilot in California.** The deciding factor is not a marginal
legal edge — it's that the PayPal cold-start model requires physical density
and founder presence, and you are in the Bay Area. CA also uniquely lets you
*buy certainty cheaply* via a DFPI interpretive opinion request.

---

## 3. Compliance checklist, ranked by cost × speed

### Tier 0 — Do before touching a dispensary (days, ~$0)
- [ ] **Do not** route any cannabis volume through Stripe or card rails.
- [ ] Decide Option A (agent-of-payee) vs Option B (segregated purse). ⚖️
- [ ] Write the merchant agreement with **explicit agent-of-payee language**:
      FAWN receives funds *as agent of the merchant*, and the customer's
      obligation is discharged on FAWN's receipt. This single clause is what
      the exemption hangs on. ⚖️
- [ ] Confirm the dispensary already banks with an MRB-friendly institution
      (make it a qualifying question in outreach).

### Tier 1 — Cheap, fast, high leverage (2–6 weeks, ~$5k–$25k)
- [ ] Engage cannabis-payments counsel for a written CA memo. ⚖️
- [ ] **Request a DFPI interpretive opinion** on the FAWN structure. Cheap,
      slow-ish, and produces written regulator comfort you can show investors.
- [ ] Register with FinCEN as an MSB if counsel says the P2P leg requires it
      (filing is free; the BSA/AML program behind it is the real cost).
- [ ] Stand up a written **BSA/AML program**: designated compliance officer,
      KYC/KYB, transaction monitoring, SAR procedures, independent review.
      *(FAWN already has OFAC screening, GoPlus address risk, velocity limits,
      and a review-hold queue — you are further along here than most.)*
- [ ] Cannabis-specific diligence per merchant: state license number, expiry,
      license status re-verification. **Implemented** — see §5.

### Tier 2 — Only if counsel says the exemption doesn't cover you (3–12 months, $50k–$500k+)
- [ ] State money transmitter licenses. App fees ~$500–$10,000/state; surety
      bonds ~$25,000–$2,000,000/state; plus audited financials, net-worth
      minimums, background checks.
- [ ] This is the "expensive and slow" branch. **The entire point of Tier 0–1
      is to avoid landing here.**

### Tier 3 — Ongoing
- [ ] Annual license re-verification (automated: `license_expiry_sweep()`).
- [ ] SAR filing where applicable. ⚖️
- [ ] Monitor SAFE Banking; re-plan if it passes.

---

## 4. Honest cost/time summary

| Path | Time to first live dispensary | Cash cost |
|---|---|---|
| Tier 0 + 1 (agent-of-payee, CA) | **6–10 weeks** | **~$15k–$40k** (mostly legal) |
| Tier 2 (full MTL, multi-state) | 6–18 months | $250k–$1M+ |

"Quickly and cheaply" is achievable **only** on the Tier 0–1 path, and only if
counsel blesses agent-of-payee. Budget the legal spend — it is the cheapest
insurance available, and it is far less than one enforcement action.

---

## 5. What is already implemented in code

`routers/merchant_onboarding.py` + `models_merchant.py` (12 passing tests):

| Control | Implementation |
|---|---|
| KYB capture | Legal name, entity type, address, state, beneficial owners |
| EIN minimization | Stored as last-4 + SHA-256 hash only — never plaintext |
| Cannabis licensing | License number, issuing state, expiry — **required** to submit |
| Expired license | Blocks submission, blocks verification, blocks live API keys |
| No auto-approval | High-risk verticals reach `verified` only via a human admin decision |
| Continuous re-check | `license_expiry_sweep()` flips lapsed merchants to `expired` |
| Live-key gating | Live API keys require verified KYB **and** an active merchant |
| Audit trail | Every state change → 7-year `UserAuditLog` entry |
| Secret handling | API keys stored as SHA-256; plaintext shown exactly once |

**Not implemented, deliberately:** automated fiat/bank payout for MRBs. It
would imply a capability that does not exist and cannot exist without an
MRB-friendly banking partner (§0.3).

---

## Sources
- [Hemp & Cannabis Payments 2026 — cannabisregulations.ai](https://www.cannabisregulations.ai/cannabis-and-hemp-regulations-compliance-ai-blog/hemp-cannabis-payments-banking-2026)
- [Cannabis Banking & Payment Compliance: 2026 Operating Guide — Greenstar](https://www.greenstaratm.com/2026/07/14/cannabis-banking-and-payment-compliance-a-2026-operating-guide-for-multi-state-dispensary-operators/)
- [Navigating Cannabis Payment Processing — Goodwin](https://www.goodwinlaw.com/en/insights/publications/2024/03/insights-finance-ftec-navigating-challenges-solutions-cannabis-payment-processing)
- [FinCEN Prepaid Access Final Rule](https://www.federalregister.gov/documents/2011/07/29/2011-19116/bank-secrecy-act-regulations-definitions-and-other-regulations-relating-to-prepaid-access)
- [Agent of the Payee Exemption Map — CSBS](https://www.csbs.org/agent-payee-exemption-map)
- [Money Transmitter Model Law — Wilson Sonsini](https://www.wsgr.com/en/insights/money-transmitter-model-law-poised-to-change-regulatory-landscape.html)
- [Agent of the Payee Exemption — Modern Treasury](https://www.moderntreasury.com/learn/what-is-an-agent-of-the-payee-exemption)
- [WAC 314-55-115 (Washington)](https://app.leg.wa.gov/wac/default.aspx?cite=314-55-115)
- [Money Transmitter License Requirements 2026 — InnReg](https://www.innreg.com/blog/money-transmitter-license-steps-and-requirements)
- [Aeropay](https://www.goaeropay.com/) · [POSaBIT](https://www.posabit.com/payments-features) · [Dutchie Payments](https://business.dutchie.com/payments)

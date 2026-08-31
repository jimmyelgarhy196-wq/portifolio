# Legal and regulatory position — GMG AI Solutions

**This document is an internal record, not legal advice.** It states what the
business does, what the published legal documents say, and what has *not* yet
been done.

---

## 1. What GMG AI Solutions actually does

GMG AI Solutions operates **GMG Investment Intelligence**, a paid subscription
service that provides:

- market information about securities listed on the Egyptian Exchange
- analytics computed from that information (scores, ratios, valuation models)
- written research commentary, some AI-assisted and labelled as such
- personal tools: watchlists, a holdings tracker, a screener, alerts

Customers pay a monthly subscription fee for access to the service. That is the
entire commercial relationship.

## 2. What it does not do

The following are absent from the business model **and from the codebase**:

| Activity | Status |
|---|---|
| Accepting or holding client money | Never. No client-funds account exists. |
| Holding client securities / custody | Never. No custody record exists. |
| Executing or routing orders | Never. There is no broker integration and no execution path. |
| Discretionary portfolio management | Never. The portfolio feature stores what the user enters. |
| Personal investment advice | Never. Output is not tailored to any individual. |
| Guaranteeing or promising returns | Never. Prohibited wording is absent throughout. |

The portfolio tool holds nothing: it records holdings the user types in so they
can be valued. It is not connected to any broker or depository.

## 3. Regulatory claims made

**None.** GMG AI Solutions does not claim, and the platform nowhere states,
that it is licensed, registered or authorised by:

- the Egyptian Financial Regulatory Authority (FRA)
- the Central Bank of Egypt
- any other regulator, in Egypt or elsewhere

The landing page, the footer of every public page, the Terms of Service and the
Investment Disclaimer each state this explicitly and in the negative.

## 4. Published legal documents

| Document | Route | Covers |
|---|---|---|
| Terms of Service | `/terms` | The service, what it is not, eligibility, acceptable use, IP, liability, governing law |
| Privacy Policy | `/privacy` | Data collected, purposes and legal bases, sharing, retention, rights, security |
| Investment Disclaimer | `/disclaimer` | No advice, no guarantee, no custody, regulatory status, data and AI limits |
| Risk Disclosure | `/risk-disclosure` | Market risk, Egypt-specific risks, model risk, what to do before investing |
| Subscription Terms | `/subscription-terms` | Plan, trial, billing, cancellation, price changes, non-payment |
| Refund Policy | `/refund-policy` | 14-day first-period refund, renewals, what is never refundable |
| Cookie Policy | `/cookies` | The single session cookie; no analytics or advertising trackers |

All seven are drafted to describe the business model above accurately, in
plain language, and are dated and versioned in
`backend/api/routes_public.py::LEGAL_VERSION`.

---

## 5. ⚠️ OUTSTANDING — REQUIRED BEFORE COMMERCIAL LAUNCH

> **Have Egyptian legal counsel review all legal documents and confirm whether
> any FRA, Central Bank, consumer-protection, data-protection, tax, e-commerce
> or other authorization/registration requirements apply to the exact GMG
> business model.**

The documents above were drafted to describe the business accurately. They have
**not** been reviewed by a qualified Egyptian lawyer. Specific questions for
counsel:

1. Does providing paid, non-personalised investment research and analytics in
   Egypt require FRA authorisation or registration of any kind, given that no
   client money, custody, execution or discretionary management is involved?
2. Does the AI-generated research commentary change that answer?
3. What consumer-protection obligations attach to an online subscription sold
   to Egyptian consumers — cooling-off, cancellation, refunds, price-change
   notice, complaint handling?
4. What are the obligations under Egypt's Personal Data Protection Law
   (Law 151/2020), including any registration, DPO appointment, breach
   notification and cross-border transfer requirements? Does the Privacy Policy
   meet them?
5. What VAT, income-tax and invoicing obligations attach to subscription revenue?
6. Are there e-commerce or electronic-signature requirements for the sign-up and
   contract flow (Law 15/2004 and related)?
7. Are the liability limitations in the Terms of Service enforceable under
   Egyptian law, and is the governing-law and jurisdiction clause appropriate?
8. Does market-data redistribution require a licence from the Egyptian
   Exchange, and what are its terms?
9. Is the company name, branding and any Arabic-language material compliant with
   advertising and financial-promotion rules?

**Do not open the service to paying customers until this review is complete.**

This requirement is also recorded in code, at
`backend/api/routes_public.py::LEGAL_REVIEW_TODO`, so it cannot be lost.

---

## 6. Language audit

The following words and phrases are **prohibited** anywhere in the product,
marketing or research output, and are absent from the codebase:

- "guaranteed profit", "guaranteed return", "guaranteed income"
- "risk-free", "no risk", "cannot lose"
- "we will make you money", "assured returns"
- any claim of FRA licensing, registration or approval
- any statement that GMG holds, manages or invests customer funds

Verify with:

```bash
grep -rniE "guaranteed (profit|return|income)|risk[- ]free|licensed by the (FRA|Egyptian Financial)" \
  backend/ frontend/ *.md
```

## 7. Data-source obligations

Market data carries the terms of whoever supplies it. Before connecting a
licensed provider, confirm with counsel and the vendor:

- whether the licence permits display to subscribers, and on what delay
- whether redistribution, export or API re-serving is permitted
- what attribution the vendor requires on screen
- whether a separate Egyptian Exchange licence is needed

The platform already displays the source, timestamp and delay of every quote,
which supports most vendor attribution requirements.

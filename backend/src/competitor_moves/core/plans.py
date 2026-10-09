"""Pricing plans: single source for the marketing page, plan limits and credits (ported from the UI's lib/plans.js)."""

PLANS = {
    "starter": {
        "id": "starter", "name": "Starter", "price": 29, "competitors": 3, "credits": 300,
        "blurb": "For founders keeping an eye on their closest rivals.",
        "features": ["3 competitor sites", "300 crawl credits / month", "Price & promo change detection",
                     "AI digest with 3–5 actions", "Crawl API access"],
    },
    "pro": {
        "id": "pro", "name": "Pro", "price": 79, "competitors": 10, "credits": 2000, "popular": True,
        "blurb": "For product and marketing teams that move fast.",
        "features": ["10 competitor sites", "2,000 crawl credits / month", "Everything in Starter",
                     "New page & product launch tracking", "Priority crawl queue"],
    },
    "business": {
        "id": "business", "name": "Business", "price": 199, "competitors": 30, "credits": 10000,
        "blurb": "For companies tracking a whole market.",
        "features": ["30 competitor sites", "10,000 crawl credits / month", "Everything in Pro",
                     "Higher API throughput", "Dedicated onboarding"],
    },
}
ANNUAL_DISCOUNT = 0.2

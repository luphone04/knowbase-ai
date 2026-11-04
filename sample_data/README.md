# Sample data

`demo_store/` holds five **fictional** policy documents for Northstar Outfitters,
a company invented for this demo (SPEC.md §1.3). Upload them to try Knowbase AI and
to check retrieval. The facts are internally consistent across files:
- 30-day returns, but 14 days for laptops;
- a 2-year tent warranty;
- free standard shipping over $50;
- exact product and policy terms such as `Trailblazer 14`, `Summit 2P`, `RMA`, and
  `NorthCare Plus`.

The evaluation questions are in
[`backend/app/evaluation/datasets/northstar-demo-v1.json`](../backend/app/evaluation/datasets/northstar-demo-v1.json)
(inside the backend package, so the API and worker images include it; ADR 0014). They name
these files as their expected sources and record each file's SHA-256: **edit a file here and
the evaluation no longer accepts it** until the dataset gets a new version.

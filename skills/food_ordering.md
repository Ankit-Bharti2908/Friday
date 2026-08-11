---
name: food_ordering
description: Searching the Swiggy catalog and recommending real orders that fit the day's diet targets
triggers: order food, order something, swiggy, instamart, zomato, takeout, order in, eat out, restaurant, dineout, groceries, delivery
agents: diet
---
The catalog tools (Swiggy Food / Instamart / Dineout) turn a plan into food
that actually arrives. They are only useful when aimed at the profile's
targets — a search result is an ingredient list, not a recommendation.
If no catalog tool is available in this turn, say so plainly and give the
"what to look for" version instead of pretending to have searched.

## 1. Before searching
Read the diet profile (targets, pattern, allergens) and today's plan if one
exists. Know these three numbers before the first search:
- calories left today, protein left today, and the budget the owner set.
Ask only what you can't infer (e.g. "lunch or dinner?" / "roughly what
budget?") — one short question, not a form.

## 2. Searching
- Search with the constraint in the query, not after it: "high protein
  paneer bowl", "grilled chicken salad", "dal", "curd". Prefer the owner's
  usual cuisine; a familiar dish gets eaten, an optimal one gets abandoned.
- Run 2-3 focused searches rather than one broad one, and stop. Do not
  page through the whole catalog — each read costs a step.
- Instamart is for INGREDIENTS (the plan's groceries, a protein top-up:
  curd, paneer, eggs, milk, fruit). Food is for prepared meals. Dineout is
  for a planned meal out. Pick the one that matches the ask.
- Never treat a menu blurb as macros. Restaurant portions run 20-30% above
  what they look like, and listed calories are optimistic when they exist
  at all. Estimate from what's IN the dish and say it's an estimate.

## 3. Recommending (this is the actual job)
Give 2-3 options, never a wall of results, each on one line:
`Dish — restaurant — ₹price — ~kcal, ~g protein — why it fits / what it costs you`
Then one plain sentence naming the tradeoff between them ("the bowl is 20 g
more protein for ₹60 more; the thali is cheaper but rice-heavy").
Rank by: fits the remaining protein > fits remaining calories > respects the
pattern and budget > delivery time. Ties break toward the familiar dish.
Say what it does to the rest of the day: "this leaves ~500 kcal and 35 g
protein for dinner — curd and a fruit covers it."
- Allergens and the eating pattern are ABSOLUTE filters, exactly as in a
  planned day: never present a dish containing an allergen, not even as a
  "you could ask them to leave it out".
- If nothing in the results fits, say that instead of stretching one to
  fit — then give the closest option plus the honest cost ("this is 900
  kcal, which puts you ~300 over; worth it if you're actually hungry, or
  split it and keep half for tomorrow").
- A single indulgent order is not a failure: absorb it into the week, no
  guilt language, no compensatory starvation the next day.

## 4. Ordering — the owner's decision, always
- You may SEARCH and BROWSE freely. Carts, orders, bookings and payments
  pause for approval by policy, and that is the intended flow: propose,
  then let the owner approve the exact action.
- Before proposing any order-placing call, state the FULL cost in one line:
  total price including delivery and fees where the tool reports them, plus
  the calorie/protein hit. No surprises inside an approval prompt.
- One order per request. Never chain a second order after an approval, and
  never re-place an order that was rejected — ask what to change instead.
- Repeat/reorder shortcuts still need the same explicit approval.
- If the tool reports the order is COD-only or the booking has conditions,
  say so BEFORE the owner approves, not after.

## 5. After
Note what was ordered against the day's targets — if a day plan exists,
offer to update the remaining meals so the totals still land. Remember the
owner's genuinely liked orders (a one-line `remember`), not every search.

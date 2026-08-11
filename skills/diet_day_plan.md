---
name: diet_day_plan
description: How the diet agent builds one day's meal plan from the profile targets and that day's training
triggers: diet plan, meal, meals, what to eat, what should i eat, nutrition, calorie, macros
agents: diet
---
Diet plans are built ONE DAY AT A TIME — never a whole week in one shot.
Inputs before designing: the profile targets (get_diet_profile), that day's
session (get_todays_workout, or get_workout_plan for another date), and the
last few day plans (get_recent_diet_plans) so meals rotate. Priority when
constraints collide: adherence > calories & protein > everything else — the
best plan is the one the owner will actually cook and eat.

## 1. Day skeleton
- Meals = the owner's REAL schedule (3-5). Every meal carries its protein
  anchor, ~0.4 g/kg (25-40 g); 4 protein feedings are ideal, 3 works with
  bigger doses. Never cram the day's protein into 1-2 meals.
- Owner skips breakfast? Fine — redistribute, don't lecture (meal frequency
  doesn't change fat loss at equal calories; adherence does).
- Same calorie target every day by DEFAULT. Optional, only if the owner
  likes structure: shift 50-100 g of carbs from rest days onto training
  days, weekly total unchanged — frame it as a performance preference, not
  fat-loss magic.
- Around training: a normal meal 1-4 h before covers a gym hour (running
  in <1 h → something small and carby: banana, poha, toast). Protein-rich
  meal within a few hours after — only urgent if they trained fasted.
  Evening lifters: dinner after the session; a curd/milk/paneer-style last
  meal helps overnight recovery. Sessions under ~75 min need nothing
  mid-workout except water.
- Rest days: identical protein, calories per the cycling choice, water
  unchanged. Rest days are growth days — say so when the owner wants to
  "eat less to earn food".

## 2. Menu construction rules
- Build from the OWNER'S cuisine and staples — dal-roti-sabzi-curd hits
  macros as well as any chicken-and-rice stereotype. Foreign "bodybuilder
  food" plans die in a week.
- Templates beat novelty: keep breakfast and lunch near-fixed across the
  week, rotate dinners and vegetables. Check recent plans — don't repeat
  the same dinner more than 2 days running unless asked. At most 1-2 new
  recipes a week.
- Portions in household measures WITH grams: "dal 2 katori (≈300 ml,
  thick)", "paneer 100 g (palm-size)", "rice 1 katori cooked", "2 rotis".
  During fat loss, weigh only the calorie-dense traps: oil, ghee, nuts,
  nut butters, dry grains — eyeballed oil is how deficits vanish.
- Exclusions are ABSOLUTE: allergens never appear, not even as "optional
  swap"; a vegetarian plan contains zero egg unless they said ovo-veg.
- Every day: 300-400 g vegetables spread across meals, 1-2 fruit servings,
  a stated oil budget ("cook today in ≤3 tsp oil total"), salt ≤5 g, the
  water target restated once.
- Eating out that day? Plan it in: protein-anchor + vegetables, sauces and
  dressings on the side, and count the meal ~20-30% above what it looks
  like. 1-2 planned outside meals a week fit inside the weekly budget —
  no guilt, no compensating starvation day after.

## 3. Day-plan document format (save_diet_plan) — for a nutrition newcomer
- Title: date + training context ("Tuesday 12 Aug — Push day, 7 pm").
- One `## <Meal> — <time>` section per meal (`## Breakfast — 8:30`); the
  morning alert sends the file exactly as saved.
- Each food line:
  `Food — household measure (grams) — ~kcal, ~g protein — note/swap`.
  The note is a why or a form tip in plain words ("soaked overnight",
  "vitamin-C side so the iron absorbs"). End every meal with
  `Meal ≈ X kcal · Y g protein`.
- Give one realistic swap per main meal, matched on protein and rough
  calories ("no paneer at home → 3 whole eggs or 40 g soy chunks, same
  protein").
- Close with `## Totals`: kcal, protein, fat ≈, carbs ≈, fiber, water —
  each shown AGAINST the profile target ("Protein 128 g / target 130 g") —
  plus one prep line for tomorrow ("soak chana tonight").
- First weeks: one-line glossary at the top (katori ≈ 150 ml bowl; hung
  curd = curd drained thick, double the protein).
- Keep it cookable TODAY: nothing they can't buy or make with the kit and
  time they told you about.

## 4. Self-check before presenting (audit every plan)
Totals within ~5% of the calorie target · protein within one meal-dose of
target · fat at or above the floor (0.5 g/kg) · fiber roughly on target ·
zero allergens/exclusions · every meal has a named protein anchor ·
portions in household measures + grams · vegetables present · training
meal placed sensibly for that day's session · no dinner repeated >2 days
running · nothing below the calorie floor · totals honest (add up the meal
lines — no wishful arithmetic).

## 5. Non-negotiables (refuse kindly, explain, counter-offer)
- Never below 1,200 kcal (women) / 1,500 kcal (men). "Crash cut for the
  wedding" gets one plain sentence on muscle loss + rebound, then the
  floor + a bigger step count as the counter-offer.
- No cuts faster than ~1% BW/week; an already-lean owner asking for an
  aggressive cut gets maintenance/recomp as the counter-offer.
- Medical flags from the intake screen → healthy-plate guidance only;
  therapeutic diets (diabetes, kidney, pregnancy) belong to a doctor/RD.
- No demonized foods, no whole food groups cut without a stated reason
  (allergy, pattern, preference). No supplement stacks — food first; whey/
  creatine/B12/D only as the intake skill flags them.
- One heavy social meal gets absorbed into the week, never "punished".
- If the owner insists after hearing the tradeoff, do the safest version
  and note the deviation at the top of the day plan.

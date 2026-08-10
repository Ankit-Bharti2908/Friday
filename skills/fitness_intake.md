---
name: fitness_intake
description: Intake interview, safety screen, and level rubric for the gym agent
triggers: gym, workout, fitness, trainer, exercise, get fit, lose weight, build muscle
agents: gym
---
Run intake like a coach's first consultation (order used by ACE/NASM/ACSM:
rapport & goals → health screen → history & lifestyle → self-tests → only then a
plan). Ask 2-4 questions per message, react to answers, mirror back what you
heard. Never dump the full questionnaire.

## 1. Safety screen (do this before any programming — PAR-Q+ 2020)
Ask, conversationally: heart condition or high blood pressure ever diagnosed?
Chest pain at rest or with activity? Dizziness/loss of consciousness in the last
12 months? Any other chronic condition (diabetes, respiratory, osteoporosis…) or
prescribed meds for one? Bone/joint/soft-tissue problem in the past 12 months
that activity could worsen? Ever told to exercise only under supervision?
Pregnant? Surgery in the last ~6 months?
- Any YES → advise seeing a doctor before starting; program only gentle activity
  (walking) until cleared. Chest pain, fainting, resting BP ≥180/110, or a recent
  cardiac event → doctor first, no exceptions.
- Meds matter: beta-blockers invalidate heart-rate zones (use RPE/talk test);
  insulin → hypoglycemia risk, carbs nearby; statins → ramp slowly.
- Joint issues or past injuries: note exactly which movements hurt — you'll
  substitute around them, never through them.
- All NO → cleared; still start light and build gradually.
- Teach the in-session stop rules with the first plan: stop for chest pain,
  unusual breathlessness, lightheadedness, or palpitations.
- HARD GATE: never save a workout plan while the profile's Health flags lack a
  screen outcome — write "cleared" or "advised doctor first" explicitly. If the
  screen hasn't happened yet, it is always the next question.

## 2. What to collect (MUST before designing; ask the rest as it fits)
MUST: age, sex, height, weight · primary goal RANKED if several ("if only one
thing improved in 6 months, which?") + target & timeline · training history
(what, how long, how consistently, current layoff length) · confidence on
squat/hinge/press/pull + any known working weights · realistic days/week +
minutes/session + preferred time · equipment (full gym / home barbell /
dumbbells-only + which weights / bands / bodyweight) · injuries & pain · the
safety screen above.
NICE (weave in later): sleep hours · desk vs on-feet job · stress · steps/day ·
exercises they love/hate · sports they already play · diet pattern & willingness
to track calories/protein (full macros vs protein-only vs habit-based) · resting
heart rate · past attempts and what killed them.
Set expectations honestly: visible change ~8-12 weeks; fat loss 0.5-1% BW/week;
muscle ≈1-1.5% BW/month for new lifters, halve it per year of training.

## 3. Optional self-tests (home benchmarks; also 4-6-week re-test markers)
- Max strict push-ups: men <17 below-par / 22+ good / 36+ excellent; women <10 /
  15+ / 30+ (20s norms; -2-3 per decade after 40). Can't do one → incline track.
- Plank: <30 s weak · 60-90 s average · 2 min+ strong.
- Bodyweight squats, one set: <10 clean → beginner + mobility work; 40+ strong.
- Can you walk briskly 30 min / jog 10 min nonstop / run 5K? → picks the cardio
  starting phase (walk-run → continuous → intervals).
- Movement checks (verbal): squat to parallel heels down? toes touched, knees
  straight? arms overhead against a wall? one-leg balance 10 s? Each NO → add
  matching mobility work.

## 4. Level rubric — combine signals, take the LOWEST (under-classifying is safe)
1. Effective training age (count ONLY stretches ≥2x/week with progressing loads):
   <6 months → beginner · 6 months-2 years → intermediate · 2+ years → advanced.
   Calendar years ≠ training age: "5 years, sporadic, same weights" = beginner.
2. Detraining: layoff <1 month → keep level, deload a week. 1-6 months → drop one
   level to restart (muscle memory brings it back in 4-12 weeks — say so).
   >6-12 months → program as beginner, progress fast.
3. Rate of progress: still adding load every session → novice responder; adds
   weekly → intermediate; PRs take months of planning → advanced.
4. Strength check (1RM ÷ bodyweight, men | women ≈ 60-70% of these): squat 1.5 |
   1.25 · bench 1.0 | 0.75 · deadlift 2.0 | 1.5 = intermediate; ~2.25/1.5/2.5 =
   advanced. Someone "advanced" benching <1x BW is intermediate at most.
5. Technique: machines-only or unsure on the big patterns caps them at beginner
   programming for those lifts regardless of anything else.
Never ask the owner to pick their own level — assess it with this rubric from
their answers; self-labels are one weak vote and people overstate. Tell them the
level you assessed and why, kindly: it sets progression speed (per session /
weekly / per block), volume (8-12 vs 10-20 sets/muscle/week) and split (per the
gym_program_design skill).

## 5. Saving the profile (update_fitness_profile)
Write markdown with sections: Stats (age/sex/height/weight, date) · Goal (ranked,
target, timeline, why) · Level (+ the signals that decided it) · Schedule
(days, minutes, time of day) · Equipment · Health flags & injuries (including
"cleared" or "advised doctor first") · Lifestyle (sleep, job, stress, steps) ·
Preferences · Baselines (self-test numbers, known lifts). Re-run the relevant
questions and update the file whenever weight, schedule, or goals change; note
date of change. Also `remember` a one-liner: goal, level, days/week.
If the user is impatient, the minimum viable set is: safety screen, age/sex/
height/weight, goal, training age + consistency, days+minutes, equipment,
injuries — defaults cover the rest (protein 1.6-2.2 g/kg/day, sleep 7-9 h,
150+ min cardio/week, start light).

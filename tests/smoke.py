"""Offline smoke test — no network, no API keys needed.

    uv run python -m tests.smoke

Covers: db (+vec round-trip, alert dedupe), policies, both graph modes,
history trim, prompt build, keyword pre-router precision, sticky routing
resolution, duplicate-tool-call detection, skills parsing/matching, agent
registry tool filtering, memory store/recall/forget with injected vectors
(+ exact-text dedupe, forget-all on a temp db, transient-extraction filter),
approval preview rendering, fitness profile/plan/log round-trip (+ today's-
section parsing + state-aware sentinels + full reset), nutrition profile/
day-plan round-trip (+ date resolution + sentinels + full reset), heartbeat
parsing, notify quiet-hours math.
"""
from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from agents import registry
from agents.heartbeat import _parse_findings
from core import approval, db, memory, notify, prompts, settings, skills
from core.db import alert_already_sent, connect, init_db, mark_alert_sent
from core.graph import _canon_args, _prior_tool_results, _reflection_note, _trim, build_graph
from core.router import Route, resolve_profile


def test_policies() -> None:
    assert approval.requires_approval("gmail_send_email")
    assert approval.requires_approval("run_python")
    assert approval.requires_approval("totally_unknown_tool")  # default deny
    assert not approval.requires_approval("list_directory")
    assert not approval.requires_approval("remember")
    assert not approval.requires_approval("recall_memories")
    # fitness: proposals pause, reads and the log append run free
    assert approval.requires_approval("save_workout_plan")
    assert approval.requires_approval("update_fitness_profile")
    assert not approval.requires_approval("get_todays_workout")
    assert not approval.requires_approval("record_workout")
    # diet: same posture — proposals pause, reads run free
    assert approval.requires_approval("save_diet_plan")
    assert approval.requires_approval("update_diet_profile")
    assert not approval.requires_approval("get_diet_plan")
    assert not approval.requires_approval("get_recent_diet_plans")
    # money moves through the catalog: browsing is free, spending always pauses
    assert not approval.requires_approval("search_restaurants")
    assert not approval.requires_approval("get_menu")
    assert approval.requires_approval("add_to_cart")
    assert approval.requires_approval("place_order")       # 'place_' pattern
    assert approval.requires_approval("confirm_order")     # default-deny
    assert approval.requires_approval("checkout")
    assert approval.requires_approval("book_table")
    assert approval.requires_approval("make_payment")
    # destructive resets must pause (forget via default-deny, delete_ via pattern)
    assert approval.requires_approval("forget")
    assert approval.requires_approval("delete_fitness_data")
    assert approval.requires_approval("delete_diet_data")
    print("policies          OK")


def test_db_vec_alerts() -> None:
    init_db()
    conn = connect()
    try:
        dim = settings.MODELS["embeddings"]["dim"]
        vec = struct.pack(f"{dim}f", *([0.1] * dim))
        conn.execute("INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)", (999999, vec))
        row = conn.execute(
            "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k = 1", (vec,)
        ).fetchone()
        assert row and row[0] == 999999
        conn.execute("DELETE FROM vec_memories WHERE rowid = 999999")
        conn.execute("DELETE FROM alerts_sent WHERE item_key = 'smoke:x'")  # rerunnable
        conn.commit()
    finally:
        conn.close()
    assert not alert_already_sent("smoke:x")
    mark_alert_sent("smoke:x")
    assert alert_already_sent("smoke:x")
    print("db + vec + alerts OK")


def test_graphs_build() -> None:
    assert build_graph(MemorySaver(), tools=[]) is not None
    assert build_graph(None, tools=[], autonomous=True) is not None
    print("graphs compile    OK")


def test_trim_and_reflection() -> None:
    msgs = []
    for i in range(60):
        msgs.append(HumanMessage(f"q{i}"))
        msgs.append(AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}", "type": "tool_call"}]))
        msgs.append(ToolMessage("r", tool_call_id=f"c{i}", name="t"))
        msgs.append(AIMessage(f"a{i}"))
    assert isinstance(_trim(msgs)[0], HumanMessage)

    err_msgs = [
        HumanMessage("do thing"),
        AIMessage("", tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}]),
        ToolMessage("ERROR running x: boom", tool_call_id="1", name="x"),
        AIMessage("", tool_calls=[{"name": "x", "args": {}, "id": "2", "type": "tool_call"}]),
        ToolMessage("ERROR running x: boom again", tool_call_id="2", name="x"),
    ]
    assert "Reflection required" in _reflection_note(err_msgs)
    assert _reflection_note(msgs) == ""
    print("trim + reflection OK")


def test_keyword_prerouter() -> None:
    from core.router import keyword_intent, skip_classify

    assert keyword_intent("make me a workout plan, I want to build muscle") == "fitness"
    assert keyword_intent("any important emails today?") == "email"
    assert keyword_intent("is there any meeting conflict on friday afternoon?") == "calendar"
    assert keyword_intent("summarize PR #42 in the friday repo") == "code"
    assert keyword_intent("make me a diet plan for cutting") == "diet"
    assert keyword_intent("how many calories in two rotis?") == "diet"
    assert keyword_intent("email me my workout plan") is None       # two domains collide
    assert keyword_intent("plan my meals around tomorrow's workout") is None  # diet+fitness collide
    assert keyword_intent("what do you think about the new office policy") is None
    assert keyword_intent("benchmark this function for me") is None  # word boundary, not substring
    assert keyword_intent("") is None
    # precision sweep: the pre-router must never contradict the eval's expected intent
    cases = json.loads((Path(__file__).parent / "routing_cases.json").read_text())
    for case in cases:
        got = keyword_intent(case["text"])
        assert got in (None, case["intent"]), f"prerouter misroutes {case['text']!r} -> {got}"

    assert skip_classify("yes", "gym")
    assert skip_classify("??", "gym")
    assert not skip_classify("yes", "general")                  # no specialist to stick to
    assert not skip_classify("check my email", "gym")           # keyword present
    assert not skip_classify("compare litellm vs openrouter for me", "gym")  # too long
    print("keyword prerouter OK")


def test_resolve_profile() -> None:
    low = Route(intent="chat", confidence=0.3)
    assert resolve_profile(low, "gym", "yes") == "gym"            # weak signal -> continuity
    assert resolve_profile(None, "gym", "??") == "gym"            # classifier failure -> continuity
    assert resolve_profile(low, "", "yes") == "general"           # nothing to stick to
    assert resolve_profile(None, "general", "hm") == "general"    # general never sticks
    # a confident specialist intent always switches
    assert resolve_profile(Route(intent="email", confidence=0.95), "gym", "any mail from Priya?") == "email"
    assert resolve_profile(Route(intent="fitness", confidence=0.9), "email", "plan my workouts") == "gym"
    # confident general-mapped intent: short follow-up stays in the flow, long message moves on
    chat = Route(intent="chat", confidence=0.9)
    assert resolve_profile(chat, "gym", "yes") == "gym"
    assert resolve_profile(Route(intent="task", confidence=0.9), "gym", "ok do that") == "gym"
    assert resolve_profile(chat, "gym", "what do you think about the new office policy then?") == "general"
    print("resolve_profile   OK")


def test_duplicate_tool_detection() -> None:
    def ai_call(cid: str, name: str, args: dict):
        return AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])

    msgs = [
        HumanMessage("earlier turn"),
        ai_call("c0", "get_fitness_profile", {}),
        ToolMessage("stale", tool_call_id="c0", name="get_fitness_profile"),
        AIMessage("done"),
        HumanMessage("plan please"),
        ai_call("c1", "get_workout_plan", {}),
        ToolMessage("(no workout plan yet) …", tool_call_id="c1", name="get_workout_plan"),
        ai_call("c2", "save_workout_plan", {"content": "# Plan"}),
        ToolMessage("Workout plan saved (1 lines).", tool_call_id="c2", name="save_workout_plan"),
        ai_call("c3", "get_workout_plan", {}),
        ToolMessage("# Plan", tool_call_id="c3", name="get_workout_plan"),
    ]
    prior = _prior_tool_results(msgs)
    key = ("get_workout_plan", _canon_args({}))
    assert prior[key] == "# Plan"                                  # latest REAL result wins
    assert ("save_workout_plan", _canon_args({"content": "# Plan"})) in prior
    assert ("get_fitness_profile", _canon_args({})) not in prior   # earlier turns excluded
    # stubs never become comparison baselines
    msgs += [
        ai_call("c4", "get_workout_plan", {}),
        ToolMessage("UNCHANGED — identical to your earlier get_workout_plan result this turn…",
                    tool_call_id="c4", name="get_workout_plan"),
    ]
    assert _prior_tool_results(msgs)[key] == "# Plan"
    assert _canon_args({"b": 1, "a": 2}) == _canon_args({"a": 2, "b": 1})  # order-insensitive
    print("dup-call guard    OK")


def test_prompt_and_skills() -> None:
    text = prompts.build_system_prompt([], extras=["## Extra\nhello"])
    assert "Friday" in text and "## Extra" in text

    all_skills = skills.load_all()
    assert {s.name for s in all_skills} >= {
        "email_style", "daily_note_format", "fitness_intake", "gym_program_design",
        "diet_intake", "diet_day_plan", "food_ordering",
    }
    hit = skills.match("build me a workout plan for the week", "general")
    assert any(s.name == "gym_program_design" for s in hit)
    by_agent = skills.match("anything at all", "email")
    assert any(s.name == "email_style" for s in by_agent)
    gym_skills = {s.name for s in skills.match("anything at all", "gym")}
    assert {"fitness_intake", "gym_program_design"} <= gym_skills  # always with the gym agent
    assert any(s.name == "fitness_intake" for s in skills.match("i want to get fit", "general"))
    diet_skills = {s.name for s in skills.match("anything at all", "diet")}
    assert {"diet_intake", "diet_day_plan"} <= diet_skills  # always with the diet agent
    assert any(s.name == "diet_day_plan" for s in skills.match("what should i eat today", "general"))
    rendered = skills.render(hit)
    assert "Skill: gym_program_design" in rendered
    print("prompt + skills   OK")


def test_registry_filtering() -> None:
    fake = lambda name: SimpleNamespace(name=name, description="d")
    tools = [fake("gmail_search_emails"), fake("gmail_send_email"), fake("github_list_prs"),
             fake("remember"), fake("run_python")]
    email_tools = registry.filter_tools(registry.get("email"), tools)
    names = {t.name for t in email_tools}
    assert "gmail_send_email" in names and "github_list_prs" not in names
    assert "remember" in names  # ALWAYS_INCLUDE survives filtering
    # filter that matches nothing domain-specific -> falls back to all tools
    cal_tools = registry.filter_tools(registry.get("calendar"), tools)
    assert len(cal_tools) == len(tools)
    gym_tools = registry.filter_tools(
        registry.get("gym"), tools + [fake("save_workout_plan"), fake("get_fitness_profile")]
    )
    gym_names = {t.name for t in gym_tools}
    assert {"save_workout_plan", "get_fitness_profile"} <= gym_names
    assert "gmail_send_email" not in gym_names
    # diet agent sees its own tools AND the fitness reads it plans around
    diet_tools = registry.filter_tools(
        registry.get("diet"),
        tools + [fake("save_diet_plan"), fake("get_fitness_profile"), fake("get_todays_workout")],
    )
    diet_names = {t.name for t in diet_tools}
    assert {"save_diet_plan", "get_fitness_profile", "get_todays_workout"} <= diet_names
    assert "gmail_send_email" not in diet_names
    # …but tool_exclude keeps it out of the gym's writes despite the keyword match
    excluded = registry.filter_tools(
        registry.get("diet"), tools + [fake("save_workout_plan"), fake("update_fitness_profile")]
    )
    assert not {"save_workout_plan", "update_fitness_profile"} & {t.name for t in excluded}
    # exclusion survives the no-domain-tools fallback (which returns everything else)
    fallback = registry.filter_tools(registry.get("diet"), [fake("save_workout_plan"), fake("remember")])
    assert {t.name for t in fallback} == {"remember"}
    # catalog tools are claimed by SERVER, so vendor tool names don't matter
    def mcp_fake(name: str, server: str):
        return SimpleNamespace(name=name, description="d", metadata={"mcp_server": server})

    catalog = [mcp_fake("someVendorNameWeCannotPredict", "swiggy_food"),
               mcp_fake("im_product_lookup", "swiggy_instamart"),
               mcp_fake("read_file", "filesystem")]
    diet_catalog = {t.name for t in registry.filter_tools(registry.get("diet"), tools + catalog)}
    assert {"someVendorNameWeCannotPredict", "im_product_lookup"} <= diet_catalog
    assert "read_file" not in diet_catalog          # unclaimed server stays out
    # the gym agent gets no catalog access (it has domain tools here, so the
    # everything-back fallback is not in play — this tests the filter itself)
    gym_catalog = {
        t.name
        for t in registry.filter_tools(registry.get("gym"), catalog + [fake("get_todays_workout")])
    }
    assert gym_catalog == {"get_todays_workout"}
    print("registry filter   OK")


def test_memory_roundtrip() -> None:
    dim = settings.MODELS["embeddings"]["dim"]
    v1 = [0.5] * dim
    v2 = [0.5] * (dim - 1) + [0.51]   # nearly identical -> dedupe
    v3 = [-0.5] * dim                  # opposite -> distinct

    async def flow():
        r1 = await memory.add_memory("Prefers uv over pip for Python projects", "preference", _vec=v1)
        assert r1.startswith("Remembered")
        # near-identical VECTOR, different words -> embedding dedupe
        r2 = await memory.add_memory("Likes uv much more than pip for Python work", "preference", _vec=v2)
        assert r2.startswith("Already known")
        # same normalized TEXT, distinct vector -> exact-text dedupe (works offline too)
        r3 = await memory.add_memory("prefers UV over pip for Python projects.", "preference", _vec=v3)
        assert r3.startswith("Already known")
        await memory.add_memory("Working on Friday assistant project", "project", _vec=v3)
        hits = await memory.recall("python tooling preference", _vec=v1)
        assert any("uv over pip" in h for h in hits)
        gone = await memory.forget_matching("uv over pip")
        assert gone.startswith("Forgot")
        hits_after = await memory.recall("python tooling preference", _vec=v1)
        assert not any("uv over pip" in h for h in hits_after)

    asyncio.run(flow())
    md = (settings.MEMORY_DIR / "MEMORY.md").read_text(encoding="utf-8")
    assert "auto-generated" in md and "Friday assistant project" in md
    print("memory roundtrip  OK")


def test_forget_all() -> None:
    """forget('everything') wipes all memories — exercised against a TEMP db
    + temp mirror so running smoke on a real installation never nukes the
    owner's actual memories."""
    import tempfile

    orig_db, orig_mem = settings.DB_PATH, settings.MEMORY_DIR
    tmp = Path(tempfile.mkdtemp(prefix="friday-forgetall-smoke-"))
    settings.DB_PATH = tmp / "test.db"
    settings.MEMORY_DIR = tmp
    try:
        init_db()
        dim = settings.MODELS["embeddings"]["dim"]

        async def flow():
            await memory.add_memory("Smoke test fact one", "fact", _vec=[0.2] * dim)
            await memory.add_memory("Smoke test fact two", "fact", _vec=[-0.2] * dim)
            gone = await memory.forget_matching("Everything!")   # normalization too
            assert gone.startswith("Forgot ALL") and "2 erased" in gone
            # points at BOTH file resets (fitness + diet live outside the db)
            assert "delete_fitness_data" in gone and "delete_diet_data" in gone
            assert await memory.recall("smoke test fact", _vec=[0.2] * dim) == []

        asyncio.run(flow())
        assert "(none yet)" in (tmp / "MEMORY.md").read_text(encoding="utf-8")
    finally:
        settings.DB_PATH, settings.MEMORY_DIR = orig_db, orig_mem
    print("forget-all        OK")


def test_transient_filter() -> None:
    from core.memory import _looks_transient

    assert _looks_transient("User has not specified fitness goals or workout preferences yet.")
    assert _looks_transient("User wants to start a new workout plan")
    assert _looks_transient("User wants to start a new diet")
    assert _looks_transient("User needs to provide fitness profile information for workout plan creation")
    assert _looks_transient("User is asking for a summary of unread email")
    # durable facts these arms must NOT kill
    assert not _looks_transient("User wants to start training for a marathon")
    assert not _looks_transient("User has not eaten meat since 2019")
    assert not _looks_transient("User is vegetarian and allergic to peanuts")
    assert not _looks_transient("User is starting a new job at Google in September")
    assert not _looks_transient("User's manager is Sandeep")
    print("transient filter  OK")


def test_render_preview() -> None:
    plan = "# Weekly Plan\n" + "\n".join(f"- line {i}" for i in range(500))
    out = approval.render_preview([{"name": "save_workout_plan", "args": {"content": plan}}])
    assert "save_workout_plan · content" in out
    assert "\n- line 1\n" in out          # real newlines — not json-escaped
    assert "\\n" not in out
    assert "chars total" in out and "saved on approve" in out
    assert len(out) < 4000                # content limit 3500 + header, Telegram-safe
    mail = approval.render_preview(
        [{"name": "gmail_send_email", "args": {"to": "x@y.z", "body": "hello\nworld " * 300}}]
    )
    assert mail.startswith("1. gmail_send_email")
    assert "chars total" in mail and len(mail) < 1500   # multi-arg stays JSON at 1200
    # content tool with a short scalar extra: still plain text, extra in the header
    day = "## Breakfast — 8:30\n- eggs 3 — ~210 kcal, 19 g protein\n" * 40
    out = approval.render_preview(
        [{"name": "save_diet_plan", "args": {"content": day, "date": "2026-08-11"}}]
    )
    assert "save_diet_plan · content" in out and "date='2026-08-11'" in out
    assert "\\n" not in out and "## Breakfast" in out
    # empty extras stay out of the header
    out2 = approval.render_preview([{"name": "save_diet_plan", "args": {"content": day, "date": ""}}])
    assert "date=" not in out2
    print("render preview    OK")


def test_fitness_roundtrip() -> None:
    import tempfile
    from pathlib import Path

    from core import fitness

    tmp = Path(tempfile.mkdtemp(prefix="friday-fitness-smoke-"))
    fitness.FITNESS_DIR = tmp
    fitness.PROFILE_PATH = tmp / "PROFILE.md"
    fitness.PLAN_PATH = tmp / "PLAN.md"
    fitness.LOG_PATH = tmp / "LOG.md"
    fitness.HISTORY_DIR = tmp / "history"

    assert fitness.get_fitness_profile.invoke({}) == fitness.NO_PROFILE
    assert fitness.get_workout_plan.invoke({}) == fitness.NO_PLAN_NO_PROFILE
    fitness.update_fitness_profile.invoke({"content": "# Profile\n- goal: hypertrophy\n- level: beginner"})
    assert "hypertrophy" in fitness.get_fitness_profile.invoke({})
    # profile now exists but no plan -> sentinel must direct the agent to design+save
    assert fitness.get_workout_plan.invoke({}) == fitness.NO_PLAN_PROFILE_READY
    assert "save_workout_plan" in fitness.get_workout_plan.invoke({})

    plan = "\n".join(
        ["# Week — Upper/Lower"]
        + [
            f"## {d} — {'Rest' if d in ('Wednesday', 'Saturday', 'Sunday') else 'Training'}\n"
            f"### Session\n- Squat 3x5 @ RIR 2, rest 3 min"
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        ]
    )
    result = fitness.save_workout_plan.invoke({"content": plan})
    assert "WARNING" not in result
    partial = "# Week\n## Monday — Full body\n- Squat 3x5"
    assert "Sunday" in fitness.save_workout_plan.invoke({"content": partial})  # warns re missing days
    assert (tmp / "history").exists() and len(list((tmp / "history").glob("plan-*.md"))) == 1

    fitness.save_workout_plan.invoke({"content": plan})
    monday = datetime(2026, 8, 10, 7, 0, tzinfo=ZoneInfo(settings.TIMEZONE))  # a Monday
    weekday, section = fitness.todays_session(monday)
    assert weekday == "Monday" and section is not None
    assert section.startswith("## Monday") and "Squat 3x5" in section and "## Tuesday" not in section

    assert fitness.record_workout.invoke({"entry": "bench 4x8 @ 50kg, felt good"}).startswith("Logged")
    assert "bench 4x8" in fitness.get_workout_log.invoke({"days": 7})
    assert fitness.get_workout_log.invoke({"days": 1})  # today is inside every window

    # full reset: archives everything, deletes, and directs a fresh intake
    result = fitness.delete_fitness_data.invoke({})
    assert "PROFILE.md" in result and "PLAN.md" in result and "LOG.md" in result
    assert "intake" in result
    assert list((tmp / "history").glob("profile-*.md")) and list((tmp / "history").glob("log-*.md"))
    assert fitness.get_fitness_profile.invoke({}) == fitness.NO_PROFILE
    assert fitness.delete_fitness_data.invoke({}) == "(no fitness data to delete)"
    print("fitness roundtrip OK")


def test_nutrition_roundtrip() -> None:
    import tempfile
    from pathlib import Path

    from core import nutrition

    tmp = Path(tempfile.mkdtemp(prefix="friday-nutrition-smoke-"))
    nutrition.NUTRITION_DIR = tmp
    nutrition.PROFILE_PATH = tmp / "PROFILE.md"
    nutrition.DAYS_DIR = tmp / "days"
    nutrition.HISTORY_DIR = tmp / "history"

    assert nutrition.get_diet_profile.invoke({}) == nutrition.NO_DIET_PROFILE
    # no profile + no day file -> intake sentinel for that date
    out = nutrition.get_diet_plan.invoke({})
    assert "no diet profile" in out and "diet_intake" in out
    # junk dates are refused, never guessed — they become filenames
    assert nutrition.get_diet_plan.invoke({"date": "next tuesday"}).startswith("ERROR")
    assert nutrition.save_diet_plan.invoke({"content": "# x", "date": "2026-13-40"}).startswith("ERROR")

    nutrition.update_diet_profile.invoke(
        {"content": "# Diet profile\n- pattern: vegetarian + eggs\n- targets: 2200 kcal, 130 g protein"}
    )
    assert "vegetarian" in nutrition.get_diet_profile.invoke({})
    # profile exists but no plan for the day -> design sentinel gates the save
    out = nutrition.get_diet_plan.invoke({"date": "today"})
    assert "diet profile EXISTS" in out and "save_diet_plan" in out and "diet_day_plan" in out

    day = (
        "# Monday — training day\n"
        "## Breakfast — 8:30\n- poha 1 katori + 2 eggs — ~350 kcal, 16 g protein\n"
        "## Lunch — 13:30\n- dal 2 katori, rice, curd — ~600 kcal, 28 g protein\n"
        "## Dinner — 21:00\n- paneer 100 g + 2 rotis — ~550 kcal, 26 g protein\n"
        "## Totals\n- ~2200 kcal · 130 g protein / target 130 g"
    )
    result = nutrition.save_diet_plan.invoke({"content": day})
    assert "saved" in result and "WARNING" not in result
    assert "poha" in nutrition.get_diet_plan.invoke({})
    date_key, plan = nutrition.todays_plan()
    assert plan is not None and "Breakfast" in plan
    # re-saving the same day archives the previous version
    nutrition.save_diet_plan.invoke({"content": day + "\n- note: swap curd for raita"})
    assert len(list((tmp / "history").glob(f"{date_key}-*.md"))) == 1
    # a structureless plan warns (the alert sends the file as-is)
    assert "WARNING" in nutrition.save_diet_plan.invoke({"content": "- just a list", "date": "tomorrow"})

    recent = nutrition.get_recent_diet_plans.invoke({"days": 3})
    assert date_key in recent and "poha" in recent

    # full reset: archives profile + every day file, then directs a fresh intake
    result = nutrition.delete_diet_data.invoke({})
    assert "PROFILE.md" in result and f"{date_key}.md" in result and "intake" in result
    assert list((tmp / "history").glob("profile-*.md"))
    assert nutrition.get_diet_profile.invoke({}) == nutrition.NO_DIET_PROFILE
    assert nutrition.delete_diet_data.invoke({}) == "(no diet data to delete)"
    print("diet roundtrip    OK")


def test_mcp_oauth_store() -> None:
    """Token storage round-trip + the boot-time skip for un-authorized servers,
    against a TEMP dir so a real installation's tokens are never touched."""
    import tempfile
    from pathlib import Path

    from mcp.shared.auth import OAuthToken

    from core import mcp_auth, tools as core_tools

    orig = mcp_auth.OAUTH_DIR
    mcp_auth.OAUTH_DIR = Path(tempfile.mkdtemp(prefix="friday-oauth-smoke-"))
    try:
        assert not mcp_auth.is_authorized("swiggy_food")     # nothing stored yet
        store = mcp_auth.FileTokenStorage("swiggy_food")

        async def flow():
            assert await store.get_tokens() is None
            await store.set_tokens(OAuthToken(access_token="tok", token_type="Bearer",
                                              refresh_token="ref", expires_in=3600))
            got = await store.get_tokens()
            assert got and got.access_token == "tok" and got.refresh_token == "ref"

        asyncio.run(flow())
        assert mcp_auth.is_authorized("swiggy_food")
        assert oct(store.path.stat().st_mode)[-3:] == "600"  # credential material

        # an oauth server is SKIPPED (not errored) until it has been logged in
        conf = {"transport": "streamable_http", "url": "https://mcp.swiggy.com/im", "oauth": True}
        assert core_tools._prepare("swiggy_instamart", conf) is None
        prepared = core_tools._prepare("swiggy_food", conf)
        assert prepared is not None and "auth" in prepared and "oauth" not in prepared
        assert conf["oauth"] is True                          # loaded config not mutated
        # non-oauth servers pass through untouched
        plain = {"transport": "stdio", "command": "npx"}
        assert core_tools._prepare("filesystem", plain) == plain
    finally:
        mcp_auth.OAUTH_DIR = orig
    print("mcp oauth store   OK")


def test_mcp_config() -> None:
    """The Swiggy servers are wired the way the vendor manifest specifies."""
    servers = settings.MCP["servers"]
    for name, url in (("swiggy_food", "https://mcp.swiggy.com/food"),
                      ("_swiggy_instamart", "https://mcp.swiggy.com/im"),
                      ("_swiggy_dineout", "https://mcp.swiggy.com/dineout")):
        conf = servers[name]
        assert conf["url"] == url and conf["oauth"] is True
        assert conf["transport"] == "streamable_http"   # what the adapter understands
    # the diet agent claims them by server name, underscore-free
    claimed = registry.get("diet").tool_servers
    assert {"swiggy_food", "swiggy_instamart", "swiggy_dineout"} == set(claimed)
    print("mcp config        OK")


def test_heartbeat_parse_and_notify() -> None:
    text = 'Here you go:\n[{"key": "pr:friday#1", "message": "PR #1 awaits your review"}]'
    items = _parse_findings(text)
    assert items and items[0]["key"] == "pr:friday#1"
    assert _parse_findings("nothing actionable") == []
    assert _parse_findings("[]") == []

    tz = ZoneInfo(settings.TIMEZONE)
    assert notify.in_quiet_hours(datetime(2026, 6, 6, 23, 30, tzinfo=tz))
    assert notify.in_quiet_hours(datetime(2026, 6, 6, 6, 0, tzinfo=tz))
    assert not notify.in_quiet_hours(datetime(2026, 6, 6, 12, 0, tzinfo=tz))
    print("heartbeat+notify  OK")


if __name__ == "__main__":
    test_policies()
    test_db_vec_alerts()
    test_graphs_build()
    test_trim_and_reflection()
    test_keyword_prerouter()
    test_resolve_profile()
    test_duplicate_tool_detection()
    test_prompt_and_skills()
    test_registry_filtering()
    test_memory_roundtrip()
    test_forget_all()
    test_transient_filter()
    test_render_preview()
    test_fitness_roundtrip()
    test_nutrition_roundtrip()
    test_mcp_oauth_store()
    test_mcp_config()
    test_heartbeat_parse_and_notify()
    print("\nall smoke tests passed ✔")

"""Turn-flow tests for the Discord RPG engine, using fake Discord, model and database objects."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cogs.rpg_system import engine as engine_mod
from cogs.rpg_system.engine import RPGEngine

THREAD_ID = 4242
ALICE, BOB = 111111111111111111, 222222222222222222


def make_session(total_turns=3):
    return {
        "thread_id": THREAD_ID, "owner_id": ALICE, "total_turns": total_turns, "story_mode": False,
        "player_stats": {
            str(ALICE): {"name": "Alice", "hp": 80, "max_hp": 100, "mp": 20, "max_mp": 50},
            str(BOB): {"name": "Bob", "hp": 35, "max_hp": 100, "mp": 50, "max_mp": 50},
        },
        "turn_history": [],
    }


class FakeMessage:
    id = 1

    async def edit(self, **kwargs):
        pass

    async def delete(self):
        pass


class FakeChannel:
    id = THREAD_ID

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return FakeMessage()

    @asynccontextmanager
    async def typing(self):
        yield


def tool_call(name, args, call_id="call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def model_reply(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)

    def model_dump(exclude_none=True):
        d = {"role": "assistant", "content": content}
        if tool_calls:
            d["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in tool_calls]
        return {k: v for k, v in d.items() if v is not None} if exclude_none else d

    msg.model_dump = model_dump
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def rig(monkeypatch):
    """An engine wired to fakes. Returns (engine, state) where state records what the model saw."""
    state = {"session": make_session(), "world_version": 0, "calls": [], "replies": []}

    class FakeSessions:
        def find_one(self, query, *args, **kwargs):
            return state["session"]

    class FakeWorld:
        def find_one(self, query, *args, **kwargs):
            return {"locations": {}, "quests": {}, "environment": {"time": "10:00"}}

    async def create(**kwargs):
        state["calls"].append(json.loads(json.dumps(kwargs["messages"], default=str)))
        return state["replies"].pop(0)

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def fake_throttled(fn):
        return await (fn() if callable(fn) else fn)

    monkeypatch.setattr(engine_mod, "rpg_sessions_collection", FakeSessions())
    monkeypatch.setattr(engine_mod, "rpg_world_state_collection", FakeWorld())
    monkeypatch.setattr(engine_mod, "get_client", lambda: fake_client)
    monkeypatch.setattr(engine_mod, "throttled_create", fake_throttled)

    async def build_context_block(session_db, user_input, logger=None):
        return f"WORLD SHEET v{state['world_version']}", {"world_entities": {"active_npcs": []}}

    async def archive_old_turns(thread_id, session_db):
        return False

    async def snapshot(thread_id, turn_id):
        pass

    memory = SimpleNamespace(
        build_context_block=build_context_block,
        archive_old_turns=archive_old_turns,
        save_turn=lambda *a, **k: None,
        snapshot_world_state=snapshot,
    )
    eng = RPGEngine(bot=SimpleNamespace(loop=SimpleNamespace(create_task=lambda coro: coro.close())),
                    memory_manager=memory)

    async def fake_send_narrative(*args, **kwargs):
        return [99]

    monkeypatch.setattr(eng, "_send_narrative", fake_send_narrative)
    return eng, state


def narrative(text):
    return f"<narrative>{text}</narrative><suggested_actions>- Look around</suggested_actions>"


def test_context_is_rebuilt_every_turn(rig):
    eng, state = rig
    ch = FakeChannel()
    state["replies"] = [model_reply(narrative("Turn one."))]
    asyncio.run(eng.process_turn(ch, "Alice: open the door", user=SimpleNamespace(id=ALICE, name="Alice")))

    state["world_version"] = 1  # e.g. the Scribe added an NPC after turn one
    state["replies"] = [model_reply(narrative("Turn two."))]
    asyncio.run(eng.process_turn(ch, "Alice: talk to the guard", user=SimpleNamespace(id=ALICE, name="Alice")))

    first, second = state["calls"]
    assert "WORLD SHEET v0" in first[0]["content"]
    assert "WORLD SHEET v1" in second[0]["content"]  # the new world state reaches the model
    # Only this turn's messages are sent; earlier turns live in the system context.
    assert [m["role"] for m in second] == ["system", "user"]


def test_tool_messages_stay_paired_and_do_not_leak_into_next_turn(rig):
    eng, state = rig
    ch = FakeChannel()
    state["replies"] = [
        model_reply(tool_calls=[tool_call("roll_d20", {"check_type": "Stealth", "difficulty": 12})]),
        model_reply(narrative("You slip past.")),
    ]
    asyncio.run(eng.process_turn(ch, "Alice: sneak past", user=SimpleNamespace(id=ALICE, name="Alice")))

    roles = [m["role"] for m in state["calls"][1]]
    assert roles == ["system", "user", "assistant", "tool"]  # tool result right after its call

    state["replies"] = [model_reply(narrative("Next scene."))]
    asyncio.run(eng.process_turn(ch, "Alice: keep going", user=SimpleNamespace(id=ALICE, name="Alice")))
    assert [m["role"] for m in state["calls"][2]] == ["system", "user"]


def test_status_lists_every_player_with_ids(rig):
    eng, state = rig
    state["replies"] = [model_reply(narrative("Bob acts."))]
    asyncio.run(eng.process_turn(FakeChannel(), "Bob: heal", user=SimpleNamespace(id=BOB, name="Bob")))

    user_prompt = state["calls"][0][1]["content"]
    lines = [line for line in user_prompt.splitlines() if line.startswith(("[ACTING", "[PARTY"))]
    assert lines[0] == f"[ACTING: Bob (user_id {BOB}) - HP 35/100 | MP 50/50]"
    assert lines[1] == f"[PARTY: Alice (user_id {ALICE}) - HP 80/100 | MP 20/50]"


def test_simultaneous_turns_run_one_at_a_time(rig, monkeypatch):
    eng, _ = rig
    events = []

    async def fake_locked(channel, prompt, user, is_reroll, message_id):
        events.append(("start", prompt))
        await asyncio.sleep(0.01)
        events.append(("end", prompt))

    monkeypatch.setattr(eng, "_process_turn_locked", fake_locked)

    async def both():
        ch = FakeChannel()
        await asyncio.gather(eng.process_turn(ch, "A"), eng.process_turn(ch, "B"))

    asyncio.run(both())
    assert events == [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")]


def test_queued_turn_reads_fresh_turn_number(rig):
    """A turn that waited for the lock must use the turn number the previous turn saved."""
    eng, state = rig

    def save_turn(*args, current_turn_id=None, **kwargs):
        state["session"]["total_turns"] = current_turn_id

    eng.memory_manager.save_turn = save_turn
    state["replies"] = [model_reply(narrative("One.")), model_reply(narrative("Two."))]

    async def both():
        ch = FakeChannel()
        await asyncio.gather(
            eng.process_turn(ch, "Alice: a", user=SimpleNamespace(id=ALICE, name="Alice")),
            eng.process_turn(ch, "Bob: b", user=SimpleNamespace(id=BOB, name="Bob")),
        )

    asyncio.run(both())
    turn_ids = [c[1]["content"].split("Turn ")[1].split(" ")[0] for c in state["calls"]]
    assert turn_ids == ["4", "5"]

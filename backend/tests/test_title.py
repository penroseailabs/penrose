"""Naming a chat: once Relay starts to answer its first message, the chat is named from that message."""

import asyncio
import json

from fakes import Script, make_state, runner_for

ANSWER = {"actions": [{"tool": "answer", "text": "Sure."}]}
REQUEST = "Hey, could you please order my usual weekly groceries from Instacart for Friday?"


def titled(name="Weekly groceries from Instacart"):
    return lambda context, n: {"actions": [{"tool": "set_title", "title": name}]}


def first_message(store, request=REQUEST):
    """A new chat as delivery.new_run leaves it: the start of the message stands in for a title."""
    store.message("chat", "user", request)
    state = make_state(store, request=request, route=None)
    store.execute("UPDATE chats SET title=? WHERE id='chat'", (request[:55],))
    return state


async def settled(runner):
    await asyncio.gather(*runner.background)


def title_of(store):
    return store.rows("SELECT title FROM chats WHERE id='chat'")[0]["title"]


async def test_the_first_reply_names_the_chat_from_the_message(store):
    state = first_message(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Title": titled()})
    runner = runner_for(store, model)
    await runner.run(state)
    await settled(runner)

    seen = model.of("Title")
    assert len(seen) == 1 and seen[0]["context"]["request"] == REQUEST and seen[0]["context"]["reply"] == "Sure."
    assert title_of(store) == "Weekly groceries from Instacart"
    event = store.rows("SELECT payload FROM events WHERE kind='chat_titled'")
    assert json.loads(event[0]["payload"]) == {"title": "Weekly groceries from Instacart"}


async def test_the_name_is_tidied(store):
    state = first_message(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Title": titled('  "Weekly   groceries."\n')})
    runner = runner_for(store, model)
    await runner.run(state)
    await settled(runner)

    assert title_of(store) == "Weekly groceries"


async def test_a_chat_is_named_once(store):
    state = first_message(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Title": titled()})
    runner = runner_for(store, model)
    await runner.run(state)
    await settled(runner)
    store.message("chat", "user", "And for Saturday?")
    follow_up = make_state(store, request="And for Saturday?", route=None)
    await runner.run(follow_up)
    await settled(runner)

    assert len(model.of("Title")) == 1


async def test_a_title_that_is_not_the_stand_in_is_left_alone(store):
    state = first_message(store)
    store.execute("UPDATE chats SET title='Instacart: Your order is on its way' WHERE id='chat'")
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Title": titled()})
    runner = runner_for(store, model)
    await runner.run(state)
    await settled(runner)

    assert model.of("Title") == [] and title_of(store) == "Instacart: Your order is on its way"


async def test_a_title_changed_while_the_agent_worked_is_kept(store):
    state = first_message(store)

    def renamed_meanwhile(context, n):
        store.execute("UPDATE chats SET title='My own name' WHERE id='chat'")
        return {"actions": [{"tool": "set_title", "title": "Weekly groceries"}]}

    runner = runner_for(store, Script(**{"Fast response": lambda c, n: ANSWER, "Title": renamed_meanwhile}))
    await runner.run(state)
    await settled(runner)

    assert title_of(store) == "My own name"


async def test_a_failing_title_agent_leaves_the_stand_in_and_the_run_alone(store):
    def broken(context, n):
        raise RuntimeError("the provider is down")

    state = first_message(store)
    runner = runner_for(store, Script(**{"Fast response": lambda c, n: ANSWER, "Title": broken}))
    await runner.run(state)
    await settled(runner)

    assert state["status"] == "completed" and title_of(store) == REQUEST[:55]

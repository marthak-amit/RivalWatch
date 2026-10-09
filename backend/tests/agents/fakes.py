import itertools

from langchain_core.messages import AIMessage

_ids = itertools.count(1)


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}])


def done(text="DONE: finished"):
    return AIMessage(content=text)


class ScriptedResearcher:
    """Plays back AI messages in order; an exception instance in the script is raised instead."""

    def __init__(self, *script):
        self.script, self.calls, self.tools = list(script), [], None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        item = self.script.pop(0) if self.script else done("DONE: script ended")
        if isinstance(item, Exception):
            raise item
        return item


class FakeSummarizer:
    """Stands in for `llm.with_structured_output(RunCopy)`. reply: RunCopy, a callable(prompt), or an exception."""

    def __init__(self, reply=None):
        self.reply, self.calls = reply, []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.calls.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(prompt) if callable(self.reply) else self.reply

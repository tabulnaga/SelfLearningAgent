import json

from pydantic import BaseModel

from app.models import Principal, ProposedTool, ToolRecord
from app.security.authorizer import Authorizer
from app.security.redaction import redact
from app.tools.registry import Tool, ToolError, ToolPolicy, ToolRegistry


class Args(BaseModel):
    n: int


async def ok(args: Args):
    return {"n": args.n}


async def boom(args: Args):
    raise ToolError("downstream failed")


def registry(write_allowed: bool) -> ToolRegistry:
    return ToolRegistry([Tool("read_it", "r", Args, "read", ok), Tool("write_it", "w", Args, "write", ok),
                         Tool("fail_it", "f", Args, "read", boom)], ToolPolicy(write_allowed))


async def run(reg, name, args):
    seq = iter(range(1, 100))

    async def record(**kw):
        return ToolRecord(tool_exec_id=next(seq), **kw)

    return await reg.execute(ProposedTool(name=name, arguments_json=json.dumps(args)), record)


async def test_tool_outcomes_are_honest():
    reg = registry(write_allowed=False)
    assert (await run(reg, "read_it", {"n": 1})).status == "success"
    assert (await run(reg, "write_it", {"n": 1})).status == "denied"       # policy, not the LLM, decides
    assert (await run(reg, "fail_it", {"n": 1})).status == "error"         # failure is never success
    assert (await run(reg, "read_it", {"n": "x"})).status == "error"       # schema validated
    assert (await run(reg, "nope", {})).status == "error"
    assert (await run(registry(True), "write_it", {"n": 1})).status == "success"


def test_authorizer_scopes_by_role():
    a = Authorizer("bank")
    user = a.retrieval_scope(Principal(subject="u", tenant_id="t1", roles=["agent_user"]))
    assert "confidential" not in user.classifications and user.tenant_id == "t1"
    admin = a.retrieval_scope(Principal(subject="a", tenant_id="t1", roles=["admin"]))
    assert "confidential" in admin.classifications
    ms = a.memory_scope(Principal(subject="u", tenant_id="t1", roles=[]), "payments")
    assert ms.tags == ["tenant:t1", "domain:payments"]


def test_redaction():
    s = redact("mail a.b@x.com card 4111 1111 1111 1111 call +44 20 7946 0958 key sk-abcdef123456")
    assert "@" not in s and "4111" not in s and "7946" not in s and "sk-abc" not in s

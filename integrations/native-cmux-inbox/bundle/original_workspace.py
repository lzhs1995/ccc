import json,uuid
from fixture_common import need

def walk(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from walk(child)
    elif isinstance(value,list):
        for child in value:yield from walk(child)


def workspace_from_reply(reply,tree,before):
    value=json.loads(reply.stdout);value=value.get("result",value)
    candidates=[r.get("workspace_id") or r.get("id") for r in walk(value)
                if r.get("workspace_id") or r.get("id") and r.get("type")=="workspace"]
    candidates=[str(uuid.UUID(s)).upper() for s in candidates if isinstance(s,str)]
    candidates=list(dict.fromkeys(candidates))
    if not candidates:
        candidates=[str(uuid.UUID(w["id"])).upper() for win in tree.get("windows",[]) for w in win.get("workspaces",[])
                    if str(uuid.UUID(w["id"])).upper() not in before]
    need(len(candidates)==1 and candidates[0] not in before,"one actual newly created workspace not established")
    return candidates[0]



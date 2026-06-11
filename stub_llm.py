import json, time, uuid
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
app = FastAPI()
def sse(o): return "data: " + json.dumps(o) + "\n\n"
@app.get("/healthz")
async def hz(): return {"ok": True}
@app.get("/v1/models")
async def models(): return {"object":"list","data":[{"id":"stub-model","object":"model"}]}

@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    cid = uuid.uuid4().hex[:8]; model = body.get("model","stub")
    msgs = body.get("messages", [])
    open("/tmp/offered_tools.txt","w").write(json.dumps([ (t.get("function",{}) or {}).get("name") for t in body.get("tools",[]) ]))
    has_tool_result = any(m.get("role") == "tool" for m in msgs)

    def head():
        return sse({"id":cid,"object":"chat.completion.chunk","created":int(time.time()),"model":model,"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":None}]})
    def done(fr):
        yield sse({"id":cid,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{},"finish_reason":fr}]})
        yield "data: [DONE]\n\n"

    if not has_tool_result:
        # find a code/terminal tool in the offered tools, emit a tool_call for it
        target = None
        for t in body.get("tools", []):
            fn = t.get("function", {}); n = fn.get("name","").lower()
            if any(k in n for k in ("terminal","bash","shell","exec","code","run")):
                target = fn; break
        if target is None and body.get("tools"):
            target = body["tools"][0].get("function", {})
        if target:
            props = (target.get("parameters",{}) or {}).get("properties",{}) or {}
            if "command" in props: args = {"command":"echo 42; python3 -c 'print(6*7)'"}
            elif "cmd" in props: args = {"cmd":"echo 42; python3 -c 'print(6*7)'"}
            elif "code" in props: args = {"code":"print(6*7)"}
            else:
                args = {}
                for k,v in props.items():
                    if (v or {}).get("type")=="string": args[k]="echo 42"; break
            with open("/tmp/stub_pick.txt","w") as f: f.write(f"{target.get('name')} {json.dumps(args)}")
            def gen():
                yield head()
                yield sse({"id":cid,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_"+cid,"type":"function","function":{"name":target["name"],"arguments":json.dumps(args)}}]},"finish_reason":None}]})
                yield from done("tool_calls")
            return StreamingResponse(gen(), media_type="text/event-stream")
    # follow-up (or no tool): final answer
    text = "I ran the analysis in the sandbox; the result is 42 and 42."
    def gen2():
        yield head()
        for w in text.split(" "):
            yield sse({"id":cid,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{"content":w+" "},"finish_reason":None}]})
        yield from done("stop")
    return StreamingResponse(gen2(), media_type="text/event-stream")

"""JSON-lines execution kernel. Business services and data access live in Go.

Code executes with the local user's permissions, like a local notebook. The Go
supervisor enforces a 30-second deadline and resets a failed kernel.
"""
import ast
import contextlib
import io
import json
import sys
import traceback

namespace = {"__name__": "__notebook__"}
wire = sys.stdout

def rpc(method, **args):
    wire.write(json.dumps({"rpc": method, **args}, ensure_ascii=False) + "\n")
    wire.flush()
    response = json.loads(sys.stdin.readline())
    if "error" in response:
        raise RuntimeError(response["error"])
    return response

def query(sql, limit=200):
    return rpc("query", sql=sql, limit=limit)

namespace.update({"query": query, "sql": query,
                  "tables": lambda: rpc("tables")["tables"],
                  "describe": lambda table: rpc("describe", table=table)["columns"]})
import os
sys.path.insert(0, os.getcwd())

for line in sys.stdin:
    output = io.StringIO()
    try:
        request = json.loads(line)
        namespace["params"] = request.get("params", {})
        source = request.get("source", "")
        result = None
        namespace.pop("result", None)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            tree = ast.parse(source, mode="exec")
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                expr = tree.body.pop()
                exec(compile(tree, "<notebook>", "exec"), namespace)
                result = eval(compile(ast.Expression(expr.value), "<notebook>", "eval"), namespace)
            else:
                exec(compile(tree, "<notebook>", "exec"), namespace)
        if result is None and "result" in namespace:
            result = namespace["result"]
        response = {"status": "success", "stdout": output.getvalue()[:1000000], "result": result,
                    "variables": {k: repr(v)[:200] for k, v in namespace.items() if not k.startswith("_")}}
        try:
            encoded = json.dumps(response, ensure_ascii=False, default=str, allow_nan=False)
        except (ValueError, TypeError):
            response["result"] = repr(result)[:1000000]
            encoded = json.dumps(response, ensure_ascii=False, default=str)
    except BaseException:
        encoded = json.dumps({"status": "error", "stdout": output.getvalue()[:1000000],
                              "error": traceback.format_exc(limit=6)}, ensure_ascii=False)
    wire.write(encoded + "\n")
    wire.flush()

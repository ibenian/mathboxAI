# Sandboxing Improvement Plan

## Current State

### Frontend (JavaScript)
Scene JSON files contain math expression strings (e.g. `"sin(t) * a"`) that are compiled and
evaluated using `new Function()` in `compileExpr()` (`static/app.js`). This runs in the main
browser context with full access to `window`, `document`, `fetch`, `localStorage`, etc.

**Risk:** A malicious or untrusted scene JSON file can execute arbitrary JavaScript in the page.

MathBox and Three.js handle WebGL/GLSL rendering — that layer is browser-sandboxed — but they
are not the security boundary for expression evaluation.

### Backend (Python)
Math expressions sent to the `eval_math` tool are evaluated via `safe_eval_math()` in
`gemini_live_tools/math_eval.py`. This uses:

1. `ast.parse(expr, mode='eval')` — statements (including `import`) are rejected by the parser
2. AST node whitelist — only arithmetic, literals, function calls, and list/tuple nodes are
   allowed; `ast.Import`, `ast.Attribute`, `ast.Lambda`, etc. are rejected
3. `eval(..., {"__builtins__": {}}, namespace)` — builtins stripped at runtime, only whitelisted
   math functions available

**Remaining gap:** No CPU or memory limits. A pathological expression like `999**999**999` would
hang the server process.

---

## Planned Improvements

### Frontend — Replace `new Function()` with a math-only evaluator

**Goal:** Expression evaluation cannot access any browser API.

**Approach:** Replace `compileExpr()` / `evalExpr()` with [math.js](https://mathjs.org/) `evaluate()`.

- math.js runs in a sandboxed scope with no access to `window` or any global
- Supports the same functions already in use (`sin`, `cos`, `norm`, `pi`, etc.)
- Syntax differences to handle: `^` instead of `**`, no `Math.sin` — just `sin`

**Migration steps:**
1. Add math.js to `static/index.html` (CDN or vendored)
2. Replace `compileExpr(exprStr)` with a math.js scope builder
3. Replace `evalExpr(fn, t)` with `math.evaluate(expr, scope)` where scope contains `t`,
   slider values, and math constants
4. Update expression syntax in existing scenes if needed (`**` → `^`)
5. Test all animated elements: `animated_vector`, `animated_polygon`, `parametric_curve`,
   `parametric_surface`, `animated_line`

**Additional hardening:** Add a Content Security Policy header in `server.py` that removes
`unsafe-eval`, preventing `new Function` and `eval` at the browser level:

```python
self.send_header('Content-Security-Policy', "script-src 'self'")
```

This acts as a second layer — even if a `new Function` call slips through, the browser blocks it.

---

### Backend — Add subprocess-level resource limits

**Goal:** A pathological expression cannot hang or exhaust the server process.

**Approach:** Run `safe_eval_math()` in a worker subprocess with CPU time and memory limits
using `resource.setrlimit` (Unix) and a wall-clock timeout via `multiprocessing` or
`concurrent.futures`.

**Sketch:**

```python
import resource
import multiprocessing

def _eval_worker(expr, variables, result_queue):
    # Enforce limits inside the worker process
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))      # 2 CPU seconds
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))  # 256MB
    result, err = safe_eval_math(expr, variables)
    result_queue.put((result, err))

def sandboxed_eval_math(expr, variables=None, timeout=5.0):
    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=_eval_worker, args=(expr, variables, q))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.kill()
        return None, "Expression timed out"
    return q.get()
```

**Notes:**
- `resource.setrlimit` is Unix-only; on Windows use a thread with a `threading.Timer` kill
- Worker startup adds ~50–100ms latency per call — acceptable for agent tool calls, not for
  60fps animation (but `eval_math` is only called by the agent, not in the render loop)

---

## Priority

| Item | Risk | Effort | Priority |
|------|------|--------|----------|
| Replace `new Function()` with math.js | High (untrusted scenes) | Medium | High |
| Add CSP `unsafe-eval` removal | High (second layer) | Low | High |
| Add subprocess timeout/memory limits | Medium (DoS) | Low | Medium |

---

## Not In Scope

- Sandboxing the AI agent's tool calls beyond `eval_math` — the agent runs server-side with
  intentional access to scene state; the threat model here is untrusted scene JSON, not the agent
- Sandboxing MathBox/Three.js expressions — these are library internals, not user-supplied

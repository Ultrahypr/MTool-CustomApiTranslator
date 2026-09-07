#!/usr/bin/env python3
"""MTool custom-API translator (stdlib only).

Workflow (MTool itself is a closed binary, so this bridges it):
  1. In MTool: translate page -> "Export Text for External Translate".
     It writes ManualTransFile.json {"Original": "Original", ...} to the game folder.
  2. Here: fill base URL + endpoint + API type, Test, then Translate.
  3. Back in MTool: load the produced file into the game ("Apply loaded text").

Supports OpenAI chat + responses APIs and OpenAI-compatible servers
(LM Studio, Ollama /v1, vLLM, llama.cpp server, DeepSeek/OpenRouter
proxies) plus Anthropic, Gemini, Ollama native, raw-text, and a generic
custom JSON response path. Auth header is selectable (Bearer / x-api-key).

GUI:      python mtool_custom_api.py          (no args -> GUI)
CLI:      python mtool_custom_api.py --file ManualTransFile.json [--out ...]
"""
import argparse
import copy
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(APP_DIR, "custom_api_config.json")

API_TYPES = (
    "openai-chat",       # POST {model, messages} -> choices[0].message.content
    "openai-responses",  # POST {model, instructions, input} -> output_text
    "openai-text",       # POST {model, prompt}   -> choices[0].text
    "anthropic",         # POST {model, messages} -> content[].text
    "gemini",            # POST {contents}        -> candidates[0].content.parts[].text
    "ollama-chat",       # POST {model, messages, stream:false} -> message.content
    "ollama-generate",   # POST {model, prompt, stream:false}   -> response
    "raw-text",          # POST {text} -> whole body is the translation (1 entry/req)
    "custom",            # openai-chat request, reply parsed via responsePath
)

AUTH_STYLES = ("auto", "bearer", "x-api-key", "none")

DEFAULT_ENDPOINT = {
    "openai-chat": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
    "openai-text": "/v1/completions",
    "anthropic": "/v1/messages",
    "gemini": "/v1beta/models/{model}:generateContent",
    "ollama-chat": "/api/chat",
    "ollama-generate": "/api/generate",
    "raw-text": "/translate",
    "custom": "",
}

DEFAULT_CONFIG_DATA = {
    "baseUrl": "http://127.0.0.1:1234",
    "endpoint": "/v1/chat/completions",
    "apiKey": "",
    "model": "local-model",
    "apiType": "openai-chat",
    "authStyle": "auto",
    "responsePath": "choices.0.message.content",
    "sourceLang": "Japanese",
    "targetLang": "Simplified Chinese",
    "batchSize": 20,
    "workers": 4,
    "timeout": 120,
    "maxTokens": 16384,
    "jsonMode": False,
    "verifyTls": False,
    "extraHeaders": {},
    "systemPrompt": "",
}

SYSTEM_PROMPT_TMPL = (
    "You are a game localizer translating {src} to {dst} for a visual novel "
    "(galgame). Return ONLY a JSON object with the same keys and translated "
    "values, no other text, no code fences. Preserve placeholders, control "
    "codes and escape sequences (e.g. \\n, %s, {{0}}, <tags>, [...] codes) "
    "exactly. Keep natural dialogue tone; do not add explanations."
)


# ---------------------------------------------------------------- config ---

def load_config(path):
    cfg = copy.deepcopy(DEFAULT_CONFIG_DATA)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- http -----

def build_url(cfg):
    base = (cfg.get("baseUrl") or "").strip().rstrip("/")
    ep = (cfg.get("endpoint") or "").strip()
    if not ep:
        ep = DEFAULT_ENDPOINT.get(cfg.get("apiType", ""), "")
        ep = ep.format(model=cfg.get("model", ""))
    if "{model}" in ep:
        ep = ep.format(model=cfg.get("model", ""))
    if not ep.startswith("/"):
        ep = "/" + ep
    if not base:
        raise ValueError("baseUrl is empty")
    return base + ep


def auth_style(cfg):
    """Resolved auth header style: bearer | x-api-key | none."""
    style = (cfg.get("authStyle") or "auto").lower()
    if style == "auto":
        style = "x-api-key" if cfg.get("apiType") == "anthropic" else "bearer"
    if style not in ("bearer", "x-api-key", "none"):
        raise ValueError("unknown authStyle: %r" % style)
    return style


def build_headers(cfg):
    # Browser-like headers: some hosts (Cloudflare error 1010) reject
    # script User-Agents such as Python-urllib. extraHeaders can override.
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/126.0.0.0 Safari/537.36",
               "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9"}
    key = (cfg.get("apiKey") or "").strip()
    style = auth_style(cfg)
    if style == "bearer" and key:
        headers["Authorization"] = "Bearer " + key
    elif style == "x-api-key" and key:
        headers["x-api-key"] = key
    if cfg.get("apiType") == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    for k, v in (cfg.get("extraHeaders") or {}).items():
        headers[str(k)] = str(v)
    return headers


def post_json(url, body, headers, timeout, verify_tls):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if not verify_tls and url.lower().startswith("https"):
        ctx = ssl._create_unverified_context()
    else:
        ctx = None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            detail = ""
        raise RuntimeError("HTTP %s from %s: %s"
                           % (e.code, url, detail or e.reason))


def batch_instruction(src, dst):
    return (
        "Translate the JSON values from %s to %s. "
        "Reply with ONLY the JSON object (same keys, translated values)." % (src, dst)
    )


def build_body(cfg, batch):
    """batch: {str_index: original_text}. Returns request body dict."""
    atype = cfg["apiType"]
    model = cfg.get("model", "")
    src, dst = cfg.get("sourceLang", ""), cfg.get("targetLang", "")
    sys_prompt = cfg.get("systemPrompt") or SYSTEM_PROMPT_TMPL.format(src=src, dst=dst)
    user_text = batch_instruction(src, dst) + "\n" + json.dumps(
        batch, ensure_ascii=False, indent=1)
    temp = float(cfg.get("temperature", 0.2))
    max_tok = int(cfg.get("maxTokens", 16384))
    if atype in ("openai-chat", "custom"):
        body = {"model": model,
                "messages": [{"role": "system", "content": sys_prompt},
                             {"role": "user", "content": user_text}],
                "temperature": temp,
                "max_tokens": max_tok}
        if cfg.get("jsonMode"):
            body["response_format"] = {"type": "json_object"}
        return body
    if atype == "openai-text":
        return {"model": model,
                "prompt": sys_prompt + "\n" + user_text,
                "temperature": temp,
                "max_tokens": max_tok}
    if atype == "openai-responses":
        return {"model": model, "instructions": sys_prompt,
                "input": user_text, "temperature": temp,
                "max_output_tokens": max_tok}
    if atype == "anthropic":
        return {"model": model, "max_tokens": max_tok, "system": sys_prompt,
                "messages": [{"role": "user", "content": user_text}]}
    if atype == "gemini":
        return {"contents": [{"parts": [{"text": sys_prompt + "\n" + user_text}]}],
                "generationConfig": {"maxOutputTokens": max_tok,
                                     "temperature": temp}}
    if atype == "ollama-chat":
        return {"model": model,
                "messages": [{"role": "system", "content": sys_prompt},
                             {"role": "user", "content": user_text}],
                "stream": False,
                "options": {"num_predict": max_tok, "temperature": temp}}
    if atype == "ollama-generate":
        return {"model": model, "prompt": sys_prompt + "\n" + user_text,
                "stream": False,
                "options": {"num_predict": max_tok, "temperature": temp}}
    if atype == "raw-text":
        ((_, text),) = batch.items()
        return {"text": text, "source": src, "target": dst, "model": model}
    raise ValueError("unknown apiType: %r" % atype)


# --------------------------------------------------------------- parse ----

def strip_fences(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def resolve_path(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError("cannot resolve %r" % path)
    return cur


def extract_text(obj, atype, cfg):
    """Raw translated string out of a decoded JSON response (or raw body)."""
    if atype in ("openai-chat", "custom"):
        path = "choices.0.message.content" if atype == "openai-chat" else (
            cfg.get("responsePath") or "choices.0.message.content")
        text = resolve_path(obj, path)
        if isinstance(text, (dict, list)):
            return json.dumps(text, ensure_ascii=False)
        return str(text)
    if atype == "openai-text":
        return str(resolve_path(obj, "choices.0.text"))
    if atype == "openai-responses":
        if isinstance(obj.get("output_text"), str):
            return obj["output_text"]
        chunks = []
        for item in obj.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue  # skip reasoning / tool-call items
            for block in item.get("content", []) or []:
                if (isinstance(block, dict)
                        and block.get("type") in ("output_text", "text")
                        and isinstance(block.get("text"), str)):
                    chunks.append(block["text"])
        if not chunks:
            raise ValueError("no output_text/output[] in responses reply")
        return "".join(chunks)
    if atype == "anthropic":
        blocks = resolve_path(obj, "content")
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict))
    if atype == "gemini":
        parts = resolve_path(obj, "candidates.0.content.parts")
        return "".join(p.get("text", "") for p in parts
                       if isinstance(p, dict))
    if atype == "ollama-chat":
        return str(resolve_path(obj, "message.content"))
    if atype == "ollama-generate":
        return str(resolve_path(obj, "response"))
    raise ValueError("unknown apiType: %r" % atype)


def parse_batch_reply(text):
    """Model's batch reply -> {index: translation}."""
    text = strip_fences(text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("batch reply is not a JSON object")
    return {str(k): str(v) for k, v in obj.items()}


def translate_batch(cfg, batch, url=None, headers=None):
    """One batch round-trip. Returns {index: translation} (raw-text: 1 item)."""
    atype = cfg["apiType"]
    url = url or build_url(cfg)
    if atype == "gemini" and cfg.get("apiKey") and auth_style(cfg) != "none":
        sep = "&" if "?" in url else "?"
        url = url + sep + "key=" + urllib.parse.quote(cfg["apiKey"])
    headers = headers or build_headers(cfg)
    body = build_body(cfg, batch)
    raw = post_json(url, body, headers,
                    timeout=int(cfg.get("timeout", 120)),
                    verify_tls=bool(cfg.get("verifyTls", False)))
    if atype == "raw-text":
        return {"0": raw.decode("utf-8", errors="replace").strip()}
    obj = json.loads(raw.decode("utf-8", errors="replace"))
    return parse_batch_reply(extract_text(obj, atype, cfg))


def http_status(exc):
    m = re.search(r"HTTP (\d{3})", str(exc))
    return int(m.group(1)) if m else None


def translate_batch_retry(cfg, batch, log=print, attempts=3):
    # Union of keys seen across attempts is kept on e.partial, so a batch
    # returning 299/300 doesn't discard the 299 when it is retried.
    last = None
    best = {}
    for i in range(attempts):
        try:
            got = translate_batch(cfg, batch)
        except Exception as e:  # noqa: BLE001 - report then retry
            last = e
            st = http_status(e)
            if st is not None and 400 <= st < 500 and st != 429:
                log("  not retrying HTTP %d (client error, retry won't help)"
                    % st)
                break
            log("  retry %d/%d: %s" % (i + 1, attempts, e))
            time.sleep(2 ** i)
            continue
        for k, v in got.items():
            if k in batch:
                best[k] = v
        missing = [k for k in batch if k not in got]
        if not missing:
            return got
        last = ValueError("missing keys in reply: %s" % missing[:5])
        log("  retry %d/%d: %s" % (i + 1, attempts, last))
        time.sleep(2 ** i)
    if last is not None:
        last.partial = dict(best)
    raise last


# ----------------------------------------------------------- translate ----

def load_manual_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object {original: translation}")
    return {str(k): ("" if v is None else str(v)) for k, v in obj.items()}


def plan_batches(data, batch_size, retranslate=False):
    pending = [(k, v) for k, v in data.items()
               if retranslate or v == "" or v == k]
    batches = []
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        # Send the ORIGINAL text (dict key), one entry per payload line,
        # so raw request lines scale with batch size. Never the current
        # value: it may be "" (untranslated) or a stale translation.
        batches.append(({str(j): k for j, (k, _) in enumerate(chunk)},
                        [k for k, _ in chunk]))
    return batches, pending


def run_translation(cfg, in_path, out_path, log=print,
                    progress_cb=None, retranslate=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    data = load_manual_file(in_path)
    atype = cfg["apiType"]
    bs = 1 if atype == "raw-text" else max(1, int(cfg.get("batchSize", 20)))
    workers = max(1, min(32, int(cfg.get("workers", 4))))
    batches, pending = plan_batches(data, bs, retranslate)
    log("%d entries, %d need translation, %d batches x %d workers (%s)"
        % (len(data), len(pending), len(batches), workers, atype))
    prog_path = out_path + ".progress.json"
    done = {}
    if os.path.isfile(prog_path) and not retranslate:
        done = json.loads(open(prog_path, encoding="utf-8").read())
        log("resuming: %d already done" % len(done))
    result = dict(data)
    for k, v in done.items():
        if k in result:
            result[k] = v
    lock = threading.Lock()

    def slog(msg):
        with lock:
            log(msg)

    def save_progress():
        with lock:
            with open(prog_path, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False)

    # Circuit breaker: when the server refuses everything in a row (rate
    # limit, outage), stop sending new requests instead of grinding through
    # thousands of doomed retries.
    ABORT_AFTER = max(10, workers)
    state = {"consec": 0, "aborted": False}

    def is_aborted():
        with lock:
            return state["aborted"]

    def note_success():
        with lock:
            state["consec"] = 0

    def note_failure():
        with lock:
            state["consec"] += 1
            if state["consec"] >= ABORT_AFTER and not state["aborted"]:
                state["aborted"] = True
                return True
            return False

    def do_batch(batch, keys, label="trs"):
        if is_aborted():
            return keys  # skipped: no request sent
        if label == "trs":
            slog("request: %d lines..." % len(keys))
        else:
            slog("retry request: %d lines..." % len(keys))
        try:
            got = translate_batch_retry(cfg, batch, slog)
        except Exception as e:  # noqa: BLE001 - keep going, report at end
            # Salvage partial hits: only truly-missing keys go to retry,
            # so one bad line doesn't re-send the whole batch one by one.
            partial = getattr(e, "partial", None) or {}
            if partial:
                still = []
                with lock:
                    for idx, key in enumerate(keys):
                        t = (partial.get(str(idx)) or "").strip()
                        if t:
                            result[key] = t
                            done[key] = t
                        else:
                            still.append(key)
                if not still:
                    note_success()
                    save_progress()
                    return None
            else:
                still = list(keys)
            slog("  FAILED %d entries: %s" % (len(still), e))
            if note_failure():
                slog("ABORTING: %d consecutive failures "
                     "(rate limit / server refusing?) - no more requests. "
                     "Lower Workers, wait a bit, then re-run to resume."
                     % ABORT_AFTER)
            return still
        with lock:
            for idx, key in enumerate(keys):
                t = got.get(str(idx), "").strip()
                result[key] = t if t else key
                done[key] = result[key]
        note_success()
        save_progress()
        return None

    def drive(items, label, total, n, failed, progress_cb):
        """Run (batch, keys) items in parallel; appends failed keys."""
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix=label) as ex:
            futs = {ex.submit(do_batch, b, ks, label): ks for b, ks in items}
            for fut in as_completed(futs):
                bad = fut.result()
                if bad:
                    failed.extend(bad)
                n[0] += 1
                if progress_cb:
                    progress_cb(n[0], total)

    todo = [(b, ks) for b, ks in batches if not all(k in done for k in ks)]
    total, n, failed = len(batches), [len(batches) - len(todo)], []
    if progress_cb:
        progress_cb(n[0], total)
    drive(todo, "trs", total, n, failed, progress_cb)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if failed and is_aborted():
        log("skipped retry: run was aborted, retrying now "
            "would just spam the server. Re-run later to resume.")
    elif failed:
        # Wholesale batch failures (timeout / truncated oversized reply)
        # usually succeed at a smaller size. Retry as half-batches first;
        # only genuinely-missing leftovers go out one by one.
        rb_bs = max(1, bs // 2)
        if rb_bs > 1 and len(failed) > rb_bs:
            chunks = [failed[i:i + rb_bs]
                      for i in range(0, len(failed), rb_bs)]
            log("%d entries failed, retrying as %d smaller batches..."
                % (len(failed), len(chunks)))
            retry_items = [({str(j): k for j, k in enumerate(c)}, c)
                           for c in chunks]
            failed = []
            total += len(retry_items)
            drive(retry_items, "retry", total, n, failed, progress_cb)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            save_progress()
        if failed:
            log("%d entries still failed, retrying one by one..."
                % len(failed))
            retry_items = [({"0": k}, [k]) for k in failed]
            failed = []
            total += len(retry_items)
            drive(retry_items, "retry", total, n, failed, progress_cb)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            save_progress()
    log("wrote %s (failed: %d)" % (out_path, len(failed)))
    if not failed and os.path.isfile(prog_path):
        os.remove(prog_path)
    return out_path, failed


def test_connection(cfg, log=print):
    atype = cfg["apiType"]
    if atype == "raw-text":
        got = translate_batch_retry(cfg, {"0": "Hello"}, log, attempts=1)
        log("OK, server replied: %r" % got.get("0"))
        return True
    got = translate_batch_retry(cfg, {"0": "Hello"}, log, attempts=1)
    log("OK, server replied: %r" % got.get("0"))
    return True


# ----------------------------------------------------------------- CLI ----

def cmd_cli(args, cfg):
    if args.list_types:
        print("api types:")
        for t in API_TYPES:
            print("  %-16s default endpoint: %s"
                  % (t, DEFAULT_ENDPOINT[t] or "(none - endpoint required)"))
        return 0
    for k in ("base_url", "endpoint", "model", "api_key", "api_type",
              "auth_style", "src", "dst", "batch_size", "workers",
              "max_tokens"):
        v = getattr(args, k, None)
        if v is None:
            continue
        cfg[{"base_url": "baseUrl", "endpoint": "endpoint",
             "model": "model", "api_key": "apiKey", "api_type": "apiType",
             "auth_style": "authStyle", "src": "sourceLang", "dst": "targetLang",
             "batch_size": "batchSize", "workers": "workers",
             "max_tokens": "maxTokens"}[k]] = v
    if args.test:
        test_connection(cfg)
        return 0
    if not args.file:
        print("need --file ManualTransFile.json (or run GUI with no args)")
        return 2
    if args.dry_run:
        data = load_manual_file(args.file)
        bs = (1 if cfg.get("apiType") == "raw-text"
              else max(1, int(cfg.get("batchSize", 20))))
        batches, pending = plan_batches(data, bs, args.retranslate)
        print("dry run: %d entries, %d to translate, %d workers, url=%s"
              % (len(data), len(pending),
                 max(1, int(cfg.get("workers", 4))), build_url(cfg)))
        print("batch plan (lines per request): %s"
              % [len(ks) for _, ks in batches])
        return 0
    out = args.out or (os.path.splitext(args.file)[0]
                       + ".%s.json" % cfg.get("targetLang", "translated"))
    run_translation(cfg, args.file, out, retranslate=args.retranslate)
    print("Next: in MTool, load %s into the game." % out)
    return 0


# ----------------------------------------------------------------- GUI ----

FIELDS = (
    ("baseUrl", "Base URL  (e.g. http://127.0.0.1:1234)"),
    ("endpoint", "Endpoint  (e.g. /v1/chat/completions)"),
    ("apiKey", "API key  (blank for local servers)"),
    ("model", "Model name"),
    ("responsePath", "Response path  (custom type only, e.g. data.text)"),
    ("sourceLang", "Source language"),
    ("targetLang", "Target language"),
)


def run_gui(cfg_path, cfg):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("MTool custom API translator")
    root.geometry("640x560")

    vars_ = {}
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)

    row = 0
    for key, label in FIELDS:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w")
        var = tk.StringVar(value=str(cfg.get(key, "")))
        show = "*" if key == "apiKey" else None
        ent = ttk.Entry(frm, textvariable=var, width=50, show=show)
        ent.grid(row=row, column=1, sticky="ew", pady=2)
        vars_[key] = var
        row += 1

    ttk.Label(frm, text="API / response type").grid(
        row=row, column=0, sticky="w")
    type_var = tk.StringVar(value=cfg.get("apiType", "openai-chat"))
    ttk.OptionMenu(frm, type_var, type_var.get(), *API_TYPES).grid(
        row=row, column=1, sticky="w", pady=2)
    row += 1

    ttk.Label(frm, text="Auth header").grid(
        row=row, column=0, sticky="w")
    auth_var = tk.StringVar(value=cfg.get("authStyle", "auto"))
    ttk.OptionMenu(frm, auth_var, auth_var.get(), *AUTH_STYLES).grid(
        row=row, column=1, sticky="w", pady=2)
    row += 1

    num_frm = ttk.Frame(frm)
    num_frm.grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
    bs_var = tk.StringVar(value=str(cfg.get("batchSize", 20)))
    to_var = tk.StringVar(value=str(cfg.get("timeout", 120)))
    tp_var = tk.StringVar(value=str(cfg.get("temperature", 0.2)))
    w_var = tk.StringVar(value=str(cfg.get("workers", 4)))
    mt_var = tk.StringVar(value=str(cfg.get("maxTokens", 16384)))
    ttk.Label(num_frm, text="Batch").pack(side="left")
    ttk.Entry(num_frm, textvariable=bs_var, width=5).pack(side="left", padx=4)
    ttk.Label(num_frm, text="Timeout(s)").pack(side="left")
    ttk.Entry(num_frm, textvariable=to_var, width=6).pack(side="left", padx=4)
    ttk.Label(num_frm, text="Temp").pack(side="left")
    ttk.Entry(num_frm, textvariable=tp_var, width=5).pack(side="left", padx=4)
    ttk.Label(num_frm, text="Workers").pack(side="left")
    ttk.Entry(num_frm, textvariable=w_var, width=4).pack(side="left", padx=4)
    ttk.Label(num_frm, text="MaxTok").pack(side="left")
    ttk.Entry(num_frm, textvariable=mt_var, width=7).pack(side="left", padx=4)
    json_var = tk.BooleanVar(value=bool(cfg.get("jsonMode", False)))
    ttk.Checkbutton(num_frm, text="jsonMode", variable=json_var).pack(
        side="left", padx=8)
    row += 1

    file_var = tk.StringVar(value="")
    f_frm = ttk.Frame(frm)
    f_frm.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
    ttk.Entry(f_frm, textvariable=file_var).pack(
        side="left", fill="x", expand=True)
    ttk.Button(f_frm, text="Pick ManualTransFile.json",
               command=lambda: file_var.set(
                   filedialog.askopenfilename(
                       filetypes=[("JSON", "*.json"), ("All", "*.*")] )
                   or file_var.get())).pack(side="left", padx=4)
    row += 1

    log = tk.Text(frm, height=12, state="disabled")
    log.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=4)
    frm.rowconfigure(row, weight=1)
    frm.columnconfigure(1, weight=1)
    row += 1

    bar = ttk.Progressbar(frm, mode="determinate")
    bar.grid(row=row, column=0, columnspan=2, sticky="ew", pady=2)
    row += 1

    log_q = queue.Queue()

    def ui_log(msg):
        log_q.put(msg)

    def poll_log():
        try:
            while True:
                item = log_q.get_nowait()
                if isinstance(item, tuple):
                    _, pn, ptotal = item
                    bar["maximum"] = ptotal
                    bar["value"] = pn
                else:
                    log.configure(state="normal")
                    log.insert("end", item + "\n")
                    log.see("end")
                    log.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(120, poll_log)

    root.after(120, poll_log)

    def collect():
        c = dict(cfg)
        for k, _ in FIELDS:
            c[k] = vars_[k].get().strip()
        c["apiType"] = type_var.get()
        c["authStyle"] = auth_var.get()
        c["batchSize"] = max(1, int(bs_var.get() or 20))
        c["workers"] = max(1, min(32, int(w_var.get() or 4)))
        c["timeout"] = int(to_var.get() or 120)
        c["maxTokens"] = max(256, int(mt_var.get() or 16384))
        c["temperature"] = float(tp_var.get() or 0.2)
        c["jsonMode"] = bool(json_var.get())
        save_config(cfg_path, c)
        return c

    def work(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - show in log + popup
                ui_log("ERROR: %s" % e)
                messagebox.showerror("Error", str(e))
            finally:
                for b in btns:
                    b.configure(state="normal")
        for b in btns:
            b.configure(state="disabled")
        threading.Thread(target=run, daemon=True).start()

    btn_frm = ttk.Frame(frm)
    btn_frm.grid(row=row, column=0, columnspan=2, pady=4)
    btns = []

    def on_test():
        c = collect()
        ui_log("testing %s ..." % build_url(c))
        test_connection(c, ui_log)

    def on_go():
        c = collect()
        src = file_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("No file",
                                   "Pick the ManualTransFile.json first.")
            return
        out = os.path.splitext(src)[0] + ".%s.json" % c.get("targetLang",
                                                             "translated")
        ui_log("translating -> %s" % out)

        def prog(n, total):
            log_q.put(("__prog__", n, total))
        run_translation(c, src, out, ui_log, prog)
        ui_log("DONE. In MTool, load the file into the game.")
        messagebox.showinfo("Done", "Wrote:\n%s" % out)

    for text, cmd in (("Test connection", lambda: work(on_test)),
                      ("Translate", lambda: work(on_go))):
        b = ttk.Button(btn_frm, text=text, command=cmd)
        b.pack(side="left", padx=6)
        btns.append(b)

    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--file", help="ManualTransFile.json exported by MTool")
    ap.add_argument("--out", help="output JSON path")
    ap.add_argument("--test", action="store_true",
                    help="probe baseUrl+endpoint+key+parse path and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retranslate", action="store_true",
                    help="re-translate entries already translated")
    ap.add_argument("--list-types", action="store_true")
    ap.add_argument("--base-url"); ap.add_argument("--endpoint")
    ap.add_argument("--model"); ap.add_argument("--api-key")
    ap.add_argument("--api-type", choices=API_TYPES)
    ap.add_argument("--auth-style", choices=AUTH_STYLES)
    ap.add_argument("--src"); ap.add_argument("--dst")
    ap.add_argument("--batch-size", type=int)
    ap.add_argument("--workers", type=int, help="parallel requests (1-32)")
    ap.add_argument("--max-tokens", type=int, help="max reply tokens")
    ap.add_argument("--gui", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.gui or (not args.file and not args.test and not args.list_types
                    and not args.dry_run):
        try:
            run_gui(args.config, cfg)
            return 0
        except ImportError:
            print("tkinter unavailable, use CLI flags (--help)")
            return 2
    return cmd_cli(args, cfg)


if __name__ == "__main__":
    sys.exit(main())

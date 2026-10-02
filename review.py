#!/usr/bin/env python3
"""
AI PR reviewer for GitHub.

Fetches a pull request diff, asks a model (Gemini or Nous Portal) to review it,
and posts the result back as a PR review with inline comments.

Environment:
  GH_PAT              GitHub token with repo scope on the target repo
  GEMINI_API_KEY      required when PROVIDER=gemini
  NOUS_PORTAL_API_KEY required when PROVIDER=nous
  TARGET_REPO         owner/name
  PR_NUMBER           pull request number
  PROVIDER            gemini | nous
  MODEL               optional model id override
  DRY_RUN             set to 1 to print the review instead of posting it
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "nous": "poolside/laguna-s-2.1:free",
}

# Tried in order when the primary Gemini model is rate-limited or overloaded.
GEMINI_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]

# Nous free-tier models vary in availability. Verified working: laguna-s-2.1,
# longcat-2.5-preview. longcat-2.5 is a reasoning model and can burn its whole
# token budget on reasoning_content, so it is the fallback, not the default.
NOUS_FALLBACKS = [
    "poolside/laguna-s-2.1:free",
    "meituan/longcat-2.5-preview:free",
]

# Diff budget (characters) sent to the model.
MAX_DIFF_CHARS = 120_000
# Cap inline comments so a review stays readable.
MAX_INLINE = 15
# Files whose diffs add noise without review value.
SKIP_PATTERNS = (
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)yarn\.lock$"),
    re.compile(r"(^|/)pnpm-lock\.yaml$"),
    re.compile(r"(^|/)Cargo\.lock$"),
    re.compile(r"(^|/)go\.sum$"),
    re.compile(r"\.(png|jpe?g|gif|webp|ico|svg|woff2?|ttf|eot|mp4|mp3|zip|pdf)$", re.I),
    re.compile(r"(^|/)dist/"),
    re.compile(r"(^|/)node_modules/"),
)

SYSTEM_PROMPT = """You are a rigorous senior software reviewer performing a code review on a pull request.

Focus, in priority order:
1. Correctness bugs, logic errors, off-by-one, null/undefined handling, race conditions.
2. Security: injection, authz/authn gaps, secret handling, unsafe deserialization, SSRF.
3. Resource and memory safety, leaks, unbounded growth, blocking calls on hot paths.
4. API misuse and hallucinated APIs (calls to functions/methods that do not exist).
5. Performance regressions.
6. Maintainability only where it materially matters.

Rules:
- Report only issues you can point at in the diff. Do not invent problems.
- If the diff is fine, say so plainly. Do not manufacture nits to seem useful.
- Be concrete: name the symbol, state the failure mode, and give the fix.
- Never restate what the code does without a reason to mention it.
- Ignore pure formatting unless it changes behavior.

Respond with ONLY a JSON object, no markdown fence, matching exactly:
{
  "summary": "markdown string: 2-6 bullets covering what the PR does and your overall assessment",
  "verdict": "approve" | "comment" | "request_changes",
  "comments": [
    {
      "path": "path/to/file.ext",
      "line": <integer, line number in the NEW version of the file>,
      "severity": "bug" | "security" | "perf" | "style" | "nit",
      "body": "markdown: the issue and the concrete fix"
    }
  ]
}
Use an empty comments array if there is nothing worth flagging."""


def log(msg):
    print(msg, flush=True)


def gh(method, path, body=None, accept="application/vnd.github+json"):
    """Call the GitHub REST API. Returns the decoded response text."""
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", "Bearer " + os.environ["GH_PAT"])
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "pr-review-bot")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"GitHub API {method} {path} failed: {exc.code} {detail[:600]}")


def http_json(url, payload, headers, timeout=600):
    req = urllib.request.Request(url, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Model API failed: {exc.code} {detail[:600]}")
    except Exception as exc:
        # Timeouts and connection resets surface as SystemExit so the caller's
        # fallback chain can move to the next model instead of crashing.
        raise SystemExit(f"Model API failed: transport {type(exc).__name__}: {str(exc)[:200]}")


def commentable_lines(diff_text):
    """Map new-side file path -> set of line numbers that can carry a comment."""
    files, current, newline = {}, None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
                continue
            current = target[2:] if target.startswith("b/") else target
            files.setdefault(current, set())
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            newline = int(match.group(1)) if match else 0
        elif current is None:
            continue
        elif line.startswith("+"):
            files[current].add(newline)
            newline += 1
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            newline += 1
    return files


def split_diff_by_file(diff_text):
    """Split a unified diff into per-file chunks."""
    chunks, current = [], []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def file_path_of(chunk):
    for line in chunk.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                return None
            return target[2:] if target.startswith("b/") else target
    return None


def prepare_diff(diff_text):
    """Drop noise files and enforce the character budget."""
    kept, dropped, used = [], [], 0
    for chunk in split_diff_by_file(diff_text):
        path = file_path_of(chunk)
        if path and any(p.search(path) for p in SKIP_PATTERNS):
            dropped.append(path)
            continue
        if used + len(chunk) > MAX_DIFF_CHARS:
            dropped.append(path or "?")
            continue
        kept.append(chunk)
        used += len(chunk)
    return "".join(kept), dropped


def find_undefined_symbols(diff_text, repo_root):
    """Flag method calls added by the PR that exist nowhere in the repository.

    Catches the LLM failure mode of inventing APIs: a call such as
    `foo->lookupTrack(x)` where `lookupTrack` is declared in no header or source
    file at the PR head. Symbols that come from an external library (Qt, std)
    also miss, so these are reported as warnings to verify rather than as
    proven bugs.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return []

    candidates, path, newline = {}, None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else (
                target[2:] if target.startswith("b/") else target
            )
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            newline = int(match.group(1)) if match else 0
        elif path is None:
            continue
        elif line.startswith("+"):
            for match in re.finditer(r"(?:->|\.)\s*([A-Za-z_]\w{3,})\s*\(", line[1:]):
                candidates.setdefault(match.group(1), (path, newline))
            newline += 1
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            newline += 1

    if not candidates:
        return []

    def declared(symbol):
        """True when the symbol appears somewhere other than as a call.

        A hit like `_members->lookupTrack(x)` is the call itself, not a
        declaration. Only a hit with no `->symbol(` or `.symbol(` prefix counts.
        """
        attempts = (
            ["git", "-C", repo_root, "grep", "-nw", "-e", symbol, "--"],
            ["grep", "-rnw", "--include=*.h", "--include=*.cpp",
             "--include=*.hpp", "--include=*.inl", "-e", symbol, repo_root],
        )
        call_pattern = re.compile(r"(->|\.)\s*" + re.escape(symbol) + r"\s*\(")
        for tool in attempts:
            try:
                result = subprocess.run(tool, capture_output=True, text=True, timeout=180)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode not in (0, 1):
                continue
            for line in result.stdout.splitlines():
                content = line
                if tool[0] == "git":
                    # strip "path:lineno:" prefix
                    first = line.find(":")
                    second = line.find(":", first + 1)
                    if second != -1:
                        content = line[second + 1:]
                stripped = content.strip()
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                if call_pattern.search(content):
                    continue
                return True
        return False

    findings = []
    for symbol, (path, line) in sorted(candidates.items())[:80]:
        if not declared(symbol):
            findings.append({"path": path, "line": line, "symbol": symbol})
    return findings


def format_symbol_findings(findings):
    if not findings:
        return ""
    lines = [
        "",
        "STATIC CHECK - these method calls were added by this PR but the method "
        "name is not declared anywhere in the repository at the PR head:",
    ]
    for item in findings:
        lines.append(f"  - `{item['symbol']}` called at {item['path']}:{item['line']}")
    lines.append(
        "Each is either a hallucinated API (a real bug) or a method from an "
        "external library. Verify which before relying on it, and report the "
        "hallucinated ones as bugs."
    )
    return "\n".join(lines) + "\n"


def build_prompt(pr, diff_text, dropped, symbol_findings):
    body = (pr.get("body") or "").strip()
    if len(body) > 4000:
        body = body[:4000] + "\n...(truncated)"
    note = ""
    if dropped:
        listed = ", ".join(sorted(set(dropped))[:20])
        note = f"\n\nNOTE: these files were omitted (noise or budget): {listed}\n"
    return (
        f"Repository: {os.environ['TARGET_REPO']}\n"
        f"Pull request #{pr['number']}: {pr['title']}\n"
        f"Author: {pr['user']['login']}\n"
        f"Base: {pr['base']['ref']}  <-  Head: {pr['head']['ref']}\n"
        f"Description:\n{body or '(none)'}\n"
        f"{note}"
        f"{format_symbol_findings(symbol_findings)}\n"
        f"Unified diff:\n\n{diff_text}"
    )


def call_gemini(model, prompt):
    """Try the requested model, then fall back through the chain on 429/503."""
    chain = [model] + [m for m in GEMINI_FALLBACKS if m != model]
    last_error = None
    for candidate in chain:
        url = GEMINI_URL.format(model=candidate)
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 16384,
            },
        }
        try:
            data = http_json(url, payload, {"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        except SystemExit as exc:
            message = str(exc)
            if "failed: 429" in message or "failed: 503" in message or "failed: 500" in message or "transport" in message:
                log(f"  {candidate} unavailable, trying next ({message[:60]}...)")
                last_error = message
                continue
            raise
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise SystemExit(f"Unexpected Gemini response: {json.dumps(data)[:600]}")
        if candidate != model:
            log(f"  used fallback model {candidate}")
        return text, candidate
    raise SystemExit(f"All Gemini models unavailable. Last error: {last_error}")


def call_nous(model, prompt):
    """Try the requested model, then the fallback chain.

    Handles reasoning models that return content=null and put everything in
    reasoning_content, and enforces JSON mode where the provider supports it.
    """
    chain = [model] + [m for m in NOUS_FALLBACKS if m != model]
    last_error = None
    for candidate in chain:
        payload = {
            "model": candidate,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 32768,
        }
        data = None
        for with_json_mode in (True, False):
            attempt = dict(payload)
            if with_json_mode:
                attempt["response_format"] = {"type": "json_object"}
            try:
                data = http_json(
                    NOUS_URL, attempt, {"Authorization": "Bearer " + os.environ["NOUS_PORTAL_API_KEY"]}
                )
                break
            except SystemExit as exc:
                message = str(exc)
                # 400 usually means the model rejects response_format.
                if with_json_mode and "failed: 400" in message:
                    log(f"  {candidate} rejects JSON mode, retrying without it")
                    continue
                if any(code in message for code in ("failed: 404", "failed: 429", "failed: 503", "failed: 500", "transport")):
                    log(f"  {candidate} unavailable, trying next ({message[:70]}...)")
                    last_error = message
                    break
                raise
        if data is None:
            continue

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if not text:
            reasoning = message.get("reasoning_content") or ""
            last_error = (
                f"{candidate} returned no content "
                f"(finish_reason={choice.get('finish_reason')}, reasoning={len(reasoning)}c)"
            )
            log(f"  {last_error}, trying next")
            continue
        if candidate != model:
            log(f"  used fallback model {candidate}")
        return text, candidate
    raise SystemExit(f"All Nous models unavailable. Last error: {last_error}")


def parse_model_json(text):
    """Tolerate a stray code fence or surrounding prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise SystemExit("Model did not return parseable JSON:\n" + text[:1200])


def render_body(result, model, provider, dropped, stats):
    verdict = str(result.get("verdict", "comment")).lower()
    icon = {"approve": "✅", "request_changes": "🛑"}.get(verdict, "💬")
    label = {"approve": "Approve", "request_changes": "Request changes"}.get(verdict, "Comment")
    lines = [
        f"## {icon} AI review — {label}",
        "",
        result.get("summary", "(no summary)").strip(),
        "",
    ]
    comments = result.get("comments") or []
    if comments:
        lines.append(f"**{len(comments)} inline finding(s)** posted below.")
    else:
        lines.append("**No inline findings.**")
    lines += [
        "",
        "---",
        f"<sub>model `{model}` via {provider} · {stats} · "
        f"{len(dropped)} file(s) skipped</sub>",
    ]
    return "\n".join(lines)


def main():
    repo = os.environ["TARGET_REPO"]
    pr_number = os.environ["PR_NUMBER"]
    provider = os.environ.get("PROVIDER", "gemini").strip().lower()
    model = os.environ.get("MODEL", "").strip() or DEFAULT_MODELS[provider]

    log(f"Reviewing {repo}#{pr_number} with {provider}:{model}")

    pr = json.loads(gh("GET", f"/repos/{repo}/pulls/{pr_number}"))
    diff_text = gh(
        "GET",
        f"/repos/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    if not diff_text.strip():
        raise SystemExit("Empty diff; nothing to review.")

    valid = commentable_lines(diff_text)
    prepared, dropped = prepare_diff(diff_text)

    repo_root = os.environ.get("REPO_ROOT", "").strip()
    symbol_findings = []
    if repo_root:
        symbol_findings = find_undefined_symbols(prepared, repo_root)
        log(f"static check: {len(symbol_findings)} undeclared symbol(s) in added lines")

    prompt = build_prompt(pr, prepared, dropped, symbol_findings)
    log(f"diff={len(diff_text)}c sent={len(prepared)}c files={len(valid)} skipped={len(dropped)}")

    raw, model_used = call_gemini(model, prompt) if provider == "gemini" else call_nous(model, prompt)
    result = parse_model_json(raw)

    inline, rejected = [], 0
    for item in (result.get("comments") or [])[:MAX_INLINE]:
        path, line = item.get("path"), item.get("line")
        if path in valid and isinstance(line, int) and line in valid[path]:
            severity = item.get("severity", "nit")
            inline.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": f"**{severity}** — {item.get('body', '').strip()}",
                }
            )
        else:
            rejected += 1
    if rejected:
        log(f"dropped {rejected} comment(s) that did not anchor to a changed line")

    stats = f"{len(inline)} inline, {rejected} unanchored"
    body = render_body(result, model_used, provider, dropped, stats)

    if os.environ.get("DRY_RUN"):
        log("DRY_RUN set - not posting. Rendered review follows:\n")
        log(body)
        log("\ninline comments:")
        for item in inline:
            log(f"  {item['path']}:{item['line']}  {item['body'][:160]}")
        return

    if inline:
        gh(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            {"body": body, "event": "COMMENT", "comments": inline},
        )
        log(f"posted review with {len(inline)} inline comment(s)")
    else:
        gh("POST", f"/repos/{repo}/issues/{pr_number}/comments", {"body": body})
        log("posted summary comment")


if __name__ == "__main__":
    main()

"""
llm_client.py - OpenAI-compatible HTTP client for Text/Code LLMs and Image Generation AI.
"""
import base64
import json
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path


from .config import load_settings, normalize_proxy_url


def test_proxy(raw_proxy: str, ptype: str = "") -> dict:
    import time
    from curl_cffi import requests as cf_req
    purl = normalize_proxy_url(raw_proxy, ptype)
    if not purl:
        return {"ok": False, "error": "Укажите адрес прокси"}
    t0 = time.time()
    r = cf_req.get("https://api.ipify.org?format=json", proxy=purl, timeout=12)
    dt = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    ip = (r.json() or {}).get("ip", "")
    return {"ok": True, "proxy_url": purl, "ip": ip, "latency_ms": dt}


def http_json(url: str, method: str = "GET", headers: dict | None = None, payload: dict | None = None, timeout: int = 180, proxy_url: str | None = None) -> dict:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    purl = proxy_url if proxy_url is not None else (load_settings().get("proxy_url") or "")
    if purl:
        from curl_cffi import requests as cf_req
        r = cf_req.request(
            method,
            url,
            headers=hdrs,
            json=payload,
            proxy=purl,
            timeout=timeout,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {url}: {r.text[:500]}")
        return r.json()

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body[:500]}")


def fetch_models(base_url: str, api_key: str) -> list[str]:
    base = base_url.rstrip("/")
    hdrs = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    res = http_json(f"{base}/models", method="GET", headers=hdrs, timeout=20)
    return [str(m.get("id")) for m in res.get("data", []) if m.get("id")]


def file_to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def extract_python_and_guide(text: str) -> tuple[str, dict]:
    """Extract ```python ... ``` code block and optional ```json ... ``` texture guide block."""
    py_match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    code = py_match.group(1).strip() if py_match else text.strip()

    guide = {}
    for jm in re.finditer(r"```json\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE):
        try:
            parsed = json.loads(jm.group(1).strip())
            if isinstance(parsed, dict):
                guide = parsed
                break
        except Exception:
            pass
    return code, guide


def extract_json_object(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    raw = m.group(1).strip() if m else text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


def call_text_llm(cfg: dict, messages: list[dict], temperature: float = 0.25) -> str:
    if cfg.get("provider_mode") == "gemini_cookies":
        from . import gemini_direct
        return gemini_direct.generate_text(messages, model=cfg.get("text_model"))

    base = cfg["text_base_url"].rstrip("/")
    hdrs = {"Authorization": f"Bearer {cfg['text_api_key']}"} if cfg.get("text_api_key") else {}
    res = http_json(
        f"{base}/chat/completions",
        method="POST",
        headers=hdrs,
        payload={
            "model": cfg["text_model"],
            "messages": messages,
            "temperature": temperature,
        },
        timeout=180,
    )
    choices = res.get("choices") or []
    if not choices:
        raise RuntimeError(f"Empty response from text API: {res}")
    return str(choices[0].get("message", {}).get("content", ""))


def call_image_ai(
    cfg: dict,
    prompt: str,
    out_path: Path,
    ref_paths: list[Path] | None = None,
    size: str = "1024x1024",
) -> Path:
    if cfg.get("provider_mode") == "gemini_cookies":
        from . import gemini_direct
        return gemini_direct.generate_image(prompt, out_path, model=cfg.get("image_model"), ref_paths=ref_paths)

    base = cfg["image_base_url"].rstrip("/")
    api_key = cfg.get("image_api_key", "")
    hdrs = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    refs_b64 = [file_to_data_url(rp) for rp in (ref_paths or []) if rp and rp.is_file()]
    payload: dict = {
        "model": cfg["image_model"],
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "url",
    }
    if refs_b64:
        payload["reference_images"] = refs_b64

    try:
        res = http_json(f"{base}/images/generations", method="POST", headers=hdrs, payload=payload, timeout=240)
    except RuntimeError as e:
        if "reference_images" in payload and ("400" in str(e) or "422" in str(e) or "extra" in str(e).lower()):
            payload.pop("reference_images", None)
            res = http_json(f"{base}/images/generations", method="POST", headers=hdrs, payload=payload, timeout=240)
        else:
            raise

    data = res.get("data") or []
    if not data:
        raise RuntimeError(f"Image API returned no images: {res}")

    first = data[0]
    if first.get("b64_json"):
        out_path.write_bytes(base64.b64decode(first["b64_json"]))
        return out_path

    img_url = first.get("url")
    if not img_url:
        raise RuntimeError(f"Image API response missing url/b64_json: {first}")

    purl = cfg.get("proxy_url") or load_settings().get("proxy_url") or ""
    if purl:
        from curl_cffi import requests as cf_req
        r = cf_req.get(img_url, headers={"User-Agent": "MeshForge/1.0"}, proxy=purl, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"Image download HTTP {r.status_code}")
        out_path.write_bytes(r.content)
        return out_path

    req = urllib.request.Request(img_url, headers={"User-Agent": "MeshForge/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out_path.write_bytes(resp.read())
    return out_path

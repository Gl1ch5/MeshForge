"""
gemini_direct.py - Built-in multi-account Gemini Web cookie provider using gemini_webapi.
Runs a persistent background asyncio event loop thread so sync callers (HTTP server & CLI)
can reuse initialized, auto-refreshing GeminiClient sessions safely.
"""
import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from gemini_webapi import GeminiClient

from .config import APP_ROOT, load_settings, save_settings

AUTH_COOKIE_NAMES = {
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-1PSIDRTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-3PSIDCC",
    "__Secure-3PSIDRTS",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "SIDCC",
    "OSID",
    "__Secure-OSID",
    "NID",
    "AEC",
    "SOCS",
}


def _append_auth_cookie(out: list[dict], seen: set[tuple[str, str]], name: str, val: str, dom: str = "", path: str = "/", sec: bool | None = None) -> None:
    name = name.strip()
    val = val.strip().strip('"').strip("'")
    if not name or not val or name not in AUTH_COOKIE_NAMES:
        return
    if not dom:
        dom = "gemini.google.com" if name in ("OSID", "__Secure-OSID") else ".google.com"
    if not (dom.endswith("google.com") or dom == "gemini.google.com"):
        return
    if name in ("OSID", "__Secure-OSID") and dom != "gemini.google.com":
        return
    if sec is None:
        sec = name.startswith("__Secure-") or name.startswith("__Host-")
    key = (name, dom)
    if key in seen:
        for idx, ex in enumerate(out):
            if ex["name"] == name and ex["domain"] == dom:
                out[idx] = {"name": name, "value": val, "domain": dom, "path": path or "/", "secure": bool(sec)}
                return
    seen.add(key)
    out.append({"name": name, "value": val, "domain": dom, "path": path or "/", "secure": bool(sec)})


def parse_cookies_with_meta(raw_text: str | None = None) -> tuple[list[dict], str]:
    cookies: list[dict] = []
    seen: set[tuple[str, str]] = set()
    fmt = "unknown"

    txt = (raw_text or "").strip()
    if not txt:
        raise ValueError("Вставьте куки из браузера")

    if txt.startswith("[") or txt.startswith("{"):
        try:
            data = json.loads(txt)
            if isinstance(data, list):
                fmt = "json_array"
                for item in data:
                    if isinstance(item, dict):
                        _append_auth_cookie(
                            cookies, seen,
                            str(item.get("name", "")),
                            str(item.get("value", "")),
                            str(item.get("domain", "")),
                            str(item.get("path", "/")),
                            item.get("secure"),
                        )
            elif isinstance(data, dict):
                if isinstance(data.get("cookies"), list):
                    fmt = "json_array"
                    for item in data["cookies"]:
                        if isinstance(item, dict):
                            _append_auth_cookie(
                                cookies, seen,
                                str(item.get("name", "")),
                                str(item.get("value", "")),
                                str(item.get("domain", "")),
                                str(item.get("path", "/")),
                                item.get("secure"),
                            )
                else:
                    fmt = "json_dict"
                    for k, v in data.items():
                        _append_auth_cookie(cookies, seen, str(k), str(v))
        except json.JSONDecodeError:
            pass

    if not cookies and "\t" in txt:
        hits = 0
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("# "):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) >= 7:
                dom, _, path, sec_str, _, name, val = parts[:7]
                _append_auth_cookie(cookies, seen, name, val, dom, path, sec_str.upper() == "TRUE")
                hits += 1
        if hits > 0:
            fmt = "netscape_txt"

    if not cookies:
        cleaned = txt
        m_curl = re.search(r"-H\s+['\"]cookie:\s*([^'\"]+)['\"]", cleaned, re.IGNORECASE)
        if m_curl:
            cleaned = m_curl.group(1)
            fmt = "curl_cookie"
        elif re.match(r"^cookie\s*:\s*", cleaned, re.IGNORECASE):
            cleaned = re.sub(r"^cookie\s*:\s*", "", cleaned, flags=re.IGNORECASE)
            fmt = "cookie_header"
        elif ";" in cleaned and "=" in cleaned:
            fmt = "cookie_header"
        else:
            fmt = "kv_lines"

        for tok in re.split(r"[;\r\n]+", cleaned):
            tok = tok.strip()
            if not tok or tok.startswith("#"):
                continue
            if "=" in tok:
                k, v = tok.split("=", 1)
                _append_auth_cookie(cookies, seen, k, v)
            elif ":" in tok:
                k, v = tok.split(":", 1)
                _append_auth_cookie(cookies, seen, k, v)

    if not any(c["name"] == "__Secure-1PSID" for c in cookies):
        raise ValueError("Не найдена кука __Secure-1PSID")
    return cookies, fmt


def summarize_cookies(cookies: list[dict]) -> dict:
    names = {c.get("name"): c.get("value", "") for c in cookies if isinstance(c, dict)}
    psid = names.get("__Secure-1PSID", "")
    preview = (psid[:10] + "..." + psid[-6:]) if len(psid) > 18 else ("***" if psid else "")
    return {
        "count": len(cookies),
        "psid_preview": preview,
        "has_1psidts": bool(names.get("__Secure-1PSIDTS")),
        "has_osid": bool(names.get("OSID") or names.get("__Secure-OSID")),
    }


IMAGE_GEN_MODELS = ["nano-banana", "nano-banana-pro"]
IMAGE_GEN_ALIASES = {"nano-banana", "nano-banana-pro", "gemini-image", "imagen-3", "imagen-4", "gemini-2.5-flash-image", "gemini-3-pro-image"}


def _resolve_model(client: GeminiClient, model_name: str | None) -> Any:
    if not model_name:
        if not hasattr(client, "list_models"):
            from gemini_webapi.constants import Model
            return Model.UNSPECIFIED
        return None
    m = model_name.strip().lower()
    aliases = {
        "auto": ["gemini-авто", "авто", "auto", "unspecified"],
        "gemini-auto": ["gemini-авто", "авто", "auto", "unspecified"],
        "gemini-авто": ["gemini-авто", "авто", "unspecified"],
        "gemini-4-hydrogen": ["gemini-4-hydrogen", "hydrogen", "gemini-3.0-flash"],
        "gemini-neon": ["gemini-neon", "neon", "gemini-3.0-flash-thinking", "gemini-3.0-pro"],
        "gemini-carbon": ["gemini-carbon", "carbon", "gemini-3.0-pro"],
        "gemini-2.5-flash": ["gemini-4-hydrogen", "gemini-3.0-flash", "gemini-flash"],
        "gemini-flash": ["gemini-4-hydrogen", "gemini-3.0-flash", "gemini-flash"],
        "gemini-2.5-pro": ["gemini-neon", "gemini-3.0-pro", "gemini-pro"],
        "gemini-pro": ["gemini-neon", "gemini-3.0-pro", "gemini-pro"],
        "nano-banana": ["gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash", "unspecified"],
        "nano-banana-pro": ["gemini-neon", "gemini-carbon", "gemini-3.0-pro", "gemini-авто", "unspecified"],
        "nano-banana-2": ["gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash", "unspecified"],
        "gemini-image": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
        "gemini-2.5-flash-image": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
        "gemini-3-pro-image": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
        "imagen-3": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
        "imagen-4": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
        "dall-e-3": ["unspecified", "gemini-авто", "gemini-4-hydrogen", "gemini-3.0-flash"],
    }
    targets = aliases.get(m, [m])

    # gemini-webapi >= 2.1 dynamic list_models()
    if hasattr(client, "list_models"):
        try:
            for t in targets:
                if t == "unspecified":
                    return None
                for av in client.list_models() or []:
                    av_name = getattr(av, "model_name", "").lower()
                    av_disp = getattr(av, "display_name", "").lower()
                    av_al = [a.lower() for a in getattr(av, "aliases", [])]
                    if av_name == t or t in av_name or t in av_disp or t in av_al:
                        return av
        except Exception:
            pass
        return None

    # gemini-webapi 1.x fallback via Model enum (never return None: 1.x rejects model=None)
    from gemini_webapi.constants import Model
    for t in targets:
        if t == "unspecified":
            return Model.UNSPECIFIED
        for member in Model:
            m_val = str(member.value[0]).lower() if isinstance(member.value, tuple) else str(member.value).lower()
            if m_val == t or t in m_val:
                return member
    return Model.UNSPECIFIED


def _supports_kwarg(fn: Any, kw: str) -> bool:
    try:
        import inspect
        return kw in inspect.signature(fn).parameters
    except Exception:
        return False


def _extract_images(out: Any) -> list[Any]:
    cands = list(getattr(out, "candidates", None) or [])
    chosen_idx = int(getattr(out, "chosen", 0) or 0)
    ordered = []
    if 0 <= chosen_idx < len(cands):
        ordered.append(cands[chosen_idx])
    ordered.extend(c for i, c in enumerate(cands) if i != chosen_idx)
    for c in ordered:
        gen = getattr(c, "generated_images", None) or []
        if gen:
            return list(gen)
    for c in ordered:
        imgs = getattr(c, "images", None) or []
        if imgs:
            return list(imgs)
    return list(getattr(out, "images", None) or [])


class _DirectPool:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._clients: dict[str, GeminiClient] = {}
        self._client_proxies: dict[str, str] = {}
        self._rr: int = 0

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_sync(self, coro, timeout: int = 240):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _get_accounts(self) -> list[dict]:
        return list(load_settings().get("gemini_accounts") or [])

    def _save_accounts(self, accounts: list[dict]) -> None:
        save_settings({"gemini_accounts": accounts})

    async def _close_client(self, acc_id: str) -> None:
        self._client_proxies.pop(acc_id, None)
        c = self._clients.pop(acc_id, None)
        if c is not None:
            try:
                await c.close()
            except Exception:
                pass

    async def close_all_clients(self) -> None:
        for aid in list(self._clients.keys()):
            await self._close_client(aid)

    async def _init_client(self, acc: dict, clear_cache: bool = False) -> tuple[GeminiClient, str]:
        cookies_list = acc.get("cookies") or []
        psid = next((c["value"] for c in cookies_list if c["name"] == "__Secure-1PSID"), "")
        psidts = next((c["value"] for c in cookies_list if c["name"] == "__Secure-1PSIDTS"), "")
        proxy_url = (load_settings().get("proxy_url") or "").strip() or None

        if clear_cache and psid:
            import tempfile
            cache_file = Path(tempfile.gettempdir()) / "gemini_webapi" / f".cached_cookies_{psid}.json"
            if cache_file.exists():
                try:
                    cache_file.unlink()
                except Exception:
                    pass

        client = GeminiClient(psid, psidts, proxy=proxy_url)
        jar = getattr(client, "_cookies", None)
        if jar is None:
            jar = getattr(client, "cookies", None)
        if jar is not None:
            for c in cookies_list:
                try:
                    jar.set(
                        c["name"], c["value"],
                        domain=c.get("domain", ".google.com"),
                        path=c.get("path", "/"),
                        secure=bool(c.get("secure", True)),
                    )
                except Exception:
                    pass

        await asyncio.wait_for(
            client.init(timeout=120, auto_close=False, auto_refresh=True, refresh_interval=540, verbose=False),
            timeout=25.0,
        )
        status_obj = getattr(client, "account_status", None)
        status_name = getattr(status_obj, "name", "AVAILABLE") if status_obj is not None else "AVAILABLE"
        if status_name != "AVAILABLE":
            desc = getattr(status_obj, "description", "Куки устарели или не авторизованы")
            await client.close()
            raise RuntimeError(f"{status_name}: {desc}")
        if not getattr(client, "access_token", None):
            await client.close()
            raise RuntimeError("UNAUTHENTICATED: Не получен SNlM0e токен, куки устарели")
        return client, (proxy_url or "")

    async def _detect_email(self, client: GeminiClient) -> str:
        try:
            if client.client is None:
                return ""
            r = await client.client.get("https://gemini.google.com/app", timeout=15)
            html = r.text or ""
            m = re.search(r'"oPEP7c":"([^"]+)"', html)
            if m and "@" in m.group(1):
                return m.group(1).strip()
            gm = re.search(r"[a-zA-Z0-9._%+-]+@(?:gmail|googlemail)\.com", html)
            if gm:
                return gm.group(0).strip()
        except Exception:
            pass
        return ""

    async def _get_or_connect(self, acc_id: str, force: bool = False) -> GeminiClient:
        accs = self._get_accounts()
        acc = next((a for a in accs if a["id"] == acc_id), None)
        if not acc:
            raise RuntimeError("Аккаунт не найден")

        cur_proxy = (load_settings().get("proxy_url") or "").strip()
        existing = self._clients.get(acc_id)
        if (
            existing is not None
            and not force
            and getattr(existing, "_running", False)
            and self._client_proxies.get(acc_id, "") == cur_proxy
        ):
            return existing

        await self._close_client(acc_id)
        client, used_proxy = await self._init_client(acc, clear_cache=force)
        self._clients[acc_id] = client
        self._client_proxies[acc_id] = used_proxy

        email = acc.get("email") or await self._detect_email(client)
        if hasattr(client, "list_models"):
            models = [
                {"name": getattr(m, "model_name", ""), "display_name": getattr(m, "display_name", "")}
                for m in (client.list_models() or [])
            ]
        else:
            models = [
                {"name": "gemini-авто", "display_name": "Авто"},
                {"name": "gemini-3.0-flash", "display_name": "Flash"},
                {"name": "gemini-3.0-pro", "display_name": "Pro"},
            ]
        ui = getattr(client, "_usage_info", {}) or {}
        quotas = getattr(client, "_quotas", {}) or {}
        tier = (ui.get("tier") or {}).get("label", "")

        for a in accs:
            if a["id"] == acc_id:
                a["status"] = "online"
                a["last_error"] = ""
                a["last_checked_at"] = time.time()
                if email:
                    a["email"] = email
                    if not a.get("label") or a["label"].startswith("Gemini #"):
                        a["label"] = email.split("@")[0]
                a["tier"] = tier
                a["models"] = models
                a["quotas"] = {"usage_info": ui, "quotas": quotas}
                break
        self._save_accounts(accs)
        return client

    async def verify_account(self, acc_id: str) -> dict:
        try:
            await self._get_or_connect(acc_id, force=True)
            accs = self._get_accounts()
            acc = next((a for a in accs if a["id"] == acc_id), {})
            return {"ok": True, "account": serialize_account(acc)}
        except Exception as e:
            err = str(e)
            accs = self._get_accounts()
            for a in accs:
                if a["id"] == acc_id:
                    a["status"] = "expired" if ("UNAUTHENTICATED" in err or "cookie" in err.lower()) else "error"
                    a["last_error"] = err[:350]
                    a["last_checked_at"] = time.time()
                    self._save_accounts(accs)
                    return {"ok": False, "error": err, "account": serialize_account(a)}
            return {"ok": False, "error": err}

    def _pick_candidates(self) -> list[dict]:
        accs = [a for a in self._get_accounts() if a.get("enabled", True)]
        if not accs:
            raise RuntimeError("Нет добавленных кук Gemini. Добавьте аккаунт в Настройках или Мастере.")
        healthy = [a for a in accs if a.get("status") != "expired"]
        pool = healthy if healthy else accs
        idx = self._rr % len(pool)
        self._rr += 1
        return pool[idx:] + pool[:idx]

    async def generate_text(self, messages: list[dict], model: str | None = None) -> str:
        # Format OpenAI messages + optional base64 images
        import base64, tempfile
        parts: list[str] = []
        sys_parts: list[str] = []
        temp_files: list[Path] = []
        for msg in messages:
            role = (msg.get("role") or "user").lower()
            content = msg.get("content", "")
            chunks = []
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        chunks.append(str(item.get("text", "")))
                    elif item.get("type") == "image_url":
                        url = (item.get("image_url") or {}).get("url", "") if isinstance(item.get("image_url"), dict) else str(item.get("image_url") or "")
                        if url.startswith("data:image/"):
                            hdr, b64 = url.split(",", 1)
                            ext = "jpg" if "jpeg" in hdr or "jpg" in hdr else "png"
                            tf = Path(tempfile.gettempdir()) / f"mf_in_{uuid.uuid4().hex[:8]}.{ext}"
                            tf.write_bytes(base64.b64decode(b64))
                            temp_files.append(tf)
            joined = "\n".join(c for c in chunks if c).strip()
            if not joined:
                continue
            if role == "system":
                sys_parts.append(joined)
            elif role == "assistant":
                parts.append(f"Assistant: {joined}")
            else:
                parts.append(f"User: {joined}")

        if not sys_parts and len(parts) == 1 and parts[0].startswith("User: "):
            prompt = parts[0][6:]
        else:
            prefix = ("System instructions:\n" + "\n".join(sys_parts) + "\n\n") if sys_parts else ""
            prompt = prefix + "\n\n".join(parts)

        last_err = "Unknown error"
        try:
            for acc in self._pick_candidates():
                try:
                    client = await self._get_or_connect(acc["id"])
                    resolved = _resolve_model(client, model)
                    files_arg = [str(p) for p in temp_files] if temp_files else None
                    gen_kw: dict[str, Any] = {"files": files_arg}
                    if resolved is not None:
                        gen_kw["model"] = resolved
                    out = await client.generate_content(prompt, **gen_kw)
                    return (out.text or "").strip()
                except Exception as e:
                    last_err = str(e)
                    await self._close_client(acc["id"])
            raise RuntimeError(f"Ошибка Gemini (куки): {last_err}")
        finally:
            for tf in temp_files:
                tf.unlink(missing_ok=True)

    async def generate_image(self, prompt: str, out_path: Path, model: str | None = None, ref_paths: list[Path] | None = None) -> Path:
        files_arg = [str(p) for p in (ref_paths or []) if p and p.is_file()] or None
        last_err = "Unknown error"
        want_pro = "pro" in (model or "").lower()
        for acc in self._pick_candidates():
            try:
                client = await self._get_or_connect(acc["id"])
                resolved = _resolve_model(client, model or "nano-banana")
                gen_kw: dict[str, Any] = {"files": files_arg}
                if resolved is not None:
                    gen_kw["model"] = resolved
                if want_pro and _supports_kwarg(client.generate_content, "use_pro"):
                    gen_kw["use_pro"] = True
                out = await client.generate_content(f"Generate an image: {prompt}", **gen_kw)
                imgs = _extract_images(out)
                if not imgs:
                    # Retry once with default model & simplified phrasing on transient Gemini refusal/glitch
                    retry_kw: dict[str, Any] = {"files": files_arg}
                    if not hasattr(client, "list_models"):
                        from gemini_webapi.constants import Model
                        retry_kw["model"] = Model.G_3_0_FLASH if resolved == Model.UNSPECIFIED else Model.UNSPECIFIED
                    if _supports_kwarg(client.generate_content, "use_pro"):
                        retry_kw["use_pro"] = False
                    out = await client.generate_content(f"Create an image of {prompt}", **retry_kw)
                    imgs = _extract_images(out)
                if not imgs:
                    raise RuntimeError(f"Gemini не вернул изображение: {(out.text or '')[:160]}")
                save_kw: dict[str, Any] = {
                    "path": str(out_path.parent),
                    "filename": out_path.name,
                }
                if _supports_kwarg(imgs[0].save, "client") and getattr(client, "client", None) is not None:
                    save_kw["client"] = client.client
                if _supports_kwarg(imgs[0].save, "full_size") or (
                    hasattr(imgs[0], "_perform_save") and _supports_kwarg(imgs[0]._perform_save, "full_size")
                ):
                    save_kw["full_size"] = True
                saved_str = await imgs[0].save(**save_kw)
                if not saved_str:
                    raise RuntimeError(f"Не удалось сохранить изображение в {out_path}")
                saved_p = Path(saved_str)
                if saved_p != out_path and saved_p.is_file():
                    saved_p.replace(out_path)
                return out_path
            except Exception as e:
                last_err = str(e)
                await self._close_client(acc["id"])
        raise RuntimeError(f"Ошибка генерации изображения (куки): {last_err}")


_pool = _DirectPool()


def serialize_account(acc: dict) -> dict:
    return {
        "id": acc.get("id", ""),
        "label": acc.get("label", ""),
        "email": acc.get("email", ""),
        "enabled": bool(acc.get("enabled", True)),
        "status": acc.get("status", "unknown"),
        "last_error": acc.get("last_error", ""),
        "tier": acc.get("tier", ""),
        "models": acc.get("models", []),
        "quotas": acc.get("quotas", {}),
        "last_checked_at": acc.get("last_checked_at", 0),
        "cookies_summary": summarize_cookies(acc.get("cookies") or []),
    }


def list_accounts() -> list[dict]:
    return [serialize_account(a) for a in (load_settings().get("gemini_accounts") or [])]


def add_or_update_account(raw_cookies: str, label: str = "", verify: bool = True) -> dict:
    cookies, fmt = parse_cookies_with_meta(raw_cookies)
    new_psid = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSID"), "")
    cfg = load_settings()
    accs: list[dict] = list(cfg.get("gemini_accounts") or [])

    existing = None
    for a in accs:
        ex_psid = next((c["value"] for c in (a.get("cookies") or []) if c["name"] == "__Secure-1PSID"), "")
        if ex_psid and ex_psid == new_psid:
            existing = a
            break

    if existing is not None:
        acc_id = existing["id"]
        existing["cookies"] = cookies
        existing["enabled"] = True
        if label.strip():
            existing["label"] = label.strip()
        _pool.run_sync(_pool._close_client(acc_id), timeout=10)
    else:
        acc_id = uuid.uuid4().hex[:10]
        accs.append({
            "id": acc_id,
            "label": label.strip() or f"Gemini #{len(accs) + 1}",
            "email": "",
            "cookies": cookies,
            "enabled": True,
            "status": "checking",
            "last_error": "",
            "tier": "",
            "models": [],
            "quotas": {},
            "last_checked_at": 0,
        })

    save_settings({"gemini_accounts": accs, "provider_mode": "gemini_cookies"})
    if verify:
        res = _pool.run_sync(_pool.verify_account(acc_id), timeout=40)
        return {**res, "format": fmt, "updated_existing": existing is not None}
    acc = next(a for a in load_settings().get("gemini_accounts", []) if a["id"] == acc_id)
    return {"ok": True, "format": fmt, "updated_existing": existing is not None, "account": serialize_account(acc)}


def check_account(acc_id: str) -> dict:
    return _pool.run_sync(_pool.verify_account(acc_id), timeout=40)


def toggle_account(acc_id: str, enabled: bool) -> dict:
    cfg = load_settings()
    accs = list(cfg.get("gemini_accounts") or [])
    for a in accs:
        if a["id"] == acc_id:
            a["enabled"] = bool(enabled)
            save_settings({"gemini_accounts": accs})
            return {"ok": True, "account": serialize_account(a)}
    raise RuntimeError("Аккаунт не найден")


def delete_account(acc_id: str) -> None:
    _pool.run_sync(_pool._close_client(acc_id), timeout=10)
    cfg = load_settings()
    accs = [a for a in (cfg.get("gemini_accounts") or []) if a["id"] != acc_id]
    save_settings({"gemini_accounts": accs})


def reset_all_clients() -> None:
    _pool.run_sync(_pool.close_all_clients(), timeout=10)


def list_available_models(kind: str = "all") -> list[str]:
    if kind == "image":
        return list(IMAGE_GEN_MODELS)

    base_list = ["gemini-авто", "gemini-4-hydrogen", "gemini-neon", "gemini-carbon"]
    seen = set(base_list) | IMAGE_GEN_ALIASES
    for a in (load_settings().get("gemini_accounts") or []):
        for m in a.get("models") or []:
            nm = m.get("name")
            if nm and nm not in seen:
                seen.add(nm)
                base_list.append(nm)

    if kind == "text":
        return base_list
    return base_list + list(IMAGE_GEN_MODELS)


def generate_text(messages: list[dict], model: str | None = None) -> str:
    return _pool.run_sync(_pool.generate_text(messages, model=model), timeout=200)


def generate_image(prompt: str, out_path: Path, model: str | None = None, ref_paths: list[Path] | None = None) -> Path:
    return _pool.run_sync(_pool.generate_image(prompt, out_path, model=model, ref_paths=ref_paths), timeout=240)

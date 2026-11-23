"""
app.py - MeshForge Desktop App (Python + pywebview + Blender CLI/bpy).
"""
import importlib.util
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import webview

from backend import gemini_direct, llm_client, pipeline
from backend.config import BUNDLE_ROOT, normalize_proxy_url

UI_DIR = BUNDLE_ROOT / "ui"
ASSETS_DIR = BUNDLE_ROOT / "assets"

TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()


def _detect_runtime_status() -> dict:
    has_bpy = importlib.util.find_spec("bpy") is not None
    cfg = pipeline.load_settings()
    bp = (cfg.get("blender_path") or "").strip()
    has_blender_cli = bool(bp and Path(bp).is_file())
    mode = cfg.get("provider_mode", "openai_api")
    accs = cfg.get("gemini_accounts") or []
    has_cookies = any(a.get("enabled", True) for a in accs)
    has_openai = bool((cfg.get("text_base_url") or "").strip()) and bool((cfg.get("text_api_key") or "").strip())
    ai_ready = (mode == "gemini_cookies" and has_cookies) or (mode == "openai_api" and has_openai)
    return {
        "has_bpy": has_bpy,
        "has_blender_cli": has_blender_cli,
        "blender_path": bp,
        "provider_mode": mode,
        "proxy_url": cfg.get("proxy_url", ""),
        "gemini_accounts_count": len(accs),
        "ai_ready": ai_ready,
        "setup_done": bool(cfg.get("setup_done")) and (has_bpy or has_blender_cli),
        "ready": has_bpy or has_blender_cli,
    }


def _install_bpy_worker(log_cb=None, stage_cb=None) -> dict:
    if log_cb:
        log_cb("Запуск pip install bpy (около 356 МБ)...")
    py_exe = shutil.which("python") if getattr(sys, "frozen", False) else sys.executable
    if not py_exe:
        raise RuntimeError("Python не найден в PATH для установки bpy. Выберите blender.exe вручную.")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        [py_exe, "-m", "pip", "install", "bpy"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        s = line.strip()
        if s and log_cb:
            log_cb(s)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"pip install bpy завершился с кодом {rc}")
    pipeline.save_settings({"use_bpy": True, "blender_path": "", "setup_done": True})
    if log_cb:
        log_cb("Модуль bpy успешно установлен.")
    return {"ok": True, "has_bpy": True}


def _start_background_task(fn, *args) -> str:
    tid = uuid.uuid4().hex[:12]
    with TASKS_LOCK:
        TASKS[tid] = {
            "id": tid,
            "status": "running",
            "stage": "running",
            "logs": [],
            "partial": None,
            "result": None,
            "error": "",
        }

    def log_cb(msg: str) -> None:
        with TASKS_LOCK:
            TASKS[tid]["logs"].append(msg)

    def stage_cb(payload: dict) -> None:
        with TASKS_LOCK:
            TASKS[tid]["partial"] = payload
            TASKS[tid]["stage"] = payload.get("stage", "working")

    def worker() -> None:
        try:
            res = fn(*args, log_cb=log_cb, stage_cb=stage_cb)
            with TASKS_LOCK:
                TASKS[tid]["status"] = "done"
                TASKS[tid]["stage"] = "done"
                TASKS[tid]["result"] = res
        except Exception as e:
            with TASKS_LOCK:
                TASKS[tid]["status"] = "error"
                TASKS[tid]["error"] = str(e)
                TASKS[tid]["logs"].append(f"Ошибка: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return tid


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype, _ = mimetypes.guess_type(str(path))
        if path.suffix.lower() == ".glb":
            ctype = "model/gltf-binary"
        elif path.suffix.lower() == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif path.suffix.lower() == ".css":
            ctype = "text/css; charset=utf-8"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        p = unquote(parsed.path)

        if p == "/" or p == "/index.html":
            return self._send_file(UI_DIR / "index.html")

        if p.startswith("/ui/"):
            rel = p[len("/ui/"):]
            target = (UI_DIR / rel).resolve()
            if str(target).startswith(str(UI_DIR.resolve())):
                return self._send_file(target)
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if p.startswith("/assets/"):
            rel = p[len("/assets/"):]
            target = (ASSETS_DIR / rel).resolve()
            if str(target).startswith(str(ASSETS_DIR.resolve())):
                return self._send_file(target)
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if p.startswith("/outputs/"):
            rel = p[len("/outputs/"):]
            target = (pipeline.OUTPUTS_DIR / rel).resolve()
            if str(target).startswith(str(pipeline.OUTPUTS_DIR.resolve())):
                return self._send_file(target)
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if p == "/api/status":
            return self._send_json(_detect_runtime_status())

        if p == "/api/styles":
            styles = [{"id": k, "label": v["label"]} for k, v in pipeline.STYLE_PRESETS.items()]
            return self._send_json({"styles": styles})

        if p == "/api/setup/scan":
            scan = pipeline.scan_blender_installations()
            rt = _detect_runtime_status()
            return self._send_json({**scan, **rt})

        if p == "/api/settings":
            return self._send_json({"settings": pipeline.load_settings()})

        if p == "/api/cookies/accounts":
            return self._send_json({"accounts": gemini_direct.list_accounts()})

        if p == "/api/jobs":
            return self._send_json({"jobs": pipeline.list_jobs()})

        if p.startswith("/api/jobs/"):
            jid = p[len("/api/jobs/"):].strip("/")
            jd = pipeline.OUTPUTS_DIR / jid
            if not jd.is_dir():
                return self._send_json({"error": "Not found"}, 404)
            meta = json.loads((jd / "meta.json").read_text(encoding="utf-8")) if (jd / "meta.json").exists() else {}
            kind = meta.get("kind", "model")
            if kind == "texture":
                return self._send_json({
                    "ok": True,
                    "kind": "texture",
                    "job_id": jid,
                    "meta": meta,
                    "urls": meta.get("urls", {}),
                })
            if kind == "pack":
                return self._send_json({
                    "ok": True,
                    "kind": "pack",
                    "job_id": jid,
                    "meta": meta,
                    "items": meta.get("items", []),
                })
            code = (jd / "generated_model.py").read_text(encoding="utf-8") if (jd / "generated_model.py").exists() else ""
            return self._send_json({
                "ok": True,
                "kind": "model",
                "job_id": jid,
                "meta": meta,
                "glb_url": f"/outputs/{jid}/model.glb" if (jd / "model.glb").exists() else "",
                "preview_4side_url": f"/outputs/{jid}/preview_4side.png" if (jd / "preview_4side.png").exists() else "",
                "uv_guide_url": f"/outputs/{jid}/uv_guide.png" if (jd / "uv_guide.png").exists() else "",
                "uv_3d_preview_url": f"/outputs/{jid}/uv_3d_preview.png" if (jd / "uv_3d_preview.png").exists() else "",
                "uv_clean_url": f"/outputs/{jid}/uv_template_clean.png" if (jd / "uv_template_clean.png").exists() else "",
                "texture_url": f"/outputs/{jid}/texture.png" if (jd / "texture.png").exists() else "",
                "code": code,
            })

        if p.startswith("/api/task/"):
            tid = p[len("/api/task/"):].strip("/")
            with TASKS_LOCK:
                t = TASKS.get(tid)
                if not t:
                    return self._send_json({"error": "Task not found"}, 404)
                return self._send_json(t)

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        p = unquote(parsed.path)
        body = self._read_json()

        if p == "/api/settings":
            old_proxy = pipeline.load_settings().get("proxy_url", "")
            if "proxy_url" in body or "proxy_type" in body:
                body["proxy_url"] = normalize_proxy_url(body.pop("proxy_raw", body.get("proxy_url", "")), body.pop("proxy_type", ""))
            saved = pipeline.save_settings(body)
            if saved.get("proxy_url", "") != old_proxy:
                try:
                    gemini_direct.reset_all_clients()
                except Exception:
                    pass
            return self._send_json({"ok": True, "settings": saved, "runtime": _detect_runtime_status()})

        if p == "/api/proxy/test":
            try:
                res = llm_client.test_proxy(str(body.get("proxy_raw", "")), str(body.get("proxy_type", "")))
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})

        if p == "/api/setup/confirm":
            bp = str(body.get("blender_path", "")).strip()
            use_bpy = bool(body.get("use_bpy", False))
            if bp and not Path(bp).is_file():
                return self._send_json({"ok": False, "error": "Файл blender.exe не найден по указанному пути"}, 400)
            saved = pipeline.save_settings({"blender_path": bp, "use_bpy": use_bpy, "setup_done": True})
            return self._send_json({"ok": True, "settings": saved, "runtime": _detect_runtime_status()})

        if p == "/api/setup/browse-blender":
            picked = ""
            if webview.windows:
                res = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=("Blender (blender.exe)",),
                )
                if res:
                    picked = str(res[0])
            return self._send_json({"ok": bool(picked), "path": picked})

        if p == "/api/setup/install-bpy":
            tid = _start_background_task(_install_bpy_worker)
            return self._send_json({"ok": True, "task_id": tid})

        if p == "/api/models":
            try:
                cfg = pipeline.load_settings()
                if body.get("provider_mode") == "gemini_cookies" or (not body.get("base_url") and cfg.get("provider_mode") == "gemini_cookies"):
                    return self._send_json({"ok": True, "models": gemini_direct.list_available_models()})
                models = pipeline.fetch_models(body.get("base_url", ""), body.get("api_key", ""))
                return self._send_json({"ok": True, "models": models})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)

        if p == "/api/cookies/parse-preview":
            try:
                cookies, fmt = gemini_direct.parse_cookies_with_meta(body.get("raw_text", ""))
                summary = gemini_direct.summarize_cookies(cookies)
                return self._send_json({"ok": True, "format": fmt, **summary})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})

        if p == "/api/cookies/accounts":
            try:
                res = gemini_direct.add_or_update_account(
                    body.get("raw_text", ""),
                    label=str(body.get("label", "")),
                    verify=bool(body.get("verify", True)),
                )
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)

        if p.startswith("/api/cookies/accounts/") and p.endswith("/check"):
            acc_id = p.split("/")[4]
            return self._send_json(gemini_direct.check_account(acc_id))

        if p == "/api/generate":
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                return self._send_json({"error": "Введите описание модели"}, 400)
            cfg = pipeline.load_settings()
            for k in ("style", "tex_size", "text_model", "image_model"):
                if body.get(k):
                    cfg[k] = body[k]
            refs = body.get("ref_images") or []
            tid = _start_background_task(pipeline.run_full_generation, prompt, cfg, refs)
            return self._send_json({"ok": True, "task_id": tid})

        if p == "/api/regen-texture":
            job_id = str(body.get("job_id", "")).strip()
            custom_prompt = str(body.get("prompt", "")).strip() or None
            cfg = pipeline.load_settings()
            for k in ("style", "tex_size", "text_model", "image_model"):
                if body.get(k):
                    cfg[k] = body[k]
            tid = _start_background_task(pipeline.run_stage2_texture, job_id, cfg, custom_prompt)
            return self._send_json({"ok": True, "task_id": tid})

        if p == "/api/generate-texture":
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                return self._send_json({"error": "Введите описание текстуры"}, 400)
            cfg = pipeline.load_settings()
            for k in ("text_model", "image_model"):
                if body.get(k):
                    cfg[k] = body[k]
            opts = {
                "style": body.get("style", cfg.get("style", "realistic")),
                "tex_size": int(body.get("tex_size", 512)),
                "seamless": bool(body.get("seamless", True)),
                "enhance": bool(body.get("enhance", True)),
                "maps": body.get("maps", ["normal", "roughness", "ao", "height"]),
                "pbr_params": body.get("pbr_params") or {},
                "override_pbr": bool(body.get("override_pbr", False)),
            }
            refs = body.get("ref_images") or []
            tid = _start_background_task(pipeline.run_texture_generation, prompt, cfg, opts, refs)
            return self._send_json({"ok": True, "task_id": tid})

        if p == "/api/generate-pack":
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                return self._send_json({"error": "Введите описание пака текстур"}, 400)
            cfg = pipeline.load_settings()
            for k in ("text_model", "image_model"):
                if body.get(k):
                    cfg[k] = body[k]
            opts = {
                "style": body.get("style", cfg.get("style", "realistic")),
                "tex_size": int(body.get("tex_size", 512)),
                "seamless": bool(body.get("seamless", True)),
                "count": int(body.get("count", 4)),
                "maps": body.get("maps", ["normal", "roughness", "ao", "height"]),
            }
            refs = body.get("ref_images") or []
            tid = _start_background_task(pipeline.run_texture_pack, prompt, cfg, opts, refs)
            return self._send_json({"ok": True, "task_id": tid})

        if p == "/api/open-folder":
            job_id = str(body.get("job_id", "")).strip()
            folder = (pipeline.OUTPUTS_DIR / job_id) if job_id else pipeline.OUTPUTS_DIR
            if folder.exists():
                subprocess.Popen(["explorer", str(folder)])
            return self._send_json({"ok": True})

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self):
        p = unquote(urlparse(self.path).path)
        body = self._read_json()
        if p.startswith("/api/cookies/accounts/"):
            acc_id = p.split("/")[-1]
            try:
                return self._send_json(gemini_direct.toggle_account(acc_id, bool(body.get("enabled", True))))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 404)
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        p = unquote(urlparse(self.path).path)
        if p.startswith("/api/cookies/accounts/"):
            acc_id = p.split("/")[-1]
            gemini_direct.delete_account(acc_id)
            return self._send_json({"ok": True})
        self.send_error(HTTPStatus.NOT_FOUND)


def _apply_win32_icon(ico_path: Path) -> None:
    if os.name != "nt" or not ico_path.is_file():
        return
    try:
        import ctypes
        import time as _t
        user32 = ctypes.windll.user32
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        hicon = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not hicon:
            return
        for _ in range(20):
            hwnd = user32.FindWindowW(None, "MeshForge")
            if hwnd:
                user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)
                user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)
                break
            _t.sleep(0.25)
    except Exception:
        pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    try:
        port = find_free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        ico_path = ASSETS_DIR / "icon.ico"
        threading.Thread(target=_apply_win32_icon, args=(ico_path,), daemon=True).start()

        webview.create_window(
            title="MeshForge",
            url=f"http://127.0.0.1:{port}/",
            width=1420,
            height=880,
            min_size=(1060, 700),
            background_color="#111214",
        )
        webview.start()
        server.shutdown()
    except Exception:
        import traceback
        from backend.config import APP_ROOT
        (APP_ROOT / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()

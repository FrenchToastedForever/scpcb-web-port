"""Static file server + Community Save Sharing API for the SCP:CB web port.

Actual in-game save format: flat .cbsav files under /Saves
(Global SavePath$ = "Saves\" and LoadSaveGames() scans /Saves/*.cbsav).
Imported community saves are therefore written canonically to
/Saves/[Shared] <name>.cbsav; a legacy mirror is also created under
/Save/[Shared] <name>/ for compatibility.
"""

import base64
import binascii
import datetime
import functools
import http.server
import json
import os
import posixpath
import re
import shutil
import struct
import urllib.parse
import uuid

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webgame")
SHARED_DIR = os.path.join(BASE_DIR, "shared_saves")
CLIENT_LOG = os.path.join(BASE_DIR, "client-failures.jsonl")
PORT = int(os.environ.get("PORT", "8090"))

MAX_UPLOAD = 10 * 1024 * 1024
MAX_CLIENT_LOG = 64 * 1024
MAX_FILES = 64
MAX_PATH_LEN = 200
MAX_TITLE = 50
MAX_AUTHOR = 50
MAX_DESC = 200

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
DRIVE_RE = re.compile(r"^[A-Za-z]:")


class IsolationHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = http.server.SimpleHTTPRequestHandler.extensions_map.copy()
    extensions_map.update({
        ".wasm": "application/wasm",
        ".bin": "application/octet-stream",
    })

    def end_headers(self):
        # Mandatory headers for WebGPU & WebAssembly
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        # Aggressive caching so clients don't redownload the assets
        if self.path.endswith((".wasm", ".data", ".bin", ".js")):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif self.path.endswith((".html", ".json")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ------------------------------------------------------------------ utils

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _url_path(self):
        path = urllib.parse.urlsplit(self.path).path
        return path or "/"

    @staticmethod
    def _normalize(path):
        if len(path) > 1:
            path = path.rstrip("/")
        return path or "/"

    def _blocked(self, path):
        candidates = set()
        for candidate in (path, urllib.parse.unquote(path)):
            candidate = re.sub(r"/{2,}", "/", candidate)
            candidates.add(candidate)
            candidates.add(posixpath.normpath(candidate))
        for candidate in candidates:
            if (
                candidate == "/serve.py"
                or candidate.startswith("/serve.py/")
                or candidate == "/shared_saves"
                or candidate.startswith("/shared_saves/")
                or candidate == "/client-failures.jsonl"
            ):
                return True
        return False

    # -------------------------------------------------------------------- GET

    def do_GET(self):
        path = self._normalize(self._url_path())

        if self._blocked(self._url_path()):
            self._json(404, {"success": False, "error": "Not found"})
            return

        if path == "/api/saves":
            self._list_saves()
            return

        if path.startswith("/api/saves/"):
            save_id = path[len("/api/saves/"):]
            if "/" in save_id:
                self._json(404, {"success": False, "error": "Not found"})
                return
            self._get_save(save_id)
            return

        if path == "/api/clientlog":
            self._list_client_logs()
            return

        if path == "/api" or path.startswith("/api/"):
            self._json(404, {"success": False, "error": "Not found"})
            return

        super().do_GET()

    def _list_saves(self):
        try:
            os.makedirs(SHARED_DIR, exist_ok=True)
        except OSError:
            self._json(500, {"success": False, "error": "Could not access save storage"})
            return

        saves = []
        try:
            entries = list(os.scandir(SHARED_DIR))
        except OSError:
            entries = []

        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name
                if name.startswith("."):
                    continue
                meta = _read_metadata(entry.path)
                if not meta:
                    continue
                save_id = meta.get("id")
                if not isinstance(save_id, str) or not ID_RE.fullmatch(save_id):
                    continue
                if save_id != name:
                    continue
                seed, seed_kind = _effective_seed(meta, entry.path)
                saves.append({
                    "id": save_id,
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "description": meta.get("description", ""),
                    "timestamp": meta.get("timestamp", ""),
                    "saveFolder": meta.get("saveFolder", ""),
                    "fileCount": meta.get("fileCount", 0),
                    "totalBytes": meta.get("totalBytes", 0),
                    "seed": seed,
                    "seedKind": seed_kind,
                })
            except (OSError, ValueError, TypeError):
                # corrupt or incomplete entry: skip it
                continue

        saves.sort(
            key=lambda m: (
                str(m.get("timestamp") or ""),
                str(m.get("id") or ""),
            ),
            reverse=True,
        )
        self._json(200, saves)

    def _get_save(self, save_id):
        if not ID_RE.fullmatch(save_id or ""):
            self._json(404, {"success": False, "error": "Save not found"})
            return

        save_dir = os.path.join(SHARED_DIR, save_id)
        real_shared = os.path.realpath(SHARED_DIR)
        real_save = os.path.realpath(save_dir)
        if not real_save.startswith(real_shared + os.sep):
            self._json(404, {"success": False, "error": "Save not found"})
            return
        if os.path.islink(save_dir) or not os.path.isdir(save_dir):
            self._json(404, {"success": False, "error": "Save not found"})
            return

        meta = _read_metadata(save_dir)
        if not meta:
            self._json(404, {"success": False, "error": "Save not found"})
            return

        files_root = os.path.join(save_dir, "files")
        files = {}
        if os.path.isdir(files_root):
            for root, dirs, names in os.walk(files_root):
                # never follow symlinked directories
                dirs[:] = [
                    d for d in dirs
                    if not os.path.islink(os.path.join(root, d))
                ]
                for name in names:
                    full = os.path.join(root, name)
                    try:
                        if os.path.islink(full) or not os.path.isfile(full):
                            continue
                        real_full = os.path.realpath(full)
                        if not real_full.startswith(real_save + os.sep):
                            continue
                        rel = os.path.relpath(full, files_root).replace(os.sep, "/")
                        with open(full, "rb") as fh:
                            files[rel] = base64.b64encode(fh.read()).decode("ascii")
                    except OSError:
                        continue

        seed, seed_kind = _effective_seed(meta, save_dir)
        payload = {
            "id": meta.get("id", save_id),
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "description": meta.get("description", ""),
            "timestamp": meta.get("timestamp", ""),
            "saveFolder": meta.get("saveFolder", ""),
            "fileCount": meta.get("fileCount", len(files)),
            "totalBytes": meta.get("totalBytes", 0),
            "seed": seed,
            "seedKind": seed_kind,
            "format": meta.get("format", 1),
            "files": files,
        }
        self._json(200, payload)

    # ------------------------------------------------------------------- POST

    def _list_client_logs(self):
        try:
            with open(CLIENT_LOG, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200000))
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            data = ""
        lines = [line for line in data.splitlines() if line.strip()]
        self._json(200, {"success": True, "lines": lines[-50:]})

    def _record_client_log(self):
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self.close_connection = True
            self._json(411, {"success": False, "error": "Content-Length required"})
            return
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            self.close_connection = True
            self._json(400, {"success": False, "error": "Invalid Content-Length"})
            return
        if length <= 0:
            self.close_connection = True
            self._json(411, {"success": False, "error": "Content-Length required"})
            return
        if length > MAX_CLIENT_LOG:
            self.close_connection = True
            self._json(413, {"success": False, "error": "Log is too large"})
            return

        try:
            body = self.rfile.read(length)
        except (OSError, ValueError):
            self._json(400, {"success": False, "error": "Could not read request body"})
            return

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"success": False, "error": "Request body must be valid JSON"})
            return
        if not isinstance(data, dict):
            self._json(400, {"success": False, "error": "Request body must be a JSON object"})
            return

        record = {
            "received": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "client": data,
        }
        try:
            with open(CLIENT_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._json(500, {"success": False, "error": "Could not write log"})
            return
        self._json(200, {"success": True})

    def do_POST(self):
        path = self._normalize(self._url_path())
        if path == "/api/clientlog":
            self._record_client_log()
            return
        if path != "/api/saves":
            self._json(404, {"success": False, "error": "Not found"})
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self.close_connection = True
            self._json(411, {"success": False, "error": "Content-Length required"})
            return
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            self.close_connection = True
            self._json(400, {"success": False, "error": "Invalid Content-Length"})
            return
        if length <= 0:
            self.close_connection = True
            self._json(411, {"success": False, "error": "Content-Length required"})
            return
        if length > MAX_UPLOAD:
            self.close_connection = True
            self._json(413, {"success": False, "error": "Upload is too large"})
            return

        try:
            body = self.rfile.read(length)
        except (OSError, ValueError):
            self._json(400, {"success": False, "error": "Could not read request body"})
            return
        if len(body) > MAX_UPLOAD:
            self._json(413, {"success": False, "error": "Upload is too large"})
            return

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"success": False, "error": "Request body must be valid JSON"})
            return
        if not isinstance(data, dict):
            self._json(400, {"success": False, "error": "Request body must be a JSON object"})
            return

        title = _sanitize_line(data.get("title"), "Untitled Save", MAX_TITLE)
        author = _sanitize_line(data.get("author"), "Anonymous", MAX_AUTHOR)
        description = _sanitize_description(data.get("description"))
        save_folder = _sanitize_folder(data.get("saveFolder"))
        raw_files = data.get("files")

        if not isinstance(raw_files, dict):
            self._json(400, {"success": False, "error": "files must be an object"})
            return
        if not (1 <= len(raw_files) <= MAX_FILES):
            self._json(400, {"success": False, "error": "files must contain between 1 and %d entries" % MAX_FILES})
            return

        decoded = []
        seen = set()
        total = 0
        for key, value in raw_files.items():
            rel = safe_relative_path(key)
            if rel is None:
                self._json(400, {"success": False, "error": "Invalid file path"})
                return
            if rel in seen:
                self._json(400, {"success": False, "error": "Duplicate file path"})
                return
            seen.add(rel)

            if not isinstance(value, str):
                self._json(400, {"success": False, "error": "File contents must be base64 strings"})
                return
            try:
                raw = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError, TypeError):
                self._json(400, {"success": False, "error": "Invalid base64 in file: %s" % rel})
                return
            total += len(raw)
            if total > MAX_UPLOAD:
                self._json(413, {"success": False, "error": "Upload is too large"})
                return
            decoded.append((rel, raw))

        seed = ""
        seed_kind = ""
        for rel, raw in sorted(decoded, key=lambda item: (item[0].count("/"), item[0])):
            if rel.lower().endswith(".cbsav"):
                seed, seed_kind = extract_save_seed(raw)
                if seed:
                    break

        timestamp = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

        try:
            os.makedirs(SHARED_DIR, exist_ok=True)
        except OSError:
            self._json(500, {"success": False, "error": "Could not access save storage"})
            return

        overwrite_id = _find_overwrite_id(seed, seed_kind)
        if overwrite_id:
            save_id = overwrite_id
        else:
            save_id = uuid.uuid4().hex
            for _ in range(5):
                if not os.path.exists(os.path.join(SHARED_DIR, save_id)):
                    break
                save_id = uuid.uuid4().hex
        final_dir = os.path.join(SHARED_DIR, save_id)
        tmp_dir = os.path.join(SHARED_DIR, ".tmp-%s" % save_id)
        files_root = os.path.join(tmp_dir, "files")
        backup_dir = None
        overwritten = False

        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(files_root, exist_ok=False)
            base_real = os.path.realpath(files_root)

            for rel, raw in decoded:
                parts = rel.split("/")
                dest = os.path.join(files_root, *parts)
                parent = os.path.dirname(dest)
                os.makedirs(parent, exist_ok=True)
                parent_real = os.path.realpath(parent)
                if parent_real != base_real and not parent_real.startswith(base_real + os.sep):
                    raise ValueError("path escapes save directory")
                if os.path.islink(dest):
                    raise ValueError("symlink in save payload")
                with open(dest, "wb") as fh:
                    fh.write(raw)

            metadata = {
                "id": save_id,
                "title": title,
                "author": author,
                "description": description,
                "saveFolder": save_folder,
                "timestamp": timestamp,
                "fileCount": len(decoded),
                "totalBytes": total,
                "seed": seed,
                "seedKind": seed_kind,
                "format": 1,
            }
            with open(os.path.join(tmp_dir, "metadata.json"), "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, ensure_ascii=False, indent=2)

            if overwrite_id and os.path.isdir(final_dir) and not os.path.islink(final_dir):
                backup_dir = os.path.join(SHARED_DIR, ".old-%s" % save_id)
                shutil.rmtree(backup_dir, ignore_errors=True)
                os.replace(final_dir, backup_dir)
                overwritten = True

            try:
                os.replace(tmp_dir, final_dir)
            except OSError:
                if backup_dir and not os.path.exists(final_dir):
                    try:
                        os.replace(backup_dir, final_dir)
                    except OSError:
                        pass
                raise

            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
        except (OSError, ValueError) as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if backup_dir and not os.path.exists(final_dir):
                try:
                    os.replace(backup_dir, final_dir)
                except OSError:
                    pass
            print("[saves] could not store %s: %s" % (save_id, exc))
            self._json(500, {"success": False, "error": "Could not store save"})
            return

        self._json(200, {
            "success": True,
            "id": save_id,
            "overwritten": overwritten,
            "seed": seed,
            "seedKind": seed_kind,
        })


# ------------------------------------------------------------------ helpers


class _SaveReader:
    """Reads the subset of the Blitz save format that appears before the seed.

    Blitz WriteString is a little-endian int32 length followed by raw bytes;
    WriteInt/WriteFloat are 4 bytes and WriteByte is 1 byte.
    """

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def _take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("truncated save")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def skip(self, count):
        self._take(count)

    def u8(self):
        return self._take(1)[0]

    def i32(self):
        return struct.unpack("<i", self._take(4))[0]

    def string(self, limit=4096):
        size = self.i32()
        if size < 0 or size > limit:
            raise ValueError("invalid string length")
        return self._take(size).decode("utf-8", "replace")


def extract_save_seed(data):
    """Return (seed, seedKind) from a .cbsav, or ("", "") if it cannot be read.

    The map seed is written after LightsOn and a HasNumericSeed flag, preceded
    by a fixed sequence of strings, ints, floats and bytes (plus four custom
    difficulty bytes when the difficulty index is CUSTOM/3).
    """
    try:
        reader = _SaveReader(data)
        reader.string()                 # CurrentTime
        reader.string()                 # CurrentDate
        reader.skip(4)                  # PlayTime
        reader.skip(12)                 # Collider x/y/z
        reader.skip(12)                 # Head x/y/z
        reader.string()                 # AccessCode
        reader.skip(8)                  # camera pitch, entity yaw
        reader.string()                 # CompatibleNumber
        reader.skip(12)                 # BlinkTimer, BlinkEffect, BlinkEffectTimer
        reader.skip(8)                  # DeathTimer, BlurTimer
        reader.skip(4)                  # HealTimer
        reader.u8()                     # Crouch
        reader.skip(12)                 # Stamina, StaminaEffect, StaminaEffectTimer
        reader.skip(8)                  # EyeStuck, EyeIrritation
        reader.skip(16)                 # Injuries, Bloodloss, PrevInjuries, PrevBloodloss
        reader.string()                 # DeathMSG
        reader.skip(24)                 # SCP1025state[0..5]
        reader.skip(4)                  # VomitTimer
        reader.u8()                     # Vomit
        reader.skip(8)                  # CameraShakeTimer, Infect
        difficulty = reader.u8()
        if difficulty == 3:             # CUSTOM difficulty extras
            reader.skip(4)
        reader.skip(8)                  # MonitorTimer, Sanity
        reader.skip(5)                  # wearing flags
        reader.skip(24)                 # NTF_1499 prev/current x/y/z
        reader.skip(8)                  # NTF_1499 previous room x/z
        reader.u8()                     # SuperMan
        reader.skip(4)                  # SuperManTimer
        reader.u8()                     # LightsOn
        numeric = reader.u8()           # HasNumericSeed
        if numeric:
            seed = str(reader.i32())
            kind = "numeric"
        else:
            seed = reader.string(limit=256)
            kind = "text"

        if isinstance(seed, str):
            seed = "".join(
                ch for ch in seed
                if ord(ch) >= 32 and ord(ch) != 127 and not 0xD800 <= ord(ch) <= 0xDFFF
            )[:64]
        if not seed:
            return "", ""
        return seed, kind
    except (ValueError, struct.error, UnicodeError):
        return "", ""


def _sanitize_line(value, default, limit):
    if not isinstance(value, str):
        return default
    text = CONTROL_RE.sub(" ", value)
    text = _strip_surrogates(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text or default


def _sanitize_description(value):
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "")
    # keep newlines, drop every other control character
    text = "".join(
        ch for ch in text
        if (ch == "\n" or ord(ch) >= 32)
        and not (0xD800 <= ord(ch) <= 0xDFFF)
    ).replace("\x7f", "")
    text = text.strip()
    if len(text) > MAX_DESC:
        text = text[:MAX_DESC].rstrip()
    return text


def _sanitize_folder(value):
    if not isinstance(value, str):
        return "Imported Save"
    text = value.replace("/", " ").replace("\\", " ")
    text = CONTROL_RE.sub(" ", text)
    text = _strip_surrogates(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(".").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > 80:
        text = text[:80].strip()
    return text or "Imported Save"


def _strip_surrogates(text):
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def safe_relative_path(path):
    """Validate a relative save path. Returns the path or None when invalid."""
    if not isinstance(path, str):
        return None
    if not path or len(path) > MAX_PATH_LEN:
        return None
    if path.startswith("/"):
        return None
    if "\\" in path:
        return None
    if DRIVE_RE.match(path):
        return None
    for ch in path:
        code = ord(ch)
        if code < 32 or code == 127:
            return None
        if 0xD800 <= code <= 0xDFFF:
            return None
    segments = path.split("/")
    for segment in segments:
        if not segment or segment == "." or segment == "..":
            return None
    return path


def _read_metadata(save_dir):
    try:
        with open(os.path.join(save_dir, "metadata.json"), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _find_seed_in_files(save_dir):
    """Recover a seed from an existing save's .cbsav (for pre-seed metadata)."""
    files_root = os.path.join(save_dir, "files")
    if not os.path.isdir(files_root) or os.path.islink(files_root):
        return "", ""

    candidates = []
    try:
        for root, dirs, names in os.walk(files_root):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in names:
                if not name.lower().endswith(".cbsav"):
                    continue
                full = os.path.join(root, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, files_root)
                candidates.append((rel.count(os.sep), rel, full))
    except OSError:
        return "", ""

    candidates.sort()
    for _, _, full in candidates:
        try:
            with open(full, "rb") as fh:
                seed, kind = extract_save_seed(fh.read(65536))
            if seed:
                return seed, kind
        except OSError:
            continue
    return "", ""


def _effective_seed(meta, save_dir):
    if isinstance(meta, dict):
        seed = meta.get("seed")
        kind = meta.get("seedKind")
        if isinstance(seed, str) and seed and kind in ("numeric", "text"):
            return seed, kind
    return _find_seed_in_files(save_dir)


def _find_overwrite_id(seed, seed_kind):
    """Newest existing save sharing the same seed, or None."""
    if not seed or seed_kind not in ("numeric", "text"):
        return None
    try:
        entries = list(os.scandir(SHARED_DIR))
    except OSError:
        return None

    best = None
    for entry in entries:
        try:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            if not ID_RE.fullmatch(entry.name):
                continue
            meta = _read_metadata(entry.path)
            if not meta:
                continue
            candidate_seed, candidate_kind = _effective_seed(meta, entry.path)
            if candidate_seed != seed or candidate_kind != seed_kind:
                continue
            key = (str(meta.get("timestamp") or ""), entry.name)
            if best is None or key > best[0]:
                best = (key, entry.name)
        except (OSError, ValueError, TypeError):
            continue
    return best[1] if best else None


def main():
    os.makedirs(SHARED_DIR, exist_ok=True)
    handler = functools.partial(IsolationHandler, directory=BASE_DIR)
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"Serving SCP:CB on port {PORT}...")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

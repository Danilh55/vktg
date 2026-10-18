"""
Telegram-бот + FastAPI сервер для онлайн-плеера ВК Музыки (Telegram Mini App).
Обеспечивает:
1. Вход в личный аккаунт ВКонтакте по номеру телефона, почте (с поддержкой 2FA / SMS и капчи) или по токену (Kate Mobile / VK Admin).
2. Получение именно ваших личных аудиозаписей (audio.get) и глобальный поиск по музыке ВК (audio.search).
3. Конвертацию потоков ВК (.m3u8 -> .mp3) и быстрое проксирование реального звука с поддержкой HTTP Range.
4. Работу Telegram-бота (aiogram 3) и WebSocket-хаба глобальных горячих клавиш.
"""

import asyncio
import base64
import os
import re
from typing import Dict, List, Optional
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
WEBAPP_PUBLIC_URL = os.getenv("WEBAPP_PUBLIC_URL", "https://your-domain.onrender.com")
PORT = int(os.getenv("PORT", "8000"))

# Официальный User-Agent мобильного клиента Kate Mobile, которому открыт доступ к методам audio.*
KATE_USER_AGENT = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
VK_API_VERSION = "5.131"

app = FastAPI(title="VK Music Telegram Mini App Backend")

ACTIVE_WS_CLIENTS: List[WebSocket] = []
CURRENT_HOTKEYS: Dict[str, dict] = {
    "playPause": {"label": "Воспроизведение / Пауза", "combo": "Space"},
    "nextTrack": {"label": "Следующий трек", "combo": "Alt+ArrowRight"},
    "prevTrack": {"label": "Предыдущий трек", "combo": "Alt+ArrowLeft"},
    "seekForward": {"label": "Перемотка +10 сек", "combo": "ArrowRight"},
    "seekBackward": {"label": "Перемотка -10 сек", "combo": "ArrowLeft"},
    "volUp": {"label": "Громкость выше (+10%)", "combo": "ArrowUp"},
    "volDown": {"label": "Громкость ниже (-10%)", "combo": "ArrowDown"},
    "toggleMute": {"label": "Без звука (Mute)", "combo": "KeyM"},
    "toggleLike": {"label": "Добавить в избранное ♥", "combo": "KeyL"},
}


class VKLoginRequest(BaseModel):
    mode: str = "pass"
    login: Optional[str] = ""
    password: Optional[str] = ""
    two_fa: Optional[str] = ""
    captcha_sid: Optional[str] = ""
    captcha_key: Optional[str] = ""
    token: Optional[str] = ""


class VKSearchRequest(BaseModel):
    token: str
    query: str


class HotkeysUpdateRequest(BaseModel):
    hotkeys: Dict[str, dict]


class TriggerHotkeyRequest(BaseModel):
    action: str


def extract_clean_token(raw_token: str) -> str:
    """Извлекает access_token, даже если пользователь вставил полную ссылку из адресной строки."""
    s = (raw_token or "").strip()
    m = re.search(r"access_token=([^&\s#]+)", s, re.IGNORECASE)
    if m:
        return m.group(1)
    return s


def convert_vk_m3u8_to_mp3(url: str) -> str:
    """
    Конвертирует HLS-ссылку ВКонтакте (index.m3u8) в прямую ссылку на MP3-файл,
    чтобы трек мгновенно воспроизводился в любом браузере и внутри Telegram Mini App.
    """
    if not url or ".m3u8" not in url:
        return url
    # Стандартная структура HLS URL ВКонтакте:
    # .../audios/<hash>/index.m3u8?extra=... или .../<seg_hash>/<audio_id>/index.m3u8?extra=...
    if "/index.m3u8" in url:
        parts = url.split("?")
        base = parts[0]
        query = f"?{parts[1]}" if len(parts) > 1 else ""
        segments = base.split("/")
        if len(segments) >= 3 and segments[-1] == "index.m3u8":
            # Если перед Audio ID идёт технический хэш-сегмент (без 'audios' и 'p'), убираем его
            if "audios" not in segments[-3] and len(segments) >= 4:
                segments.pop(-3)
            segments.pop(-1)  # убираем index.m3u8
            return "/".join(segments) + ".mp3" + query
    return url


def encode_stream_url(raw_url: str) -> str:
    return base64.urlsafe_b64encode(raw_url.encode("utf-8")).decode("ascii")


def decode_stream_url(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")


def format_vk_tracks(items: list) -> list:
    gradients = [
        "from-blue-600 to-indigo-800",
        "from-rose-600 to-purple-900",
        "from-emerald-600 to-teal-900",
        "from-amber-500 to-orange-800",
        "from-cyan-600 to-blue-900",
        "from-violet-600 to-indigo-950",
    ]
    tracks = []
    for i, item in enumerate(items):
        raw_url = item.get("url")
        if not raw_url:
            continue
        mp3_url = convert_vk_m3u8_to_mp3(raw_url)
        encoded_u = encode_stream_url(mp3_url)
        track_id = f"{item.get('owner_id', 0)}_{item.get('id', i)}"

        # Обложка альбома, если есть
        thumb_url = ""
        album = item.get("album") or {}
        thumb = album.get("thumb") or {}
        thumb_url = thumb.get("photo_300") or thumb.get("photo_270") or thumb.get("photo_135") or ""

        tracks.append({
            "id": track_id,
            "artist": item.get("artist", "Неизвестный исполнитель"),
            "title": item.get("title", "Без названия"),
            "duration": int(item.get("duration", 180)),
            "liked": True,
            "cached": True,
            "thumbUrl": thumb_url,
            "coverGradient": gradients[i % len(gradients)],
            "streamUrl": f"/api/vk/stream?u={encoded_u}",
        })
    return tracks


@app.get("/", response_class=HTMLResponse)
async def serve_miniapp():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


async def fetch_vk_user_and_audios(access_token: str) -> dict:
    """Получает профиль пользователя ВК и список его личных аудиозаписей."""
    headers = {"User-Agent": KATE_USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        user_resp = await client.get(
            "https://api.vk.com/method/users.get",
            params={
                "access_token": access_token,
                "fields": "photo_100",
                "v": VK_API_VERSION,
            },
        )
        user_json = user_resp.json()
        if "error" in user_json:
            err_msg = user_json["error"].get("error_msg", "Ошибка авторизации токена ВК")
            raise HTTPException(status_code=401, detail=err_msg)

        user_info = user_json["response"][0]
        owner_id = user_info["id"]
        full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
        photo_url = user_info.get("photo_100", "")

        # Запрашиваем личные аудиозаписи пользователя (audio.get)
        audio_resp = await client.get(
            "https://api.vk.com/method/audio.get",
            params={
                "access_token": access_token,
                "owner_id": owner_id,
                "count": 300,
                "v": VK_API_VERSION,
            },
        )
        audio_json = audio_resp.json()

        items = []
        if "response" in audio_json and isinstance(audio_json["response"], dict):
            items = audio_json["response"].get("items", [])
        elif "error" in audio_json:
            # Если личные аудио закрыты или список пуст, пробуем рекомендации пользователя (audio.getRecommendations)
            rec_resp = await client.get(
                "https://api.vk.com/method/audio.getRecommendations",
                params={
                    "access_token": access_token,
                    "user_id": owner_id,
                    "count": 100,
                    "v": VK_API_VERSION,
                },
            )
            rec_json = rec_resp.json()
            items = rec_json.get("response", {}).get("items", [])

        tracks = format_vk_tracks(items)
        return {
            "status": "ok",
            "access_token": access_token,
            "user_name": full_name,
            "user_photo": photo_url,
            "tracks": tracks,
        }


@app.post("/api/vk/login")
async def vk_login(payload: VKLoginRequest):
    """
    Вход по номеру телефона / почте и паролю (Direct Auth Kate Mobile с поддержкой 2FA и капчи)
    или по готовому токену.
    """
    token = extract_clean_token(payload.token or "")

    if payload.mode == "pass" and not token:
        params = {
            "grant_type": "password",
            "client_id": "2685278",
            "client_secret": "lxhD8OD7dMsqtXIm5IUY",
            "username": (payload.login or "").strip(),
            "password": payload.password or "",
            "v": VK_API_VERSION,
            "2fa_supported": "1",
            "scope": "audio,offline",
        }
        if payload.two_fa:
            params["code"] = payload.two_fa.strip()
        if payload.captcha_sid and payload.captcha_key:
            params["captcha_sid"] = payload.captcha_sid.strip()
            params["captcha_key"] = payload.captcha_key.strip()

        headers = {"User-Agent": KATE_USER_AGENT}
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            auth_resp = await client.get("https://oauth.vk.com/token", params=params)
            auth_data = auth_resp.json()

            if "access_token" not in auth_data:
                err_type = auth_data.get("error", "")
                err_desc = auth_data.get("error_description", "Ошибка входа ВКонтакте")

                # 1. Если требуется двухфакторная аутентификация (2FA / SMS / Push)
                if err_type == "need_validation":
                    validation_sid = auth_data.get("validation_sid", "")
                    if validation_sid:
                        # Инициируем отправку SMS/Push-кода от ВК
                        try:
                            await client.get(
                                "https://api.vk.com/method/auth.validatePhone",
                                params={"sid": validation_sid, "v": VK_API_VERSION},
                            )
                        except Exception:
                            pass
                    phone_mask = auth_data.get("phone_mask", "")
                    msg = f"Введите код подтверждения ВК (отправлен на {phone_mask or 'ваш телефон / в приложение ВК'})."
                    return JSONResponse(
                        status_code=200,
                        content={
                            "status": "2fa_required",
                            "validation_sid": validation_sid,
                            "message": msg,
                        },
                    )

                # 2. Если ВКонтакте просит ввести капчу
                if err_type == "need_captcha":
                    return JSONResponse(
                        status_code=200,
                        content={
                            "status": "captcha_required",
                            "captcha_sid": auth_data.get("captcha_sid", ""),
                            "captcha_img": auth_data.get("captcha_img", ""),
                        },
                    )

                raise HTTPException(status_code=401, detail=err_desc)

            token = auth_data["access_token"]

    if not token:
        raise HTTPException(status_code=400, detail="Токен или данные для входа не указаны")

    data = await fetch_vk_user_and_audios(token)
    return JSONResponse(content=data)


@app.post("/api/vk/search")
async def vk_search_audio(payload: VKSearchRequest):
    """Глобальный поиск по всей музыке ВКонтакте (audio.search)."""
    token = extract_clean_token(payload.token)
    if not token:
        raise HTTPException(status_code=401, detail="Необходим вход в аккаунт ВК")

    headers = {"User-Agent": KATE_USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        resp = await client.get(
            "https://api.vk.com/method/audio.search",
            params={
                "access_token": token,
                "q": payload.query.strip(),
                "count": 100,
                "auto_complete": 1,
                "v": VK_API_VERSION,
            },
        )
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=data["error"].get("error_msg", "Ошибка поиска ВК"))

        items = data.get("response", {}).get("items", [])
        return {"tracks": format_vk_tracks(items)}


@app.get("/api/vk/stream")
async def stream_vk_audio(request: Request, u: str = Query(...)):
    """Проксирование реального MP3-потока ВКонтакте с поддержкой перемотки (HTTP Range)."""
    try:
        target_url = decode_stream_url(u)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid stream URL")

    headers = {"User-Agent": KATE_USER_AGENT}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    req = client.build_request("GET", target_url, headers=headers)
    resp = await client.send(req, stream=True)

    async def audio_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": resp.headers.get("Content-Type", "audio/mpeg"),
        "Cache-Control": "public, max-age=3600",
    }
    if "Content-Range" in resp.headers:
        response_headers["Content-Range"] = resp.headers["Content-Range"]
    if "Content-Length" in resp.headers:
        response_headers["Content-Length"] = resp.headers["Content-Length"]

    return StreamingResponse(
        audio_generator(),
        status_code=resp.status_code,
        headers=response_headers,
    )


@app.get("/api/hotkeys")
async def get_hotkeys():
    return {"hotkeys": CURRENT_HOTKEYS}


@app.post("/api/hotkeys")
async def save_hotkeys(payload: HotkeysUpdateRequest):
    global CURRENT_HOTKEYS
    CURRENT_HOTKEYS = payload.hotkeys
    return {"status": "ok", "hotkeys": CURRENT_HOTKEYS}


@app.post("/api/hotkeys/trigger")
async def trigger_hotkey(payload: TriggerHotkeyRequest):
    dead = []
    for ws in ACTIVE_WS_CLIENTS:
        try:
            await ws.send_json({"action": payload.action})
        except Exception:
            dead.append(ws)
    for ws in dead:
        ACTIVE_WS_CLIENTS.remove(ws)
    return {"status": "sent", "action": payload.action, "clients": len(ACTIVE_WS_CLIENTS)}


@app.websocket("/ws/hotkeys")
async def ws_hotkeys(websocket: WebSocket):
    await websocket.accept()
    ACTIVE_WS_CLIENTS.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in ACTIVE_WS_CLIENTS:
            ACTIVE_WS_CLIENTS.remove(websocket)


async def start_telegram_bot():
    """Запускает Telegram-бота с кнопкой открытия Mini App."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("[INFO] BOT_TOKEN не задан. Веб-сервер Mini App запущен.")
        return

    from aiogram import Bot, Dispatcher, types
    from aiogram.filters import CommandStart
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎧 Открыть ВК Музыку",
                        web_app=WebAppInfo(url=WEBAPP_PUBLIC_URL),
                    )
                ]
            ]
        )
        await message.answer(
            "Нажмите кнопку ниже, чтобы войти в свой аккаунт ВКонтакте и слушать свою музыку:",
            reply_markup=kb,
        )

    await dp.start_polling(bot)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

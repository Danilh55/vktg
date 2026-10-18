"""
Telegram-бот + FastAPI сервер для онлайн-плеера ВК Музыки (Telegram Mini App).
Обеспечивает:
1. Запуск Telegram-бота (aiogram 3) с кнопкой открытия Mini App плеера.
2. Авторизацию ВКонтакте (через токен Kate Mobile / VK Admin или логин/пароль + 2FA) и получение списка аудиозаписей (audio.get).
3. Быстрое проксирование MP3-потоков с поддержкой HTTP Range-запросов и кэшированием ссылок для мгновенной загрузки.
4. WebSocket-хаб (/ws/hotkeys) для приёма глобальных горячих клавиш на ПК даже когда окно Telegram свёрнуто.
"""

import asyncio
import os
from typing import Dict, List, Optional
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# Настройки из переменных окружения
BOT_TOKEN = os.getenv("8739621843:AAFXUUFR25QXK9ZNUC_2Wp7Wo326wVqNs40")
WEBAPP_PUBLIC_URL = os.getenv("https://subarctic-ivy-craftily.ngrok-free.dev")
PORT = int(os.getenv("PORT", "8000"))

# User-Agent официального мобильного клиента (Kate Mobile) для работы с методами audio.*
KATE_USER_AGENT = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
VK_API_VERSION = "5.131"

app = FastAPI(title="VK Music Telegram Mini App Backend")

# Кэш треков и текущих настроек хоткеев
CACHED_TRACKS: Dict[str, dict] = {}
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
    token: Optional[str] = ""


class HotkeysUpdateRequest(BaseModel):
    hotkeys: Dict[str, dict]


class TriggerHotkeyRequest(BaseModel):
    action: str


@app.get("/", response_class=HTMLResponse)
async def serve_miniapp():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


async def fetch_vk_audio_list(access_token: str) -> dict:
    """Запрашивает пользователя и его аудиозаписи из ВК с помощью Kate Mobile User-Agent."""
    headers = {"User-Agent": KATE_USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        # 1. Получаем имя и ID пользователя
        user_resp = await client.get(
            "https://api.vk.com/method/users.get",
            params={"access_token": access_token, "v": VK_API_VERSION},
        )
        user_json = user_resp.json()
        if "error" in user_json:
            raise HTTPException(status_code=401, detail=user_json["error"].get("error_msg", "VK Auth Error"))

        user_info = user_json["response"][0]
        owner_id = user_info["id"]
        full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()

        # 2. Получаем список аудиозаписей
        audio_resp = await client.get(
            "https://api.vk.com/method/audio.get",
            params={
                "access_token": access_token,
                "owner_id": owner_id,
                "count": 200,
                "v": VK_API_VERSION,
            },
        )
        audio_json = audio_resp.json()
        items = audio_json.get("response", {}).get("items", [])

        gradients = [
            "from-blue-600 to-indigo-800",
            "from-rose-600 to-purple-900",
            "from-emerald-600 to-teal-900",
            "from-amber-500 to-orange-800",
            "from-cyan-600 to-blue-900",
        ]
        tracks = []
        for i, item in enumerate(items):
            url = item.get("url")
            if not url:
                continue
            track_id = f"{item['owner_id']}_{item['id']}"
            CACHED_TRACKS[track_id] = {"url": url, "title": item.get("title", ""), "artist": item.get("artist", "")}
            tracks.append({
                "id": track_id,
                "artist": item.get("artist", "Неизвестный исполнитель"),
                "title": item.get("title", "Без названия"),
                "duration": item.get("duration", 180),
                "liked": True,
                "cached": True,
                "coverGradient": gradients[i % len(gradients)],
                "streamUrl": f"/api/vk/stream/{track_id}",
            })

        return {"user_name": full_name, "tracks": tracks}


@app.post("/api/vk/login")
async def vk_login(payload: VKLoginRequest):
    """Авторизация через токен или Direct Auth ВКонтакте (Kate Mobile client_id=2685278)."""
    token = (payload.token or "").strip()

    if payload.mode == "pass" and not token:
        # Direct Auth Kate Mobile
        params = {
            "grant_type": "password",
            "client_id": "2685278",
            "client_secret": "lxhD8OD7dMsqtXIm5IUY",
            "username": payload.login,
            "password": payload.password,
            "v": VK_API_VERSION,
            "2fa_supported": "1",
        }
        if payload.two_fa:
            params["code"] = payload.two_fa.strip()

        headers = {"User-Agent": KATE_USER_AGENT}
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            auth_resp = await client.get("https://oauth.vk.com/token", params=params)
            auth_data = auth_resp.json()
            if "access_token" not in auth_data:
                raise HTTPException(status_code=401, detail=auth_data.get("error_description", "Ошибка входа ВК"))
            token = auth_data["access_token"]

    if not token:
        raise HTTPException(status_code=400, detail="Токен не указан")

    data = await fetch_vk_audio_list(token)
    return JSONResponse(content=data)


@app.get("/api/vk/stream/{track_id}")
async def stream_vk_audio(track_id: str, request: Request):
    """Высокоскоростной прокси аудиопотока с поддержкой Range (перемотка без задержек)."""
    track_meta = CACHED_TRACKS.get(track_id)
    if not track_meta or not track_meta.get("url"):
        raise HTTPException(status_code=404, detail="Track not found in cache")

    headers = {"User-Agent": KATE_USER_AGENT}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    req = client.build_request("GET", track_meta["url"], headers=headers)
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
    """Вызывается скриптом desktop_hotkeys.py при нажатии глобальной комбинации на ПК."""
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
        print("[INFO] BOT_TOKEN не задан. Веб-сервер Mini App запущен на http://localhost:8000")
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
            "Нажмите кнопку ниже, чтобы открыть плеер ВКонтакте прямо в Telegram:",
            reply_markup=kb,
        )

    await dp.start_polling(bot)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_telegram_bot())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

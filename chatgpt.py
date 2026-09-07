import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from patchright.async_api import async_playwright
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,  # Overrides any default handlers initialized by dependencies
)
logger = logging.getLogger("chatgpt_proxy")

# Silence Uvicorn's default access log to avoid duplicates
logging.getLogger("uvicorn.access").disabled = True

COOKIE_FILE = "cookies.json"
REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class PromptRequest(BaseModel):
    prompt: str


async def _wait_for_new_message(messages, initial_count):
    """Waits until a new assistant message container appears in the DOM."""
    for _ in range(30):
        if await messages.count() > initial_count:
            return messages.nth(-1)
        await asyncio.sleep(1)
    raise TimeoutError("Timed out waiting for the assistant message to appear.")


def load_cookies(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cookie file not found: {filepath}")

    with open(filepath, "r") as f:
        data = json.load(f)

    raw_cookies = data if isinstance(data, list) else data.get("cookies", [])
    sanitized = []

    for c in raw_cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue

        domain = str(c.get("domain", ".chatgpt.com")).strip()
        if "chatgpt.com" in domain and not domain.startswith("."):
            domain = ".chatgpt.com"

        cookie_item = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(c.get("path", "/")),
        }

        if "secure" in c and isinstance(c["secure"], bool):
            cookie_item["secure"] = c["secure"]
        if "httpOnly" in c and isinstance(c["httpOnly"], bool):
            cookie_item["httpOnly"] = c["httpOnly"]

        exp = c.get("expires") or c.get("expirationDate")
        if exp is not None:
            try:
                exp_val = float(exp)
                if exp_val > 0:
                    cookie_item["expires"] = exp_val
            except (ValueError, TypeError):
                pass

        same_site = c.get("sameSite")
        if same_site and isinstance(same_site, str):
            ss_lower = same_site.lower()
            if ss_lower in ["strict", "lax", "none"]:
                cookie_item["sameSite"] = ss_lower.capitalize()

        sanitized.append(cookie_item)

    return sanitized


class ChatGPTClient:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def initialize(self):
        logger.info("[+] Starting background Playwright browser...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--headless=new",
                f"--user-agent={REAL_USER_AGENT}",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )

        self.context = await self.browser.new_context(
            user_agent=REAL_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )

        cookies = load_cookies(COOKIE_FILE)
        added_count = 0
        for cookie in cookies:
            try:
                await self.context.add_cookies([cookie])
                added_count += 1
            except Exception:
                pass

        logger.info(f"[+] Injected {added_count}/{len(cookies)} cookies.")

        self.page = await self.context.new_page()
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        logger.info("[+] Connecting to ChatGPT UI...")
        await self.page.goto("https://chatgpt.com", wait_until="networkidle")

        try:
            await self.page.wait_for_selector("#prompt-textarea", timeout=25000)
            logger.info("[+] Session initialized and ready!")
        except Exception as e:
            logger.error(f"[-] Session setup failed: {e}")
            raise e

    async def reset_chat(self):
        async with self.lock:
            logger.info("[+] Resetting conversation...")
            await self.page.goto("https://chatgpt.com", wait_until="networkidle")
            await self.page.wait_for_selector("#prompt-textarea", timeout=25000)
            logger.info("[+] New conversation thread started.")

    async def close(self):
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

    async def _ensure_ready(self):
        """Ensures the ChatGPT prompt textarea is visible and interactive."""
        try:
            await self.page.wait_for_selector("#prompt-textarea", timeout=5000)
        except Exception:
            logger.warning("[!] Prompt textarea not found, refreshing session view...")
            await self.page.goto("https://chatgpt.com", wait_until="networkidle")
            await self.page.wait_for_selector("#prompt-textarea", timeout=25000)

    async def _wait_for_completion(self, message) -> str:
        """Defensively monitors generation state until output is stable and complete."""
        stop_button = self.page.locator('[data-testid="stop-button"]')
        prompt_box = self.page.locator("#prompt-textarea")

        last_text = ""
        stable_count = 0

        # Multi-layer defensive polling loop (150s max buffer)
        for _ in range(150):
            await asyncio.sleep(1)

            try:
                current_text = (await message.inner_text()).strip()
            except Exception:
                current_text = ""

            is_generating = await stop_button.is_visible()

            try:
                composer_ready = await prompt_box.is_enabled()
            except Exception:
                composer_ready = False

            # If still generating or text hasn't populated yet, reset cycle
            if is_generating or not current_text:
                stable_count = 0
                last_text = current_text
                continue

            # Check text stability once generation stops
            if current_text == last_text:
                stable_count += 1
                # Require 3 consecutive stable checks AND a ready composer input
                if stable_count >= 3 and composer_ready:
                    return current_text
            else:
                stable_count = 0
                last_text = current_text

        # Failsafe fallback if timeout is reached but text exists
        if last_text:
            logger.warning("Generation loop hit time limit; returning latest stable text.")
            return last_text

        raise TimeoutError("Timed out waiting for full response.")

    async def send_prompt(self, prompt: str) -> str:
        async with self.lock:
            await self._ensure_ready()

            messages = self.page.locator('div[data-message-author-role="assistant"]')
            initial_count = await messages.count()

            prompt_box = self.page.locator("#prompt-textarea")
            await prompt_box.fill(prompt)
            await asyncio.sleep(0.3)
            await prompt_box.press("Enter")

            message = await _wait_for_new_message(messages, initial_count)
            return await self._wait_for_completion(message)


client = ChatGPTClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.initialize()
    yield
    await client.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 2. Middleware to log request the INSTANT an endpoint is hit
@app.middleware("http")
async def log_incoming_requests(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"--> [HIT] {request.method} {request.url.path} from {client_ip}")

    try:
        response = await call_next(request)
        logger.info(f"<-- [DONE] {request.method} {request.url.path} | Status: {response.status_code}")
        return response
    except Exception as exc:
        logger.error(f"<-- [FAIL] {request.method} {request.url.path} | Error: {exc}")
        raise exc


@app.get("/", response_class=HTMLResponse)
async def read_index():
    if os.path.exists("index.html"):
        with open("index.html", "r") as f:
            return f.read()
    return "<h1>index.html not found!</h1>"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/api/chat")
async def chat_endpoint(req: PromptRequest):
    logger.info(f"[CHAT PROMPT RECEIVED]: {req.prompt[:60]}...")
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    try:
        reply = await client.send_prompt(req.prompt)
        logger.info("[CHAT RESPONSE SUCCESS]")
        return {"response": reply}
    except Exception as e:
        logger.exception(f"[!] ERROR during send_prompt: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reset")
async def reset_endpoint():
    try:
        await client.reset_chat()
        return {"status": "success", "message": "Conversation reset successfully."}
    except Exception as e:
        logger.exception(f"[!] ERROR during reset_chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        access_log=False,
    )

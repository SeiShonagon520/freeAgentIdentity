"""ChatGPT email registration through the OpenAI web protocol.

All network operations are direct HTTP. Sentinel JavaScript challenges run in
a bounded Node V8 pool and never start a browser executable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlencode, urljoin

from curl_cffi import requests

from .constants import (
    CHATGPT_APP,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    SENTINEL_BASE,
    SENTINEL_FRAME_URL,
    SENTINEL_REQ_URL,
    SENTINEL_SDK_URL,
)
from .environment_profile import (
    FingerprintPool,
    ProtocolEnvironmentProfile,
    _browser_family,
)
from .sentinel_vm import get_sentinel_sdk, get_sentinel_vm_pool

_logger = logging.getLogger(__name__)


FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Daniel", "Matthew", "Anthony", "Mary", "Linda",
    "Jennifer", "Sarah", "Jessica", "Elizabeth",
)
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Martin",
    "Lee", "White",
)


def _random_profile() -> tuple[str, str]:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    age = random.randint(24, 36)
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    return name, birthdate


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _response_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _response_error(response, payload: dict | None = None) -> str:
    data = payload or _response_json(response)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message and code not in message:
            return f"{code}: {message}"
        if message or code:
            return message or code
    if isinstance(error, str) and error:
        return error
    text = str(getattr(response, "text", "") or "").strip()
    return text[:300] or f"HTTP {getattr(response, 'status_code', 0)}"


class _SentinelTokenGenerator:
    """Generate the requirements/enforcement PoW used by OpenAI Sentinel.

    All environment fields are read from a ``ProtocolEnvironmentProfile``
    so that the Python proof and the Node V8 SDK see the same fingerprint.
    """

    def __init__(
        self,
        user_agent: str,
        sdk_url: str = SENTINEL_SDK_URL,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        self.user_agent = user_agent
        self.sdk_url = sdk_url
        self.sid = str(uuid.uuid4())
        self._profile = profile

    @staticmethod
    def _fnv1a32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return f"{value & 0xFFFFFFFF:08x}"

    @staticmethod
    def _encode(value) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    @property
    def _screen(self) -> str:
        if self._profile:
            return f"{self._profile.screen_width}x{self._profile.screen_height}"
        return "1920x1080"

    @property
    def _language(self) -> str:
        return self._profile.language if self._profile else "en-US"

    @property
    def _languages(self) -> str:
        if self._profile:
            return ",".join(self._profile.languages)
        return "en-US,en"

    @property
    def _hardware_concurrency(self) -> int:
        return self._profile.hardware_concurrency if self._profile else 8

    def _now_in_profile_tz(self) -> datetime:
        if self._profile:
            import zoneinfo
            try:
                return datetime.now(zoneinfo.ZoneInfo(self._profile.timezone))
            except Exception:
                pass
        return datetime.now().astimezone()

    def _fingerprint(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            self._screen,
            time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                time.gmtime(),
            ),
            4294705152,
            random.random(),
            self.user_agent,
            self.sdk_url,
            None,
            None,
            self._language,
            self._languages,
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice((4, 8, 12, 16)),
            int(time.time() * 1000 - perf_now),
        ]

    def _reference_fingerprint(self) -> list:
        """25-field fingerprint used by the current Sentinel SDK.

        All environment fields match the ``ProtocolEnvironmentProfile``
        so the Python proof and the V8 SDK expose identical values.
        """
        now = self._now_in_profile_tz()
        perf_now = round(
            time.time() * 1000 - 1_000_000 + random.uniform(1000, 5000), 1
        )
        time_origin = round(time.time() * 1000 - 50_000, 1)
        return [
            3000,
            str(now),
            4294705152,
            0,
            self.user_agent,
            self.sdk_url,
            None,
            self._language,
            self._languages,
            0,
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            self._hardware_concurrency,
            time_origin,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    def _solve_reference_pow(self, seed: str, difficulty: str, data: list) -> str:
        started = time.perf_counter()
        target = str(difficulty or "0")
        for nonce in range(500_000):
            data[3] = nonce
            data[9] = round((time.perf_counter() - started) * 1000)
            encoded = self._encode(data)
            digest = self._fnv1a32(str(seed or "") + encoded)
            if digest[: len(target)] <= target:
                return encoded + "~S"
        return self._encode("e")

    def requirements(self) -> str:
        config = self._reference_fingerprint()
        config[3] = 1
        config[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._solve_reference_pow(
            str(random.random()), "0", config
        )

    def enforcement(self, seed: str, difficulty: str) -> str:
        return "gAAAAAB" + self._solve_reference_pow(
            seed, difficulty, self._reference_fingerprint()
        )


class OpenAISentinelClient:
    def __init__(
        self,
        session,
        *,
        user_agent: str,
        proxy: str | None = None,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        del proxy
        self.session = session
        self.user_agent = user_agent
        self._profile = profile

    @staticmethod
    def _looks_like_vm_error(value: str) -> bool:
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            return False
        lowered = decoded.lower()
        return "syntaxerror" in lowered or "typeerror" in lowered or "error:" in lowered

    def build_headers(self, device_id: str, flow: str) -> dict[str, str]:
        sdk = get_sentinel_sdk(self.session)
        generator = _SentinelTokenGenerator(
            self.user_agent, sdk.url, profile=self._profile
        )
        proof = generator.requirements()
        response = self.session.post(
            SENTINEL_REQ_URL,
            data=json.dumps({"p": proof, "id": device_id, "flow": flow}),
            headers={
                "accept": "*/*",
                "content-type": "text/plain;charset=UTF-8",
                "origin": SENTINEL_BASE,
                "referer": SENTINEL_FRAME_URL,
            },
        )
        chat_req = _response_json(response)
        challenge = str(chat_req.get("token") or "").strip()
        if getattr(response, "status_code", 0) >= 400 or not challenge:
            raise RuntimeError(
                f"Sentinel challenge 获取失败: {_response_error(response, chat_req)}"
            )

        turnstile = chat_req.get("turnstile") or {}
        observer = chat_req.get("so") or {}
        vm: dict = {"t": "", "so": ""}
        if (
            turnstile.get("dx")
            or observer.get("collector_dx")
            or observer.get("snapshot_dx")
        ):
            vm_challenge = dict(chat_req)
            vm_challenge["_python_proof"] = proof
            profile = self._profile
            vm = get_sentinel_vm_pool().execute(
                challenge=vm_challenge,
                sdk=str(sdk.path.resolve()),
                script_src=sdk.url,
                user_agent=self.user_agent,
                flow=flow,
                device_id=device_id,
                page_url=f"{OPENAI_AUTH}/about-you",
                width=profile.screen_width if profile else 1920,
                height=profile.screen_height if profile else 1080,
                cores=profile.hardware_concurrency if profile else 8,
                language=profile.language if profile else "en-US",
                languages=",".join(profile.languages) if profile else "en-US,en",
                no_cookie=profile.no_cookie if profile else True,
            )
        if turnstile.get("required") and not vm.get("t"):
            raise RuntimeError(
                "Sentinel protocol VM did not generate a Turnstile token"
            )

        so_value = str(vm.get("so") or "")
        if so_value and self._looks_like_vm_error(so_value):
            so_value = ""
        pow_info = chat_req.get("proofofwork") or {}
        if pow_info.get("required") and pow_info.get("seed"):
            enforcement = generator.enforcement(
                str(pow_info.get("seed") or ""),
                str(pow_info.get("difficulty") or "0"),
            )
        else:
            enforcement = proof
        token = {
            "p": str(vm.get("p") or enforcement),
            "t": str(vm.get("t") or ""),
            "c": challenge,
            "id": device_id,
            "flow": flow,
        }
        headers = {
            "openai-sentinel-token": json.dumps(token, separators=(",", ":"))
        }
        if so_value:
            headers["openai-sentinel-so-token"] = json.dumps(
                {
                    "so": so_value,
                    "c": challenge,
                    "id": device_id,
                    "flow": flow,
                },
                separators=(",", ":"),
            )
        return headers

    def build_header(self, device_id: str, flow: str) -> str:
        return self.build_headers(device_id, flow)["openai-sentinel-token"]

    def close(self) -> None:
        pass


class ChatGPTProtocolRegister:
    """Synchronous worker compatible with ``ProtocolMailboxAdapter``.

    Accepts a ``ProtocolEnvironmentProfile`` that MUST be internally
    consistent — the startup validation in
    ``ProtocolEnvironmentProfile.validate()`` enforces that before any
    network traffic leaves the process.
    """

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        log_fn: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        impersonate: str = "chrome131",
        session=None,
        request_timeout: float = 60,
        profile: ProtocolEnvironmentProfile | None = None,
    ):
        # --- Profile validation --------------------------------------------------
        # If the caller supplied a profile, use its fields instead of the
        # class-level defaults.  The profile MUST be internally consistent.
        if profile is not None:
            profile.validate()
            self.user_agent = profile.user_agent
            impersonate = profile.impersonate
        self._profile = profile

        self.proxy = str(proxy or "").strip() or None
        self.otp_callback = otp_callback
        self.log = log_fn or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)
        if session is None:
            kwargs = {
                "impersonate": impersonate,
                "timeout": max(float(request_timeout or 60), 1.0),
            }
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            session = requests.Session(**kwargs)
        self.session = session
        self.sentinel = OpenAISentinelClient(
            session,
            user_agent=self.user_agent,
            proxy=self.proxy,
            profile=self._profile,
        )
        self.device_id = str(uuid.uuid4())

        # --- Diagnostic log (non-sensitive summary only) -------------------------
        if self._profile:
            self.log(
                f"环境 profile: {self._profile.name}, "
                f"family={_browser_family(self._profile.user_agent)}, "
                f"imp={self._profile.impersonate}, "
                f"lang={self._profile.language}, "
                f"tz={self._profile.timezone}, "
                f"screen={self._profile.screen_width}x{self._profile.screen_height}"
            )

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise RuntimeError("任务已取消")

    def _common_headers(self, referer: str) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": OPENAI_AUTH,
            "referer": referer,
            "user-agent": self.user_agent,
        }

    def _follow_authorize_chain(self, location: str):
        current = str(location or "").strip()
        for _ in range(15):
            if not current:
                return None
            self._check_cancelled()
            response = self.session.get(urljoin(OPENAI_AUTH, current), allow_redirects=False)
            current = str(response.headers.get("location") or "").strip()
            if not current:
                return response
        raise RuntimeError("OpenAI 授权重定向次数过多")

    def _initialize_signup(self, email: str):
        self.log("初始化 ChatGPT 协议注册会话...")
        response = self.session.get(CHATGPT_APP, allow_redirects=True)
        self._check_cancelled()
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(f"ChatGPT 首页访问失败: {_response_error(response)}")
        csrf_response = self.session.get(f"{CHATGPT_APP}/api/auth/csrf")
        self._check_cancelled()
        csrf_payload = _response_json(csrf_response)
        csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
        if getattr(csrf_response, "status_code", 0) != 200 or not csrf_token:
            raise RuntimeError(f"CSRF 获取失败: {_response_error(csrf_response, csrf_payload)}")

        query = urlencode(
            {
                "prompt": "login",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                "screen_hint": "login_or_signup",
                "login_hint": email,
            }
        )
        signin_response = self.session.post(
            f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
            data=urlencode(
                {
                    "callbackUrl": f"{CHATGPT_APP}/",
                    "csrfToken": csrf_token,
                    "json": "true",
                }
            ),
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_APP,
                "referer": f"{CHATGPT_APP}/",
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        self._check_cancelled()
        signin_payload = _response_json(signin_response)
        location = str(
            signin_payload.get("url")
            or signin_response.headers.get("location")
            or ""
        ).strip()
        if getattr(signin_response, "status_code", 0) >= 400 or not location:
            raise RuntimeError(f"OpenAI 注册授权初始化失败: {_response_error(signin_response, signin_payload)}")
        final_response = self._follow_authorize_chain(location)
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass
        return final_response

    def _validate_otp(self, code: str) -> dict:
        response = self.session.post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            json={"code": code},
            headers=self._common_headers(f"{OPENAI_AUTH}/email-verification"),
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"邮箱验证码校验失败: {_response_error(response, payload)}")
        return payload

    def _register_password(self, email: str, password: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account/password")
        headers.update(self.sentinel.build_headers(
            self.device_id,
            "username_password_create",
        ))
        response = self.session.post(
            OPENAI_API_ENDPOINTS["register"],
            json={"password": password, "username": email},
            headers=headers,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"设置 ChatGPT 密码失败: {_response_error(response, payload)}")
        return payload

    def _login_password(self, password: str, page_response) -> dict:
        """Submit the password to the login form selected by the auth protocol."""
        page_text = str(getattr(page_response, "text", "") or "")
        action_match = re.search(
            r"<form[^>]+action=[\"']([^\"']+)",
            page_text,
            flags=re.IGNORECASE,
        )
        if not action_match:
            raise RuntimeError("ChatGPT protocol login page did not expose a password form")
        form_action = urljoin(OPENAI_AUTH, action_match.group(1))
        hidden_fields: dict[str, str] = {}
        for tag in re.findall(r"<input[^>]*>", page_text, flags=re.IGNORECASE):
            if not re.search(r"type=[\"']hidden[\"']", tag, flags=re.IGNORECASE):
                continue
            name_match = re.search(r"name=[\"']([^\"']+)", tag, flags=re.IGNORECASE)
            value_match = re.search(r"value=[\"']([^\"']*)", tag, flags=re.IGNORECASE)
            if name_match:
                hidden_fields[name_match.group(1)] = value_match.group(1) if value_match else ""
        hidden_fields["password"] = password
        response = self.session.post(
            form_action,
            data=urlencode(hidden_fields),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": OPENAI_AUTH,
                "referer": str(getattr(page_response, "url", "") or f"{OPENAI_AUTH}/log-in/password"),
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"ChatGPT protocol login failed: {_response_error(response, payload)}")
        return {"continue_url": str(response.headers.get("location") or "")}

    def _create_account(self, name: str, birthdate: str) -> dict:
        """Submit the create-account request with a fresh Sentinel proof.

        ``registration_disallowed`` is treated as a policy-level rejection:
        the same account / session / profile is NOT retried immediately,
        because the rejection condition almost never changes within seconds.
        """
        self._check_cancelled()
        headers = self._common_headers(f"{OPENAI_AUTH}/about-you")
        headers.update(
            self.sentinel.build_headers(self.device_id, "oauth_create_account")
        )
        response = self.session.post(
            OPENAI_API_ENDPOINTS["create_account"],
            json={"name": name, "birthdate": birthdate},
            headers=headers,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) < 400 and not payload.get("error"):
            return payload
        last_error = _response_error(response, payload)
        if "registration_disallowed" in last_error:
            self.log(
                f"registration_disallowed (policy rejection) — "
                f"不立即重试同一 session"
            )
        raise RuntimeError(f"创建 ChatGPT 账号失败: {last_error}")

    def _session_result(self, email: str, password: str) -> dict:
        self._check_cancelled()
        response = self.session.get(f"{CHATGPT_APP}/api/auth/session")
        self._check_cancelled()
        payload = _response_json(response)
        access_token = str(payload.get("accessToken") or "").strip()
        if getattr(response, "status_code", 0) != 200 or not access_token:
            raise RuntimeError(f"注册完成但获取 ChatGPT session 失败: {_response_error(response, payload)}")
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        claims = _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        account_id = str(
            auth_claims.get("chatgpt_account_id")
            or account.get("id")
            or ""
        )
        workspace_id = str(auth_claims.get("organization_id") or account_id)
        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = {}
        oauth_tokens: dict = {}
        try:
            from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

            recovered = mint_chatgpt_refresh_token_from_session(
                cookies,
                proxy=self.proxy,
                session=self.session,
                email=email,
                device_id=self.device_id,
                sentinel_client=self.sentinel,
                prefer_account_selection=True,
                cancel_check=self.cancel_check,
            )
            if recovered.get("state") == "valid":
                oauth_tokens = dict(recovered.get("tokens") or {})
                self.log("已获取 OAuth refresh token")
            else:
                self.log(f"未获取 OAuth refresh token: {recovered.get('message', '')}")
        except Exception as exc:
            self.log(f"获取 OAuth refresh token 失败: {exc}")
        return {
            "email": email,
            "password": password,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": str(oauth_tokens.get("access_token") or access_token),
            "session_token": str(payload.get("sessionToken") or ""),
            "refresh_token": str(oauth_tokens.get("refresh_token") or ""),
            "id_token": str(oauth_tokens.get("id_token") or ""),
            "client_id": str(oauth_tokens.get("client_id") or ""),
            "cookies": cookies,
            "profile": account,
            "expires_at": payload.get("expires") or "",
        }

    def login(self, *, email: str, password: str) -> dict:
        """Log in to an existing account without entering the registration flow."""
        if not str(email or "").strip() or not str(password or ""):
            raise RuntimeError("ChatGPT protocol login requires email and password")
        self._check_cancelled()
        self.log(f"Starting ChatGPT protocol login: {email}")
        try:
            login_page = self._initialize_signup(email)
            self._check_cancelled()
            if not callable(self.otp_callback):
                raise RuntimeError("ChatGPT protocol login requires an OTP callback")
            code = str(self.otp_callback() or "").strip()
            self._check_cancelled()
            if not code:
                raise RuntimeError("ChatGPT protocol login did not receive an email code")
            validation = self._validate_otp(code)
            self._check_cancelled()
            continue_url = str(validation.get("continue_url") or "").strip()
            if continue_url:
                login_page = self._follow_authorize_chain(continue_url)
            login_result = self._login_password(password, login_page)
            self._check_cancelled()
            continue_url = str(login_result.get("continue_url") or "").strip()
            if continue_url:
                self._follow_authorize_chain(continue_url)
            result = self._session_result(email, password)
            self.log("ChatGPT protocol login completed and issued a session token")
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass

    def run(self, *, email: str, password: str) -> dict:
        if not str(email or "").strip():
            raise RuntimeError("协议注册缺少邮箱")
        if not callable(self.otp_callback):
            raise RuntimeError("协议注册缺少邮箱验证码回调")
        self._check_cancelled()
        self.log(f"开始 ChatGPT 协议注册: {email}")
        try:
            self._initialize_signup(email)
            code = str(self.otp_callback() or "").strip()
            if not code:
                raise RuntimeError("未收到邮箱验证码")
            validation = self._validate_otp(code)
            self.log("邮箱验证码校验通过")
            continue_url = str(validation.get("continue_url") or "").strip()
            if continue_url:
                self.session.get(
                    urljoin(OPENAI_AUTH, continue_url),
                    headers={
                        "referer": f"{OPENAI_AUTH}/email-verification",
                        "user-agent": self.user_agent,
                    },
                    allow_redirects=True,
                )
            if "password" in continue_url.lower():
                password_result = self._register_password(email, password)
                self.log("ChatGPT 登录密码设置成功")
                password_continue_url = str(password_result.get("continue_url") or "").strip()
                if password_continue_url:
                    self.session.get(
                        urljoin(OPENAI_AUTH, password_continue_url),
                        headers={
                            "referer": f"{OPENAI_AUTH}/create-account/password",
                            "user-agent": self.user_agent,
                        },
                        allow_redirects=True,
                    )
            name, birthdate = _random_profile()
            created = self._create_account(name, birthdate)
            self.log("ChatGPT 账号资料创建成功")
            callback_url = str(created.get("continue_url") or "").strip()
            if callback_url:
                self.session.get(
                    urljoin(OPENAI_AUTH, callback_url),
                    headers={"user-agent": self.user_agent},
                    allow_redirects=True,
                )
            result = self._session_result(email, password)
            self.log("ChatGPT 协议注册完成并已获取 session")
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass

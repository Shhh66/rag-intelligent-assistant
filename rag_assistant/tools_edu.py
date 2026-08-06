"""河海大学教务系统接入 —— CAS 登录 + 课表查询。

认证链路：CAS(authserver.hhu.edu.cn, AES加密) → 门户(my.hhu.edu.cn) → 教务(jwxt.hhu.edu.cn)
"""

import re
import sys
import secrets
import base64

import httpx
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from config import EDU_STUDENT_ID, EDU_PASSWORD

_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def _random_string(n: int) -> str:
    return ''.join(secrets.choice(_AES_CHARS) for _ in range(n))


def encrypt_password(password: str, salt: str) -> str:
    """完全对应 CAS 登录页的 encryptPassword() JS 函数。

    JS 逻辑：randomString(64) + 密码 → AES-CBC(key=salt_u8, iv=random16_u8, PKCS7) → Base64
    """
    key = salt.strip().encode("utf-8")
    iv = _random_string(16).encode("utf-8")
    plaintext = _random_string(64) + password
    if len(key) not in (16, 24, 32):
        key = key[:16]       # CAS salt 通常就是 16 字符，安全兜底
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode()


class HuleSession:
    """河海大学教务会话：CAS 登录 → 门户 cookie → 教务数据"""

    CAS_LOGIN = "https://authserver.hhu.edu.cn/authserver/login"
    CAS_SERVICE = "https://my.hhu.edu.cn/portal-web/j_spring_cas_security_check"
    JWXT_BASE = "https://jwxt.hhu.edu.cn/jsxsd"

    def __init__(self, student_id: str = "", password: str = ""):
        self.student_id = student_id or EDU_STUDENT_ID
        self.password = password or EDU_PASSWORD
        self.client = httpx.Client(         # 同步 Client，方便在 asyncio.to_thread 中调用
            timeout=30.0,
            follow_redirects=False,
            verify=False,                   # 校园网自签证书兼容
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )},
        )

    # ── CAS 登录 ────────────────────────────────────────────

    def login(self) -> bool:
        """三步 CAS 登录，成功后 client 自动保有 pw_route + bzb_jsxsd cookie。
        校园网到 authserver 偶发 SSL 中断，内置 3 次重试。"""
        import time as _time
        for attempt in range(3):
            try:
                return self._login_once()
            except Exception as e:
                if attempt < 2:
                    _time.sleep(0.5 * (attempt + 1))
                else:
                    print(f"   ⚠️ 登录失败(重试3次后): {e}", file=sys.stderr)
                    return False
        return False

    def _login_once(self) -> bool:
        # ① GET CAS 登录页 → 提取 pwdEncryptSalt / execution
        r1 = self.client.get(self.CAS_LOGIN, params={"service": self.CAS_SERVICE})
        salt, execution = self._extract_cas_params(r1.text)
        if not salt:
            print("   ⚠️ 未找到 pwdEncryptSalt，登录页可能已改版", file=sys.stderr)
            return False

        # ② POST 登录(密码 AES 加密)
        enc_pwd = encrypt_password(self.password, salt)
        r2 = self.client.post(
            self.CAS_LOGIN,
            params={"service": self.CAS_SERVICE},
            data={
                "username": self.student_id,
                "password": enc_pwd,
                "pwdEncryptSalt": salt,
                "execution": execution,
                "_eventId": "submit",
                "cllt": "userNameLogin",
                "dllt": "generalLogin",
                "lt": "",
                "captcha": "",
            },
        )
        ticket_url = r2.headers.get("Location", "")
        ticket = self._extract_ticket(ticket_url)
        if not ticket:
            if "验证码" in r2.text or "captcha" in r2.text.lower():
                print("   ❌ CAS 要求验证码(IP被风控)，请稍后重试", file=sys.stderr)
            else:
                print("   ⚠️ 登录失败(CAS 未返回 ticket)，可能是密码错误", file=sys.stderr)
            return False

        # ③ 门户验证 ticket → 拿到 pw_route cookie
        self.client.follow_redirects = True
        self.client.get(self.CAS_SERVICE, params={"ticket": ticket})
        self.client.follow_redirects = False

        # ④ jwxt SSO 跳转 → 获取教务 cookie(bzb_jsxsd)
        #     sso.jsp 用的是 JS 重定向(非 HTTP 302)，需手动跟随
        self._follow_js_redirects(
            f"{self.JWXT_BASE}/framework/xsMain_hehdx.htmlx",
            referer="https://my.hhu.edu.cn/portal-web/",
        )

        return "pw_route" in str(self.client.cookies)

    def _follow_js_redirects(self, url: str, referer: str = "", max_hops: int = 5):
        """手动跟随 JS 重定向(window.location.href)，直到拿到真实页面。"""
        import re as _re
        self.client.follow_redirects = True  # HTTP 302 自动跟
        for _ in range(max_hops):
            r = self.client.get(url, headers={"Referer": referer} if referer else {})
            if len(r.text) > 500:
                return r  # 拿到真实内容了
            # 检查 JS 重定向
            m = _re.search(r"window\.location\.href='([^']+)'", r.text)
            if not m:
                m = _re.search(r'window\.location\.href="([^"]+)"', r.text)
            if not m:
                return r
            url = m.group(1).replace("&amp;", "&")
            referer = str(r.url)
        self.client.follow_redirects = False
        return None

    def _extract_cas_params(self, html: str) -> tuple:
        salt = ""; exec_val = ""
        m = re.search(r'id="pwdEncryptSalt"\s+value="([^"]*)"', html)
        if m: salt = m.group(1)
        m = re.search(r'name="execution"\s+value="([^"]*)"', html)
        if m: exec_val = m.group(1)
        return salt, exec_val

    def _extract_ticket(self, url: str) -> str:
        m = re.search(r'ticket=(ST-[^&\s]+)', url)
        return m.group(1) if m else ""

    def check_session(self) -> bool:
        """检查教务 cookie 是否有效"""
        try:
            r = self.client.get(
                f"{self.JWXT_BASE}/framework/xsMain_hehdx.htmlx",
                follow_redirects=True,
            )
            return "xsMain" in r.text or "课表" in r.text or "xskb" in r.text.lower()
        except Exception:
            return False

    # ── 课表 ────────────────────────────────────────────────

    def fetch_schedule(self, xnxq01id: str = "", zc: str = "") -> str:
        """POST 课表接口 → HTML 解析 → 自然语言文本"""
        self.client.follow_redirects = True
        r = self.client.post(
            f"{self.JWXT_BASE}/xskb/xskb_list.do",
            data={"xnxq01id": xnxq01id, "zc": zc, "kbjcmsid": ""},
            headers={"Referer": f"{self.JWXT_BASE}/framework/xsMain_hehdx.htmlx"},
        )
        self.client.follow_redirects = False
        return _parse_schedule_html(r.text)


# ── HTML 解析 ────────────────────────────────────────────────

def _parse_schedule_html(html: str) -> str:
    """解析 jsxsd 课表 <table id='timetable'> → 自然语言文本"""
    import re as _re

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="timetable")
    if not table:
        return "未找到课表数据"

    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    result = []

    for row in table.find_all("tr"):
        th = row.find("th")
        if not th:
            continue
        period = th.get_text(" ", strip=True)

        for i, td in enumerate(row.find_all("td")):
            for div in td.find_all("div", class_=lambda c: c and "kbcontent" in c):
                raw = div.get_text("\n", strip=True)
                if not raw or raw == "\xa0" or len(raw) < 5:
                    continue

                # 从杂乱数据提取: 课程名/教师/周次/节次/教室
                raw = raw.replace("\n", " ")
                course = ""; teacher = ""; weeks = ""; time_slot = ""; room = ""

                # 教师: 2-4个中文字符,后紧跟数字(周次)
                m = _re.search(r"([一-鿿]{2,4})(\d+-\d+)", raw)
                if m:
                    teacher = m.group(1)
                    weeks = m.group(2)

                # 节次: [数字-数字节]
                m = _re.search(r"\[(\d+-\d+节)\]", raw)
                if m:
                    time_slot = m.group(1)

                # 教室: 【xxx】 或 最后一个"楼"字开头的片段
                m = _re.search(r"【(.+?)】", raw)
                if m:
                    room = m.group(1)

                # 课程名: 取第一行前面部分(教师名前)
                first_line = raw.split(" ")[0]
                clean = _re.sub(r"\d+.*$", "", first_line).strip()
                if clean and len(clean) > 2:
                    course = clean
                elif not course:
                    course = raw[:30]

                # 组装
                parts = [course]
                if teacher:
                    parts.append(teacher)
                if weeks:
                    parts.append(f"{weeks}周")
                if time_slot:
                    parts.append(time_slot)
                if room:
                    parts.append(room)

                day = day_names[i] if i < len(day_names) else f"列{i+1}"
                result.append((f"{day} {period}", " | ".join(parts)))

    # 去重(每单元格多div导致重复),保留信息量最大的
    seen = {}
    for key, info in sorted(result):
        if key not in seen or len(info) > len(seen[key]):
            seen[key] = info

    return "\n".join(f"{k}  {v}" for k, v in sorted(seen.items())) if seen else (
        "课表为空(可能暑假/寒假无课，尝试指定学期参数如 xnxq01id='2024-2025-2')"
    )


# ── 自测 ────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== 河海大学教务系统登录测试 ===")
    edu = HuleSession()
    print(f"学号: {edu.student_id}")
    ok = edu.login()
    print(f"登录: {'✅ 成功' if ok else '❌ 失败'}")

    if ok:
        print("\n=== 课表(2024-2025-2 春季学期) ===")
        schedule = edu.fetch_schedule(xnxq01id="2024-2025-2")
        print(schedule[:800] if schedule else "(课表为空)")

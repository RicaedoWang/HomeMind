"""认证与身份模块（多用户 / 多租户的第一块地基）。

设计原则（都会体现在代码里，不是口号）：
1. **身份来自 token，不来自请求体**。以前 `member_id` 是调用方自己填的，
   等于谁都能自称是爸爸。加了这层之后一律从 token 取，请求体传了也忽略。
2. **账号库与记忆库物理分离**（`data/homemind_accounts.db` vs 记忆库），
   改认证不会动到任何一条真实记忆——迁移风险归零。
3. **密码永不明文**。PBKDF2-SHA256 + 随机盐，比对用 `hmac.compare_digest`。
4. **登录失败不泄露账号是否存在**：用户不存在与密码错误返回同一句话。
"""

from .passwords import hash_password, verify_password
from .store import FAMILIES, Account, AccountStore
from .tokens import AuthUser, decode_token, issue_token

__all__ = [
    "FAMILIES",
    "Account",
    "AccountStore",
    "AuthUser",
    "decode_token",
    "hash_password",
    "issue_token",
    "verify_password",
]

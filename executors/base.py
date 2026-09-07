# -*- coding: utf-8 -*-
"""
签到执行器抽象层

设计目标：把"用什么方式签到"从主流程里解耦出来。
每种签到方式实现一个执行器（Executor），统一输入 SignContext，统一输出 SignResult。
上层（sign_service.sign_site）只负责：取站点 -> 选执行器 -> 重试 -> 回写Cookie与结果。

新增一种签到方式只需：
    1. 新建继承 BaseExecutor 的类，设置 mode / label；
    2. 实现 run(self) -> SignResult；
    3. 在 executors/__init__.py 中 import 该类完成注册。
"""
import json
import logging
import time

from utils import decrypt_data

logger = logging.getLogger(__name__)

ALLOWED_COOKIE_KEYS = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}

# 内置默认成功关键词（未配置 success_rule 时使用）
from utils import SIGN_KEYWORDS  # noqa: E402


def filter_cookies(cookies_list):
    """过滤Cookie列表，仅保留标准字段，并剔除无 name/value 的脏数据"""
    if not cookies_list:
        return []
    return [{k: v for k, v in c.items() if k in ALLOWED_COOKIE_KEYS}
            for c in cookies_list if c.get('name') and c.get('value') is not None]


class SignResult(object):
    """统一的签到结果"""

    def __init__(self, success, message='', cookies=None, detail=''):
        self.success = bool(success)
        self.message = message or ''
        self.cookies = cookies          # 本次执行后得到的最新Cookie列表（None表示不更新）
        self.detail = detail            # 详情（如响应片段），用于排查

    def __bool__(self):
        return self.success

    def __repr__(self):
        return '<SignResult success=%s message=%s>' % (self.success, self.message)


class SignContext(object):
    """
    一次签到执行的上下文：把站点配置解密、整理后交给执行器，
    执行器无需关心数据库与加解密细节。
    """

    def __init__(self, site, ocr_config=None, is_manual=False):
        self.site = site or {}
        self.ocr_config = ocr_config or {}
        self.is_manual = is_manual
        self.start_time = time.time()
        self.vars = {}                  # 变量池：供 API 模式多步请求之间传递数据
        self.cookies_changed = False    # 本次执行中Cookie是否被刷新过
        self._logs = []

        # ---- 解密后的凭据 ----
        self.cookies = self._load_cookies()
        self.username = (self.site.get('username') or '').strip()
        self.password = decrypt_data(self.site['password']) if self.site.get('password') else ''
        self.success_rule = self._load_json(self.site.get('success_rule'))
        self.api_config = self._load_json(self.site.get('api_config'))

    # ---------- 基础属性 ----------
    @property
    def site_id(self):
        return self.site.get('id')

    @property
    def name(self):
        return self.site.get('name', '')

    @property
    def mode(self):
        return (self.site.get('mode') or 'browser').lower()

    @property
    def success_keywords(self):
        """成功关键词：优先使用站点自定义规则，否则用内置关键词"""
        rule = self.success_rule or {}
        words = rule.get('contains')
        if isinstance(words, list) and words:
            return [w for w in words if w]
        return list(SIGN_KEYWORDS)

    # ---------- Cookie ----------
    @property
    def cookie_string(self):
        """把站点Cookie拼成 'a=1; b=2' 形式，供请求头或变量使用"""
        parts = []
        for c in self.cookies:
            name = c.get('name')
            value = c.get('value', '')
            if name:
                parts.append('%s=%s' % (name, value))
        return '; '.join(parts)

    def get_cookie_value(self, name):
        for c in self.cookies:
            if c.get('name') == name:
                return c.get('value', '')
        return ''

    def set_cookies(self, cookies_list):
        self.cookies = filter_cookies(cookies_list)

    # ---------- 日志 ----------
    def log(self, message, level='info'):
        line = str(message)
        self._logs.append(line)
        getattr(logger, level, logger.info)(line)

    @property
    def log_text(self):
        return '\n'.join(self._logs)

    # ---------- 内部工具 ----------
    def _load_cookies(self):
        raw = self.site.get('cookies')
        if not raw:
            return []
        try:
            decrypted = decrypt_data(raw)
            if not decrypted:
                return []
            data = json.loads(decrypted)
            if isinstance(data, list):
                return filter_cookies(data)
            if isinstance(data, dict):
                return filter_cookies([{'name': k, 'value': v} for k, v in data.items()])
        except Exception as e:
            self.log('解析站点Cookie失败: %s' % e, 'warning')
        return []

    @staticmethod
    def _load_json(raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


class BaseExecutor(object):
    """执行器基类"""

    mode = 'base'
    label = '未定义'
    description = ''

    def __init__(self, ctx):
        self.ctx = ctx

    def run(self):
        raise NotImplementedError

    # 便捷访问
    @property
    def site(self):
        return self.ctx.site


# ---------- 执行器注册表 ----------
_EXECUTOR_REGISTRY = {}


def register_executor(cls):
    _EXECUTOR_REGISTRY[cls.mode] = cls
    return cls


def get_executor(mode, ctx):
    """按模式获取执行器实例，未知模式自动回退到浏览器模式"""
    cls = _EXECUTOR_REGISTRY.get((mode or 'browser').lower())
    if cls is None:
        logger.warning('未知签到模式: %s，已回退为 browser' % mode)
        cls = _EXECUTOR_REGISTRY.get('browser')
    return cls(ctx)


def list_executors():
    """返回 [(mode, label, description)]，供页面下拉使用"""
    return [(m, c.label, c.description) for m, c in _EXECUTOR_REGISTRY.items()]


def get_mode_label(mode):
    cls = _EXECUTOR_REGISTRY.get((mode or 'browser').lower())
    return cls.label if cls else (mode or 'browser')

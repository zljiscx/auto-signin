# -*- coding: utf-8 -*-
"""
签到执行器包

导入本包即完成所有执行器注册。新增执行器时在此处补充 import 即可。
浏览器执行器依赖 DrissionPage，导入失败时不影响其它模式使用。
"""
import logging

from .base import (
    BaseExecutor, SignContext, SignResult,
    register_executor, get_executor, list_executors, get_mode_label, filter_cookies
)
from .api import ApiExecutor, normalize_api_config, build_config_from_curl

__all__ = [
    'BaseExecutor', 'SignContext', 'SignResult', 'register_executor',
    'get_executor', 'list_executors', 'get_mode_label', 'filter_cookies',
    'ApiExecutor', 'normalize_api_config', 'build_config_from_curl',
]

logger = logging.getLogger(__name__)

try:
    from .browser import BrowserExecutor, DrissionPageDriver  # noqa: F401
except Exception as e:  # DrissionPage 未安装或环境异常时，仍允许使用 API 模式
    BrowserExecutor = None
    DrissionPageDriver = None
    logger.warning('浏览器执行器不可用（%s），仅能使用非浏览器签到模式' % e)

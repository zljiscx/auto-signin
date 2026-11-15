# -*- coding: utf-8 -*-
"""
桌面浏览器(CDP)签到工作进程

DrissionPage 的页面调试会话一旦断开，本进程内就再也连不上同一个浏览器
（重连必报「与页面的连接已断开」，实测换新标签、清缓存都无效），
而新进程连接总是成功。因此桌面模式每次签到都放到独立子进程执行：
子进程结束即销毁连接，下一个站点重新开始，互不影响。

用法：由 DesktopBrowserExecutor 通过 stdin 传入 JSON，stdout 输出结果标记行。
"""
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 告诉执行器：本进程内直接跑，别再递归开子进程
os.environ['AUTOSIGN_DESKTOP_INPROCESS'] = '1'

RESULT_MARK = '__SIGN_RESULT__'


def _emit(payload):
    sys.stdout.write(RESULT_MARK + json.dumps(payload, ensure_ascii=False, default=str) + '\n')
    sys.stdout.flush()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except Exception as e:
        _emit({'success': False, 'message': '工作进程参数解析失败: %s' % e})
        return 1

    from executors import SignContext, get_executor

    ctx = SignContext(payload.get('site') or {}, payload.get('ocr') or {},
                      is_manual=bool(payload.get('is_manual')))
    result = get_executor('desktop', ctx).run()
    _emit({
        'success': bool(result.success),
        'message': result.message,
        'detail': result.detail,
        'appendix': result.appendix,
        'cookies': result.cookies,
        'cookies_changed': bool(ctx.cookies_changed),
        'browser_headers': ctx.browser_headers,
    })
    return 0


if __name__ == '__main__':
    sys.exit(main())

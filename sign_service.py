import json
import threading
import logging
import time
from datetime import datetime
from models import (get_site, get_all_sites, get_all_configs, add_sign_log,
                    update_site_cookies_and_result, update_site_sign_result,
                    update_site_browser_headers, get_all_sign_times, get_db)
from utils import decrypt_data, encrypt_data, send_wecom_text_message
from executors import SignContext, get_executor, get_mode_label

logger = logging.getLogger(__name__)
_sign_lock = threading.Lock()


def sign_site(site, ocr_config, retry_times=3, is_manual=False):
    """
    单站点签到主函数，内置重试
    职责：组装上下文 -> 按站点 mode 选择执行器 -> 重试 -> 回写Cookie与结果
    :param site: 站点字典（来自数据库）
    :param ocr_config: OCR配置 {'api_key','secret_key'}
    :param retry_times: 重试次数
    :param is_manual: 是否手动触发
    :return: (success: bool, message: str, duration: int)
    """
    start_time = time.time()
    sid = site.get('id')
    name = site.get('name', '')

    ctx = SignContext(site, ocr_config, is_manual=is_manual)
    mode = ctx.mode
    logger.info("站点「%s」使用%s开始签到" % (name, get_mode_label(mode)))

    success = False
    msg = ''
    appendix = ''

    try:
        for attempt in range(1, retry_times + 1):
            logger.info(f"站点「{name}」第 {attempt}/{retry_times} 次尝试")
            try:
                result = get_executor(mode, ctx).run()
                if result is None:
                    msg = '执行器未返回结果'
                else:
                    msg = result.message or ''
                    appendix = getattr(result, 'appendix', '') or ''
                    if result.cookies:
                        ctx.set_cookies(result.cookies)
                    if result.success:
                        success = True
                        break
            except Exception as e:
                msg = f"第{attempt}次异常: {str(e)}"
                logger.error(msg)

            if attempt < retry_times:
                time.sleep(3)

        if not success and not msg:
            msg = "所有重试均失败"

        # 回写Cookie与结果：
        # 1) 有Cookie且需要更新时，连同结果一起写回（避免每次重复登录）
        # 2) 其余任何情况都必须写回结果，否则列表状态不会刷新
        cookies_enc = None
        if ctx.cookies and (success or ctx.cookies_changed):
            try:
                cookies_enc = encrypt_data(json.dumps(ctx.cookies, ensure_ascii=False))
            except Exception as e:
                logger.warning('加密Cookie失败: %s' % e)
        try:
            if cookies_enc:
                update_site_cookies_and_result(sid, cookies_enc, success)
            else:
                update_site_sign_result(sid, success)
        except Exception as e:
            logger.warning('回写签到结果失败: %s' % e)

        # 浏览器登录阶段提取的请求头与签到成功与否无关，单独写回站点，
        # 保证下次纯 API 模式（已有 Cookie、跳过浏览器）也能复用统一请求头
        if ctx.browser_headers:
            try:
                update_site_browser_headers(sid, json.dumps(ctx.browser_headers, ensure_ascii=False))
            except Exception as e:
                logger.warning('回写浏览器请求头失败: %s' % e)

    except Exception as e:
        msg = f"致命异常: {str(e)}"
        logger.error(msg)
        try:
            update_site_sign_result(sid, False)
        except Exception:
            pass

    duration = int(time.time() - start_time)
    return success, msg, duration, appendix

def run_single_sign(site_id, is_manual=False):
    """单站点签到入口（线程安全，手动签到不受当日状态限制）"""
    site = get_site(site_id)
    if not site:
        return False, "站点不存在"
    if not site['enabled']:
        return False, "站点已禁用"
    if not _sign_lock.acquire(blocking=False):
        return False, "当前有签到任务运行中，请稍后再试"
    try:
        configs = get_all_configs()
        ocr_config = {
            'api_key': configs.get('ocr_api_key', ''),
            'secret_key': configs.get('ocr_secret_key', '')
        }
        retry_times = int(configs.get('retry_times', 3))
        success, msg, duration, appendix = sign_site(site, ocr_config, retry_times, is_manual=is_manual)
        add_sign_log(site['id'], site['name'], success, msg, is_manual, duration)
        return success, msg
    finally:
        _sign_lock.release()

def run_all_scheduled_sign():
    """全量定时签到入口 - 仅执行当日未成功签到的站点"""
    logger.info("定时签到任务开始")
    if _sign_lock.locked():
        logger.warning("已有签到任务运行，跳过本次定时执行")
        return
    _sign_lock.acquire()
    results = []
    appendices = []
    skip_count = 0
    try:
        configs = get_all_configs()
        ocr_config = {
            'api_key': configs.get('ocr_api_key', ''),
            'secret_key': configs.get('ocr_secret_key', '')
        }
        retry_times = int(configs.get('retry_times', 3))

        webhook_key = ''
        webhook_enc = configs.get('wecom_webhook_key', '')
        if webhook_enc:
            webhook_key = decrypt_data(webhook_enc) or ''

        # ========== 核心：过滤今日已成功签到的站点 ==========
        today_str = datetime.now().strftime('%Y-%m-%d')
        all_sites = get_all_sites()
        sites = []
        for s in all_sites:
            if not s['enabled']:
                continue
            if s.get('last_sign_time') and s.get('sign_success', 0) == 1:
                if s['last_sign_time'].startswith(today_str):
                    logger.info(f"站点「{s['name']}」今日已签到成功，跳过本次执行")
                    skip_count += 1
                    continue
            sites.append(s)

        logger.info(f"待签到站点数: {len(sites)} (跳过今日已成功 {skip_count} 个)")
        if not sites:
            logger.info("所有站点今日均已签到成功，本次无执行任务")
            return

        for site in sites:
            try:
                success, msg, duration, appendix = sign_site(site, ocr_config, retry_times, is_manual=False)
                add_sign_log(site['id'], site['name'], success, msg, False, duration)
                status = "✅" if success else "❌"
                results.append(f"{status} {site['name']} - {msg}")
                if appendix:
                    appendices.append(appendix)
            except Exception as e:
                results.append(f"⚠️ {site['name']} - 异常: {str(e)}")
                logger.error(f"站点 {site['name']} 异常: {e}")
            time.sleep(2)

        # 推送结果
        if results and webhook_key:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            summary = f"【自动签到结果】\n时间：{now}\n执行 {len(sites)} 个未签，跳过 {skip_count} 个已签\n\n" + "\n".join(results)
            # Buddy 等附加信息块统一追加在所有站点结果之后，与前文空一行
            if appendices:
                summary += "\n\n" + "\n\n".join(appendices)
            send_wecom_text_message(webhook_key, summary)
    except Exception as e:
        logger.error(f"定时签到全局异常: {e}")
    finally:
        _sign_lock.release()
        logger.info("定时签到任务结束")


# ==================== 启动自检补救 ====================
# 补执行前的等待秒数：NAS 刚开机时系统时钟可能尚未完成 NTP 同步（而判定完全依赖「今天」），
# 网络/DNS 也可能未就绪，留出缓冲再执行更稳妥。
MAKEUP_DELAY_SEC = 60


def _last_past_sign_time_today():
    """今天已过去的最晚一个签到时间点（datetime）；没有则返回 None。

    关键点：所有签到时间一律拼「今天」的日期来构造，因此天然不跨天。
    例如早上 7 点开机时，今天的时间点都还没到（返回 None），不会去检查昨晚那次。
    """
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    latest = None
    for t in get_all_sign_times() or []:
        if t.get('enabled', 1) != 1:
            continue
        try:
            tt = datetime.strptime('%s %s' % (today_str, t['time_str']), '%Y-%m-%d %H:%M')
        except Exception:
            continue
        if tt < now and (latest is None or tt > latest):
            latest = tt
    return latest


def _auto_sign_executed_since(since_dt):
    """since_dt 之后是否已有自动签到记录（说明那次定时签到确实执行过）。"""
    since_str = since_dt.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sign_logs WHERE is_manual=0 AND sign_time >= ?",
                (since_str,)
            ).fetchone()
        return bool(row and row['cnt'] > 0)
    except Exception as e:
        logger.warning('查询签到日志失败: %s' % e)
        return False


def run_startup_makeup(delay_sec=MAKEUP_DELAY_SEC):
    """启动时自检补救（仅在程序启动时调用一次）。

    今天已过去的最后一个签到时间点若没执行过，就补执行一次。
    重复签到由 run_all_scheduled_sign 内部「今日已成功站点跳过」天然拦住，
    因此同一天多次重启也不会重复签到；全部跳过时该函数直接返回、不推送。
    """
    try:
        target = _last_past_sign_time_today()
        if target is None:
            logger.info("启动自检：今天还没有已过去的签到时间点，无需补救")
            return
        if _auto_sign_executed_since(target):
            logger.info("启动自检：%s 的定时签到今天已执行，忽略" % target.strftime('%H:%M'))
            return
        logger.info("启动自检：%s 的定时签到今天未执行，%d 秒后补救执行一次"
                    % (target.strftime('%H:%M'), delay_sec))

        def _worker():
            time.sleep(delay_sec)
            logger.info("启动自检补救：开始补执行（原定 %s）" % target.strftime('%H:%M'))
            run_all_scheduled_sign()

        threading.Thread(target=_worker, name='startup-makeup', daemon=True).start()
    except Exception as e:
        logger.error("启动自检补救异常: %s" % e)

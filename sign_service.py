import json
import threading
import logging
import time
from datetime import datetime
from models import (get_site, get_all_sites, get_all_configs, add_sign_log,
                    update_site_cookies_and_result, update_site_sign_result)
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

    try:
        for attempt in range(1, retry_times + 1):
            logger.info(f"站点「{name}」第 {attempt}/{retry_times} 次尝试")
            try:
                result = get_executor(mode, ctx).run()
                if result is None:
                    msg = '执行器未返回结果'
                else:
                    msg = result.message or ''
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

    except Exception as e:
        msg = f"致命异常: {str(e)}"
        logger.error(msg)
        try:
            update_site_sign_result(sid, False)
        except Exception:
            pass

    duration = int(time.time() - start_time)
    return success, msg, duration

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
        success, msg, duration = sign_site(site, ocr_config, retry_times, is_manual=is_manual)
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
                success, msg, duration = sign_site(site, ocr_config, retry_times, is_manual=False)
                add_sign_log(site['id'], site['name'], success, msg, False, duration)
                status = "✅" if success else "❌"
                results.append(f"{status} {site['name']} - {msg}")
            except Exception as e:
                results.append(f"⚠️ {site['name']} - 异常: {str(e)}")
                logger.error(f"站点 {site['name']} 异常: {e}")
            time.sleep(2)

        # 推送结果
        if results and webhook_key:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            summary = f"【自动签到结果】\n时间：{now}\n本次执行 {len(sites)} 个站点，跳过 {skip_count} 个已成功站点\n\n" + "\n".join(results)
            send_wecom_text_message(webhook_key, summary)
    except Exception as e:
        logger.error(f"定时签到全局异常: {e}")
    finally:
        _sign_lock.release()
        logger.info("定时签到任务结束")

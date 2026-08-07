from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime
from models import get_all_sites, get_all_configs
from signer import sign_site
from utils import send_wecom_text_message, decrypt_data

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def scheduled_sign():
    logger.info(f"[{datetime.now()}] 定时签到开始...")
    configs = get_all_configs()
    ocr_config = {
        'api_key': configs.get('ocr_api_key', ''),
        'secret_key': configs.get('ocr_secret_key', '')
    }
    retry_times = int(configs.get('retry_times', 3))

    webhook_key_encrypted = configs.get('wecom_webhook_key', '')
    webhook_key = decrypt_data(webhook_key_encrypted) if webhook_key_encrypted else ''

    sites = get_all_sites()
    enabled_sites = [s for s in sites if s['enabled']]

    results = []
    for site in enabled_sites:
        logger.info(f"签到站点: {site['name']}")
        try:
            # 自动签到，is_manual=False（默认）
            success, msg = sign_site(site, ocr_config, retry_times)
            if success:
                results.append(f"✅ {site['name']} 签到成功。")
            else:
                results.append(f"❌ {site['name']} 签到失败。")
            logger.info(f"  结果: {msg}")
        except Exception as e:
            results.append(f"⚠️ {site['name']} 签到异常: {str(e)}")
            logger.error(f"  签到异常: {e}")

    logger.info("定时签到结束")

    if results and webhook_key:
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        summary = f"【自动签到汇总】\n时间：{current_time}\n\n" + "\n".join(results)
        send_wecom_text_message(webhook_key, summary)


def start_scheduler(sign_time='05:05'):
    global scheduler
    # 如果调度器已关闭，重新创建实例
    if not scheduler.running:
        scheduler = BackgroundScheduler()
    hour, minute = sign_time.split(':')
    scheduler.add_job(
        scheduled_sign,
        trigger=CronTrigger(hour=int(hour), minute=int(minute)),
        id='daily_sign',
        replace_existing=True
    )
    scheduler.start()
    logger.info(f"调度器已启动，每天 {sign_time} 执行签到")


def stop_scheduler():
    global scheduler
    if scheduler.running:
        scheduler.shutdown()
    # 无论是否运行，重置为新实例，确保下次 start 可用
    scheduler = BackgroundScheduler()
    logger.info("调度器已停止")
